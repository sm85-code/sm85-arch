"""One guarded path for ads changes: budget, target ROAS, pause, and resume.

Dry-run is the default. Caps always apply. A real write needs the owner's approval
(``persetujuan`` = ``ya``) unless that shop's autonomy flag is on. STOP blocks every write.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_guardrail
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import models_agen, shopee_iklan
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

JENIS = {"anggaran": "change_budget", "roas": "change_roas_target", "jeda": "pause", "lanjut": "resume"}
# Shopee marks a blocked keyword as status "blacklist" (or "deleted"). The edit API has no separate
# negative-keyword field: delete takes a live keyword out of the auction, restore puts it back.
JENIS_KATA = {"bid", "kata_kunci", "kata_negatif"}
BALIK = {"jeda": "lanjut", "lanjut": "jeda"}
NEGATIF = {"tambah": "delete", "hapus": "restore"}


class KataPerubahan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aksi: str
    kata: str = Field(min_length=1, max_length=80)
    bid: Decimal | None = None
    tipe: str | None = None


class AksiIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jenis: str
    campaign_id: int = Field(gt=0)
    nilai: Decimal | None = None
    kata: str | None = Field(None, max_length=80)
    kata_kunci: list[KataPerubahan] | None = None
    alasan: str = Field("", max_length=500)
    kering: bool = True
    persetujuan: str | None = None
    request_id: str | None = Field(None, max_length=64)


class KembaliIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alasan: str = Field("", max_length=500)
    kering: bool = True
    persetujuan: str | None = None
    request_id: str | None = Field(None, max_length=64)


def _desimal(nilai) -> Decimal | None:
    if nilai in (None, ""):
        return None
    try:
        angka = Decimal(str(nilai))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="Nilai iklan tidak terbaca") from exc
    if not angka.is_finite():
        raise HTTPException(status_code=422, detail="Nilai iklan harus angka terbatas")
    return angka


def persen_ubah(lama: Decimal, baru: Decimal) -> Decimal:
    if lama <= 0:
        raise HTTPException(status_code=422, detail="Nilai sekarang nol atau tidak terbatas, jadi persen perubahan tidak bisa diukur")
    return (abs(baru - lama) / lama) * Decimal(100)


def _daftar_kata(manual: dict) -> list[dict]:
    keluar = []
    for baris in manual.get("selected_keywords") or []:
        if not isinstance(baris, dict) or not baris.get("keyword") or baris.get("status") == "deleted":
            continue
        keluar.append({
            "kata": str(baris["keyword"]),
            "status": baris.get("status"),
            "tipe": baris.get("match_type"),
            "bid": _desimal(baris.get("bid_price_per_click")),
            "negatif": baris.get("status") == "blacklist",
        })
    return keluar


def _cuplikan(pengaturan: dict) -> dict:
    info = pengaturan.get("common_info") or {}
    auto = pengaturan.get("auto_bidding_info") or {}
    manual = pengaturan.get("manual_bidding_info") or {}
    return {
        "campaign_id": str(pengaturan.get("campaign_id")),
        "anggaran": _desimal(info.get("campaign_budget")),
        "roas_target": _desimal(auto.get("roas_target")),
        "status": info.get("campaign_status"),
        "kata": _daftar_kata(manual),
    }


def _cari_kata(sekarang: dict, kata: str) -> dict | None:
    for baris in sekarang.get("kata") or []:
        if baris.get("kata") == kata:
            return baris
    return None


def _cek_bid(lama: Decimal | None, baru: Decimal, batas: dict, *, baru_sekali: bool, otonomi: bool, longgar: bool = False) -> None:
    cap = batas.get("maks_cpc")
    if cap is not None and baru > Decimal(str(cap)):
        raise HTTPException(status_code=422, detail="Bid melebihi CPC maksimum toko")
    if not longgar and lama is not None and lama > 0 and persen_ubah(lama, baru) > Decimal(batas["maks_persen_perubahan"]):
        raise HTTPException(status_code=422, detail=f"Perubahan bid melebihi {batas['maks_persen_perubahan']}%")
    if not longgar and baru_sekali and otonomi and cap is None:
        raise HTTPException(status_code=422, detail="Otonomi butuh CPC maksimum sebelum menambah bid baru")


def _susun_kata(body: AksiIn, sekarang: dict, batas: dict, *, otonomi: bool, longgar: bool = False) -> dict:
    """Return sesudah including the Shopee keyword list and the inverse edits for rollback."""
    if body.jenis == "bid":
        if not body.kata or body.nilai is None:
            raise HTTPException(status_code=422, detail="Bid butuh kata dan nilai")
        ada = _cari_kata(sekarang, body.kata)
        if ada is None or ada.get("negatif"):
            raise HTTPException(status_code=422, detail="Kata kunci aktif tidak ditemukan")
        _cek_bid(ada.get("bid"), body.nilai, batas, baru_sekali=False, otonomi=otonomi, longgar=longgar)
        perubahan = [{"aksi": "change_bid_price", "kata": body.kata, "bid": body.nilai}]
        balik = [{"aksi": "change_bid_price", "kata": body.kata, "bid": ada.get("bid")}]
    elif body.jenis == "kata_negatif":
        if not body.kata_kunci:
            raise HTTPException(status_code=422, detail="kata_kunci wajib untuk kata negatif")
        perubahan, balik = [], []
        for item in body.kata_kunci:
            if item.aksi not in NEGATIF:
                raise HTTPException(status_code=422, detail="Kata negatif hanya tambah atau hapus")
            ada = _cari_kata(sekarang, item.kata)
            if item.aksi == "tambah" and (ada is None or ada.get("negatif")):
                raise HTTPException(status_code=422, detail=f"'{item.kata}' bukan kata yang sedang ditawar, jadi tidak bisa diblokir")
            if item.aksi == "hapus" and (ada is None or not ada.get("negatif")):
                raise HTTPException(status_code=422, detail=f"'{item.kata}' bukan kata negatif")
            perubahan.append({"aksi": NEGATIF[item.aksi], "kata": item.kata})
            balik.append({"aksi": "restore" if item.aksi == "tambah" else "delete", "kata": item.kata})
    elif body.jenis == "kata_kunci":
        if not body.kata_kunci:
            raise HTTPException(status_code=422, detail="kata_kunci wajib")
        perubahan, balik = [], []
        for item in body.kata_kunci:
            ada = _cari_kata(sekarang, item.kata)
            if item.aksi in {"add", "change_bid_price"}:
                if item.bid is None:
                    raise HTTPException(status_code=422, detail=f"Bid untuk '{item.kata}' wajib")
                _cek_bid(None if ada is None else ada.get("bid"), item.bid, batas, baru_sekali=ada is None, otonomi=otonomi, longgar=longgar)
            perubahan.append({"aksi": item.aksi, "kata": item.kata, "bid": item.bid, "tipe": item.tipe})
            if ada and ada.get("bid") is not None and item.aksi in {"add", "change_bid_price"}:
                balik.append({"aksi": "change_bid_price", "kata": item.kata, "bid": ada["bid"], "tipe": ada.get("tipe")})
            elif item.aksi == "add":
                balik.append({"aksi": "delete", "kata": item.kata})
            elif item.aksi == "delete":
                balik.append({"aksi": "restore", "kata": item.kata, "bid": None if ada is None else ada.get("bid"), "tipe": None if ada is None else ada.get("tipe")})
            elif ada is not None:
                balik.append({"aksi": item.aksi, "kata": item.kata, "bid": ada.get("bid"), "tipe": ada.get("tipe")})
    else:
        raise HTTPException(status_code=422, detail="Jenis kata kunci tidak dikenal")
    sesudah = dict(sekarang)
    sesudah["perubahan"] = perubahan
    sesudah["balik"] = balik
    return sesudah


async def _baca_kampanye(session, akun, campaign_id: int) -> dict:
    data = await shopee_iklan.panggil(
        session, akun, "pengaturan_kampanye",
        params={"info_type_list": "1,2,3", "campaign_id_list": str(campaign_id)},
    )
    daftar = ((data or {}).get("response") or {}).get("campaign_list") or []
    for row in daftar:
        if isinstance(row, dict) and str(row.get("campaign_id")) == str(campaign_id):
            return _cuplikan(row)
    raise HTTPException(status_code=404, detail="Kampanye tidak ditemukan di Shopee")


def _nilai_baru(jenis: str, sekarang: dict, nilai: Decimal | None) -> dict:
    if jenis not in JENIS:
        raise HTTPException(status_code=422, detail="Jenis aksi harus anggaran, roas, jeda, lanjut, bid, kata_kunci, atau kata_negatif")
    sesudah = dict(sekarang)
    if jenis == "anggaran":
        if nilai is None:
            raise HTTPException(status_code=422, detail="Anggaran baru wajib diisi")
        sesudah["anggaran"] = nilai
    elif jenis == "roas":
        if nilai is None:
            raise HTTPException(status_code=422, detail="Target ROAS baru wajib diisi")
        sesudah["roas_target"] = nilai
    elif jenis == "jeda":
        sesudah["status"] = "paused"
    else:
        sesudah["status"] = "ongoing"
    return sesudah


def _cek_batas(jenis: str, sekarang: dict, sesudah: dict, batas: dict) -> None:
    if jenis == "anggaran":
        lama, baru = sekarang.get("anggaran"), sesudah["anggaran"]
        if lama is None:
            raise HTTPException(status_code=422, detail="Anggaran kampanye sekarang tidak terbaca")
        if persen_ubah(lama, baru) > Decimal(batas["maks_persen_perubahan"]):
            raise HTTPException(status_code=422, detail=f"Perubahan anggaran melebihi {batas['maks_persen_perubahan']}%")
        cap = batas.get("anggaran_harian_maks")
        if cap is not None and baru > Decimal(str(cap)):
            raise HTTPException(status_code=422, detail="Anggaran baru melebihi plafon harian toko")
    elif jenis == "roas":
        lama, baru = sekarang.get("roas_target"), sesudah["roas_target"]
        if lama is None:
            raise HTTPException(status_code=422, detail="Target ROAS sekarang tidak terbaca")
        if persen_ubah(lama, baru) > Decimal(batas["maks_persen_perubahan"]):
            raise HTTPException(status_code=422, detail=f"Perubahan ROAS melebihi {batas['maks_persen_perubahan']}%")


async def _terlalu_cepat(session, akun_id: str, campaign_id: int, jenis: str, min_hari: int) -> bool:
    if min_hari <= 0:
        return False
    sejak = datetime.now(timezone.utc) - timedelta(days=min_hari)
    ada = (await session.execute(
        select(models_agen.AgenIklanAudit.id).where(
            models_agen.AgenIklanAudit.akun_id == akun_id,
            models_agen.AgenIklanAudit.kampanye_id == str(campaign_id),
            models_agen.AgenIklanAudit.aksi == jenis,
            models_agen.AgenIklanAudit.kering.is_(False),
            models_agen.AgenIklanAudit.hasil == "diterapkan",
            models_agen.AgenIklanAudit.dibuat_at >= sejak,
        ).limit(1)
    )).first()
    return ada is not None


def _badan(jenis: str, campaign_id: int, sesudah: dict, request_id: str | None) -> dict:
    payload = {"aksi": JENIS[jenis], "reference_id": request_id}
    if jenis == "anggaran":
        payload["budget"] = sesudah["anggaran"]
    elif jenis == "roas":
        payload["roas_target"] = sesudah["roas_target"]
    return erp_shopee.susun_aksi_kampanye(campaign_id, payload)


def _json_aman(data):
    """Decimals become plain numbers so the audit JSON and the HTTP body match."""
    if isinstance(data, Decimal):
        return agen_guardrail._angka(data)
    if isinstance(data, dict):
        return {kunci: _json_aman(nilai) for kunci, nilai in data.items()}
    if isinstance(data, list):
        return [_json_aman(nilai) for nilai in data]
    return data


async def _tolak(session, *, akun, jenis, campaign_id, pengguna, sebelum, sesudah, body, hasil, status, detail, kering):
    await agen_guardrail._catat(
        session, akun_id=akun.id, aksi=jenis, kampanye_id=str(campaign_id), pengguna=pengguna,
        sebelum=_json_aman(sebelum), sesudah=_json_aman({**sesudah, "ditolak": detail}),
        alasan=getattr(body, "alasan", ""), request_id=getattr(body, "request_id", None),
        persetujuan=getattr(body, "persetujuan", None), kering=kering, hasil=hasil,
    )
    await session.commit()
    raise HTTPException(status_code=status, detail=detail)


async def terapkan(session: AsyncSession, akun, body: AksiIn, pengguna, *, kembalikan: bool = False) -> dict:
    if getattr(akun, "platform", None) != "shopee":
        raise HTTPException(status_code=409, detail="API iklan ini khusus toko Shopee.")
    if body.jenis not in JENIS and body.jenis not in JENIS_KATA:
        raise HTTPException(status_code=422, detail="Jenis aksi harus anggaran, roas, jeda, lanjut, bid, kata_kunci, atau kata_negatif")
    ada = await agen_guardrail._sudah(session, akun.id, body.request_id)
    if ada is not None:
        ulang = agen_guardrail._muat(ada.sesudah_json)
        ulang["audit_id"] = ada.id
        ulang["diulang"] = True
        return ulang
    alasan_stop = await agen_guardrail.tulisan_diblokir(session, akun.id)
    if alasan_stop:
        await _tolak(
            session, akun=akun, jenis="kembalikan" if kembalikan else body.jenis, campaign_id=body.campaign_id,
            pengguna=pengguna, sebelum={}, sesudah={}, body=body, hasil="ditolak", status=409,
            detail=alasan_stop, kering=body.kering,
        )
    sekarang = await _baca_kampanye(session, akun, body.campaign_id)
    batas = await agen_guardrail.baca_batas(session, akun.id)
    if body.jenis in JENIS_KATA:
        try:
            sesudah = _susun_kata(body, sekarang, batas, otonomi=bool(batas["otonomi"]) and not kembalikan, longgar=kembalikan)
        except HTTPException as exc:
            await _tolak(
                session, akun=akun, jenis=body.jenis, campaign_id=body.campaign_id, pengguna=pengguna,
                sebelum=sekarang, sesudah={}, body=body, hasil="ditolak", status=exc.status_code,
                detail=str(exc.detail), kering=body.kering,
            )
    else:
        sesudah = _nilai_baru(body.jenis, sekarang, body.nilai)
    if not kembalikan:
        try:
            _cek_batas(body.jenis, sekarang, sesudah, batas)
        except HTTPException as exc:
            await _tolak(
                session, akun=akun, jenis=body.jenis, campaign_id=body.campaign_id, pengguna=pengguna,
                sebelum=sekarang, sesudah=sesudah, body=body, hasil="ditolak", status=exc.status_code,
                detail=str(exc.detail), kering=body.kering,
            )
        if body.jenis in {"anggaran", "roas", "bid"} and await _terlalu_cepat(
            session, akun.id, body.campaign_id, body.jenis, int(batas["min_hari_antar_perubahan"])
        ):
            await _tolak(
                session, akun=akun, jenis=body.jenis, campaign_id=body.campaign_id, pengguna=pengguna,
                sebelum=sekarang, sesudah=sesudah, body=body, hasil="ditolak", status=409,
                detail=f"Perubahan {body.jenis} yang sama baru saja diterapkan", kering=body.kering,
            )
    else:
        cap = batas.get("anggaran_harian_maks")
        if body.jenis == "anggaran" and cap is not None and sesudah["anggaran"] > Decimal(str(cap)):
            await _tolak(
                session, akun=akun, jenis="kembalikan", campaign_id=body.campaign_id, pengguna=pengguna,
                sebelum=sekarang, sesudah=sesudah, body=body, hasil="ditolak", status=422,
                detail="Nilai sebelum melebihi plafon harian toko", kering=body.kering,
            )
        cpc = batas.get("maks_cpc")
        if cpc is not None and body.jenis in JENIS_KATA:
            for item in sesudah.get("perubahan") or []:
                if item.get("bid") is not None and Decimal(str(item["bid"])) > Decimal(str(cpc)):
                    await _tolak(
                        session, akun=akun, jenis="kembalikan", campaign_id=body.campaign_id, pengguna=pengguna,
                        sebelum=sekarang, sesudah=sesudah, body=body, hasil="ditolak", status=422,
                        detail="Bid sebelum melebihi CPC maksimum toko", kering=body.kering,
                    )
    disetujui = body.persetujuan == "ya"
    if kembalikan and not body.kering and not disetujui:
        await _tolak(
            session, akun=akun, jenis="kembalikan", campaign_id=body.campaign_id, pengguna=pengguna,
            sebelum=sekarang, sesudah=sesudah, body=body, hasil="ditolak", status=409,
            detail="Pengembalian butuh persetujuan pemilik (ya)", kering=body.kering,
        )
    if not kembalikan and not batas["otonomi"] and not disetujui and not body.kering:
        await _tolak(
            session, akun=akun, jenis=body.jenis, campaign_id=body.campaign_id, pengguna=pengguna,
            sebelum=sekarang, sesudah=sesudah, body=body, hasil="ditolak", status=409,
            detail="Butuh persetujuan pemilik (ya), atau nyalakan otonomi toko di dalam batas", kering=False,
        )
    sebelum_simpan = dict(sekarang)
    if sesudah.get("balik"):
        sebelum_simpan["balik"] = sesudah["balik"]
    jawaban = {
        "kering": body.kering,
        "diterapkan": False,
        "butuh_persetujuan": not disetujui and (kembalikan or not batas["otonomi"]),
        "jenis": "kembalikan" if kembalikan else body.jenis,
        "campaign_id": str(body.campaign_id),
        "sebelum": _json_aman(sebelum_simpan),
        "sesudah": _json_aman(sesudah),
    }
    if body.kering or jawaban["butuh_persetujuan"]:
        row = await agen_guardrail._catat(
            session, akun_id=akun.id, aksi=jawaban["jenis"], kampanye_id=str(body.campaign_id), pengguna=pengguna,
            sebelum=jawaban["sebelum"], sesudah=jawaban, alasan=body.alasan, request_id=body.request_id,
            persetujuan=body.persetujuan, kering=True, hasil="kering",
        )
        await session.commit()
        jawaban["audit_id"] = row.id
        return jawaban
    if body.jenis in JENIS_KATA:
        badan = erp_shopee.susun_kata_kunci(body.campaign_id, {"reference_id": body.request_id, "kata_kunci": sesudah["perubahan"]})
        tujuan = "ubah_kata_kunci"
    else:
        badan = _badan(body.jenis, body.campaign_id, sesudah, body.request_id)
        tujuan = "ubah_manual"
    try:
        await shopee_iklan.panggil(session, akun, tujuan, body=badan)
    except HTTPException as exc:
        await _tolak(
            session, akun=akun, jenis=jawaban["jenis"], campaign_id=body.campaign_id, pengguna=pengguna,
            sebelum=sekarang, sesudah=sesudah, body=body, hasil="gagal", status=exc.status_code,
            detail=str(exc.detail), kering=False,
        )
    jawaban["diterapkan"] = True
    jawaban["kering"] = False
    row = await agen_guardrail._catat(
        session, akun_id=akun.id, aksi=jawaban["jenis"], kampanye_id=str(body.campaign_id), pengguna=pengguna,
        sebelum=jawaban["sebelum"], sesudah=jawaban, alasan=body.alasan, request_id=body.request_id,
        persetujuan=body.persetujuan, kering=False, hasil="diterapkan",
    )
    await session.commit()
    jawaban["audit_id"] = row.id
    return jawaban


async def kembalikan(session: AsyncSession, akun, audit_id: str, body: KembaliIn, pengguna) -> dict:
    row = await session.get(models_agen.AgenIklanAudit, audit_id)
    if row is None or row.akun_id != akun.id or row.hasil != "diterapkan" or row.kering:
        raise HTTPException(status_code=404, detail="Audit perubahan yang terkirim tidak ditemukan")
    sebelum = agen_guardrail._muat(row.sebelum_json)
    if row.aksi in BALIK:
        jenis, nilai = BALIK[row.aksi], None
    elif row.aksi == "anggaran":
        jenis, nilai = "anggaran", _desimal(sebelum.get("anggaran"))
        if nilai is None:
            raise HTTPException(status_code=422, detail="Audit tidak menyimpan anggaran sebelumnya")
    elif row.aksi == "roas":
        jenis, nilai = "roas", _desimal(sebelum.get("roas_target"))
        if nilai is None:
            raise HTTPException(status_code=422, detail="Audit tidak menyimpan target ROAS sebelumnya")
    elif row.aksi == "bid":
        balik = (sebelum.get("balik") or [{}])[0]
        jenis, nilai = "bid", _desimal(balik.get("bid"))
        if not balik.get("kata") or nilai is None:
            raise HTTPException(status_code=422, detail="Audit tidak menyimpan bid sebelumnya")
    elif row.aksi in {"kata_kunci", "kata_negatif"}:
        # Inverse edits are already Shopee keyword actions (delete / restore / change_bid_price).
        jenis, nilai = "kata_kunci", None
        if not sebelum.get("balik"):
            raise HTTPException(status_code=422, detail="Audit tidak menyimpan kata kunci sebelumnya")
    else:
        raise HTTPException(status_code=422, detail="Aksi ini tidak bisa dikembalikan")
    if not row.kampanye_id or not str(row.kampanye_id).isdigit():
        raise HTTPException(status_code=422, detail="Audit tidak menyimpan kampanye")
    kata_balik = None
    if row.aksi in JENIS_KATA:
        mentah = sebelum.get("balik") if row.aksi != "bid" else None
        kata_balik = [KataPerubahan(**{k: item.get(k) for k in ("aksi", "kata", "bid", "tipe")}) for item in (mentah or [])] or None
    return await terapkan(
        session, akun,
        AksiIn(
            jenis=jenis, campaign_id=int(row.kampanye_id), nilai=nilai,
            kata=(sebelum.get("balik") or [{}])[0].get("kata") if row.aksi == "bid" else None,
            kata_kunci=kata_balik, alasan=body.alasan or "kembalikan",
            kering=body.kering, persetujuan=body.persetujuan, request_id=body.request_id,
        ),
        pengguna, kembalikan=True,
    )
