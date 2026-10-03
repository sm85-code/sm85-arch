"""Format file penghasilan & pencairan marketplace/iPaymu (spesifikasi 8.3, 10.3; tabel 14.4 baris 2.4–2.8).

Alur: file → mesin format (pencairan_format) → baris standar → pencocokan per order → simpan (draf: baris standar +
transaksi draf) → "Kirim ke laporan keuangan" (sumber kiriman "pencairan": transaksi resmi + order ditandai cair).
Service ini juga titik masuk job sinkronisasi marketplace_erp kelak (lihat INTEGRASI.md): baris standar yang sama.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import pencairan_format as pf
from tenants.bumi_lestari.modules.bumi_lestari.application.audit_core import catat_audit
from tenants.bumi_lestari.modules.bumi_lestari.application.kategori_core import (
    KATEGORI_BIAYA_MARKETPLACE,
    KATEGORI_PENJUALAN_MARKETPLACE,
    KATEGORI_PENJUALAN_WEB,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.pembayaran_services import (
    _batalkan_transaksi_ref,
    _tolak_bila_terkirim,
    _transaksi_otomatis,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.pencairan_adapter import adapter_untuk
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import OrderOut
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pencairan import (
    BacaHeaderOut,
    BarisStandarOut,
    FormatPenghasilanIn,
    FormatPenghasilanOut,
    KelompokOut,
    KolomPetaOut,
    MasalahOut,
    PencairanManualIn,
    PratinjauOut,
    UjiFormatOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.services import pastikan_bulan_terbuka
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import STATUS_DRAF, BlAkunKas, BlUser
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import (
    STATUS_BATAL,
    STATUS_CAIR_BELUM,
    STATUS_CAIR_CAIR,
    STATUS_RETUR,
    BlOrder,
    BlSaluran,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pencairan import (
    REF_PENCAIRAN,
    BlFormatPenghasilan,
    BlFormatPenghasilanKolom,
    BlPencairanBaris,
    BlPencairanUnggahan,
)

_NOL = Decimal("0")
DIBUKUKAN = ("cocok", "selisih", "penyesuaian")
ALASAN_RETUR_FILE = "Retur dari file pencairan"


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


async def _saluran(session: AsyncSession, saluran_id: str) -> BlSaluran:
    s = await session.get(BlSaluran, saluran_id)
    if s is None:
        raise _bad("Saluran tidak ditemukan", status.HTTP_404_NOT_FOUND)
    return s


# ---- format file penghasilan -----------------------------------------------------------------------------------


async def _kolom(session: AsyncSession, format_id: str) -> list[BlFormatPenghasilanKolom]:
    return list(
        (
            await session.execute(
                select(BlFormatPenghasilanKolom)
                .where(BlFormatPenghasilanKolom.format_id == format_id)
                .order_by(BlFormatPenghasilanKolom.urutan)
            )
        ).scalars()
    )


def konfig_dari(fmt: BlFormatPenghasilan, kolom: list[BlFormatPenghasilanKolom]) -> pf.KonfigFormat:
    return pf.KonfigFormat(
        kolom=[pf.KolomPeta(k.kolom_tujuan, k.kolom_sumber, k.operasi, k.nama_rincian) for k in kolom],
        jenis_file=fmt.jenis_file, nama_sheet=fmt.nama_sheet, baris_header=fmt.baris_header,
        baris_data_mulai=fmt.baris_data_mulai, format_tanggal=fmt.format_tanggal, pemisah_desimal=fmt.pemisah_desimal,
        pemisah_ribuan=fmt.pemisah_ribuan, aturan_tanda=fmt.aturan_tanda, aturan_jenis_baris=fmt.aturan_jenis_baris or {},
        satuan_baris=fmt.satuan_baris, aturan_abaikan=fmt.aturan_abaikan or {},
    )


async def format_out(session: AsyncSession, fmt: BlFormatPenghasilan) -> FormatPenghasilanOut:
    out = FormatPenghasilanOut.model_validate(fmt)
    out.kolom = [KolomPetaOut.model_validate(k) for k in await _kolom(session, fmt.id)]
    return out


async def list_format(session: AsyncSession, saluran_id: str | None = None) -> list[FormatPenghasilanOut]:
    stmt = select(BlFormatPenghasilan).order_by(BlFormatPenghasilan.saluran_id, BlFormatPenghasilan.versi.desc())
    if saluran_id:
        stmt = stmt.where(BlFormatPenghasilan.saluran_id == saluran_id)
    return [await format_out(session, f) for f in (await session.execute(stmt)).scalars()]


async def get_format(session: AsyncSession, format_id: str) -> BlFormatPenghasilan:
    fmt = await session.get(BlFormatPenghasilan, format_id)
    if fmt is None:
        raise _bad("Format file penghasilan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    return fmt


def _cek_payload(payload: FormatPenghasilanIn) -> None:
    konfig = pf.KonfigFormat(
        kolom=[pf.KolomPeta(k.kolom_tujuan, k.kolom_sumber, k.operasi, k.nama_rincian) for k in payload.kolom],
        satuan_baris=payload.satuan_baris, aturan_tanda=payload.aturan_tanda,
    )
    salah = pf.cek_konfig(konfig)
    if salah:
        raise _bad("; ".join(salah), 422)


def _isi_format(fmt: BlFormatPenghasilan, payload: FormatPenghasilanIn) -> None:
    for kolom in (
        "nama", "jenis_file", "nama_sheet", "baris_header", "baris_data_mulai", "format_tanggal", "pemisah_desimal",
        "pemisah_ribuan", "aturan_tanda", "aturan_jenis_baris", "satuan_baris", "aturan_abaikan", "catatan",
    ):
        setattr(fmt, kolom, getattr(payload, kolom))


async def _ganti_kolom(session: AsyncSession, fmt: BlFormatPenghasilan, kolom) -> None:
    for k in await _kolom(session, fmt.id):
        await session.delete(k)
    await session.flush()
    for i, k in enumerate(kolom):
        session.add(
            BlFormatPenghasilanKolom(
                format_id=fmt.id, kolom_tujuan=k.kolom_tujuan, kolom_sumber=k.kolom_sumber.strip(), operasi=k.operasi,
                nama_rincian=(k.nama_rincian or "").strip() or None, urutan=i,
            )
        )


async def _versi_berikut(session: AsyncSession, saluran_id: str) -> int:
    v = (
        await session.execute(select(func.max(BlFormatPenghasilan.versi)).where(BlFormatPenghasilan.saluran_id == saluran_id))
    ).scalar_one()
    return int(v or 0) + 1


async def create_format(session: AsyncSession, user: BlUser, payload: FormatPenghasilanIn) -> BlFormatPenghasilan:
    await _saluran(session, payload.saluran_id)
    _cek_payload(payload)
    fmt = BlFormatPenghasilan(
        saluran_id=payload.saluran_id, versi=await _versi_berikut(session, payload.saluran_id), status="draf",
        dibuat_oleh=user.id, aturan_jenis_baris={}, aturan_abaikan={},
    )
    _isi_format(fmt, payload)
    session.add(fmt)
    await session.flush()
    await _ganti_kolom(session, fmt, payload.kolom)
    await session.flush()
    return fmt


async def update_format(session: AsyncSession, format_id: str, payload: FormatPenghasilanIn) -> BlFormatPenghasilan:
    """Hanya versi draf yang bisa diubah; format aktif diubah lewat versi baru (10.3.4)."""
    fmt = await get_format(session, format_id)
    if fmt.status != "draf":
        raise _bad("Format aktif/arsip tidak bisa diubah; buat versi baru", status.HTTP_409_CONFLICT)
    if payload.saluran_id != fmt.saluran_id:
        raise _bad("Saluran format tidak bisa diganti")
    _cek_payload(payload)
    _isi_format(fmt, payload)
    await _ganti_kolom(session, fmt, payload.kolom)
    fmt.lulus_uji, fmt.hasil_uji = False, None  # pemetaan berubah: wajib uji ulang
    await session.flush()
    return fmt


async def versi_baru(session: AsyncSession, user: BlUser, format_id: str) -> BlFormatPenghasilan:
    asal = await get_format(session, format_id)
    baru = BlFormatPenghasilan(
        saluran_id=asal.saluran_id, versi=await _versi_berikut(session, asal.saluran_id), status="draf",
        dibuat_oleh=user.id, contoh_file=asal.contoh_file, contoh_nama=asal.contoh_nama,
    )
    for kolom in (
        "nama", "jenis_file", "nama_sheet", "baris_header", "baris_data_mulai", "format_tanggal", "pemisah_desimal",
        "pemisah_ribuan", "aturan_tanda", "aturan_jenis_baris", "satuan_baris", "aturan_abaikan", "catatan",
    ):
        setattr(baru, kolom, getattr(asal, kolom))
    session.add(baru)
    await session.flush()
    for k in await _kolom(session, asal.id):
        session.add(
            BlFormatPenghasilanKolom(
                format_id=baru.id, kolom_tujuan=k.kolom_tujuan, kolom_sumber=k.kolom_sumber, operasi=k.operasi,
                nama_rincian=k.nama_rincian, urutan=k.urutan,
            )
        )
    await session.flush()
    return baru


def baca_header(isi: bytes, nama_file: str, nama_sheet: str | None = None, baris_header: int | None = None) -> BacaHeaderOut:
    jenis = "csv" if nama_file.lower().endswith(".csv") else "xlsx"
    try:
        sheets, rows = pf.baca_sheet(isi, jenis, nama_sheet)
    except pf.FormatError as exc:
        raise _bad(str(exc), 422) from exc
    hdr = baris_header or pf.saran_baris_header(rows)
    kolom = [pf._teks(v) for v in rows[hdr - 1]] if len(rows) >= hdr else []
    contoh = [[pf._teks(v) for v in r] for r in rows[hdr:hdr + 5]]
    return BacaHeaderOut(
        sheets=sheets, nama_sheet=nama_sheet or (sheets[0] if jenis == "xlsx" and sheets else None), baris_header=hdr,
        kolom=[k for k in kolom if k], contoh=contoh, saran=pf.saran_pemetaan([k for k in kolom if k], pf.KAMUS_SARAN),
    )


def _jalankan(isi: bytes, konfig: pf.KonfigFormat) -> pf.HasilFormat:
    try:
        _sheets, rows = pf.baca_sheet(isi, konfig.jenis_file, konfig.nama_sheet)
        return pf.terapkan(rows, konfig)
    except pf.FormatError as exc:
        raise _bad(str(exc), 422) from exc


def _masalah_out(m: list[pf.Masalah]) -> list[MasalahOut]:
    return [MasalahOut(baris=x.baris, kolom=x.kolom, nilai=x.nilai, alasan=x.alasan) for x in m]


def _baris_out(b: pf.BarisStandar, kelompok: str = "cocok", **kw) -> BarisStandarOut:
    return BarisStandarOut(
        baris_file=b.baris_file, kode_pesanan=b.kode_pesanan, tanggal_cair=b.tanggal_cair, harga_jual=b.harga_jual,
        potongan_biaya=b.potongan_biaya, rincian_biaya=b.rincian_biaya, jumlah_cair=b.jumlah_cair,
        jenis_baris=b.jenis_baris, mode_catat=b.mode_catat, catatan=b.catatan, kelompok=kelompok, **kw,
    )


async def uji_format(session: AsyncSession, format_id: str, isi: bytes, nama_file: str) -> UjiFormatOut:
    """Uji format pada contoh file (10.3.3 langkah 5). Lulus = terbaca tanpa kolom hilang & minimal satu baris sah."""
    fmt = await get_format(session, format_id)
    hasil = _jalankan(isi, konfig_dari(fmt, await _kolom(session, fmt.id)))
    out = UjiFormatOut(
        lulus=bool(hasil.baris), jumlah_sah=len(hasil.baris), jumlah_masalah=len(hasil.masalah),
        total_cair=sum((b.jumlah_cair for b in hasil.baris), _NOL), neto=any(b.mode_catat == "neto" for b in hasil.baris),
        baris=[_baris_out(b) for b in hasil.baris[:50]], masalah=_masalah_out(hasil.masalah[:200]),
    )
    if fmt.status == "draf":
        fmt.contoh_file, fmt.contoh_nama, fmt.lulus_uji = isi, nama_file, out.lulus
        fmt.hasil_uji = {
            "lulus": out.lulus, "jumlah_sah": out.jumlah_sah, "jumlah_masalah": out.jumlah_masalah,
            "total_cair": str(out.total_cair), "neto": out.neto, "diuji_pada": datetime.now(timezone.utc).isoformat(),
        }
        await session.flush()
    return out


async def aktifkan_format(session: AsyncSession, user: BlUser, format_id: str) -> BlFormatPenghasilan:
    """Hanya satu versi aktif per saluran; wajib lulus uji. Versi lama bisa diaktifkan kembali (lulus uji tersimpan)."""
    fmt = await get_format(session, format_id)
    if not fmt.lulus_uji:
        raise _bad("Format belum lulus uji; uji dengan contoh file dulu", status.HTTP_409_CONFLICT)
    lama = (
        await session.execute(
            select(BlFormatPenghasilan).where(
                BlFormatPenghasilan.saluran_id == fmt.saluran_id, BlFormatPenghasilan.status == "aktif",
                BlFormatPenghasilan.id != fmt.id,
            )
        )
    ).scalars()
    for f in lama:
        f.status = "arsip"
    fmt.status, fmt.diaktifkan_oleh, fmt.diaktifkan_pada = "aktif", user.id, datetime.now(timezone.utc)
    await catat_audit(session, user.id, "aktifkan", "format_penghasilan", fmt.id, sesudah={"versi": fmt.versi})
    await session.flush()
    return fmt


async def _format_untuk(session: AsyncSession, saluran: BlSaluran, format_id: str | None) -> BlFormatPenghasilan | None:
    if format_id:
        fmt = await get_format(session, format_id)
        if fmt.saluran_id != saluran.id:
            raise _bad("Format ini milik saluran lain")
        return fmt
    return (
        await session.execute(
            select(BlFormatPenghasilan).where(
                BlFormatPenghasilan.saluran_id == saluran.id, BlFormatPenghasilan.status == "aktif"
            )
        )
    ).scalar_one_or_none()


async def baca_file_pencairan(
    session: AsyncSession, saluran: BlSaluran, isi: bytes, nama_file: str, format_id: str | None = None
) -> tuple[pf.HasilFormat, BlFormatPenghasilan | None]:
    fmt = await _format_untuk(session, saluran, format_id)
    if fmt is not None:
        return _jalankan(isi, konfig_dari(fmt, await _kolom(session, fmt.id))), fmt
    adapter = adapter_untuk(saluran.nama)
    if adapter is not None:
        return adapter(isi, nama_file), None
    raise _bad(
        f"Belum ada format file penghasilan aktif untuk {saluran.nama}. Buat & aktifkan di Data master > Format file penghasilan.",
        422,
    )


# ---- pencocokan ------------------------------------------------------------------------------------------------


@dataclass
class Cocok:
    baris: pf.BarisStandar
    kelompok: str
    kunci: str
    order_ids: list[str] = field(default_factory=list)
    perkiraan: Decimal | None = None
    selisih: Decimal = _NOL
    alasan: str = ""


def kunci_unik(saluran_id: str, b: pf.BarisStandar) -> str:
    return f"{saluran_id}|{b.kode_pesanan.strip().upper()}|{b.jenis_baris}|{b.tanggal_cair.isoformat()}|{b.jumlah_cair:.2f}"


async def _grup_order(session: AsyncSession, saluran_id: str, kode: list[str]) -> dict[str, list[BlOrder]]:
    if not kode:
        return {}
    rows = (
        await session.execute(
            select(BlOrder).where(
                BlOrder.saluran_id == saluran_id, func.upper(BlOrder.no_order).in_([k.strip().upper() for k in kode]),
                BlOrder.status != STATUS_BATAL,
            ).order_by(BlOrder.created_at)
        )
    ).scalars()
    grup: dict[str, list[BlOrder]] = {}
    for o in rows:
        grup.setdefault(o.no_order.strip().upper(), []).append(o)
    return grup


def _perkiraan(orders: list[BlOrder]) -> Decimal:
    return sum((OrderOut.model_validate(o).total_penjualan - Decimal(o.potongan_marketplace) for o in orders), _NOL)


async def cocokkan(session: AsyncSession, saluran: BlSaluran, baris: list[pf.BarisStandar]) -> list[Cocok]:
    """Kelompok pratinjau 8.3.3: cocok, selisih, tidak cocok, duplikat (sudah pernah diunggah), penyesuaian/retur."""
    kunci = [kunci_unik(saluran.id, b) for b in baris]
    ada = set(
        (
            await session.execute(
                select(BlPencairanBaris.kunci_unik).where(
                    BlPencairanBaris.kunci_unik.in_(kunci), BlPencairanBaris.dibatalkan.is_(False),
                    BlPencairanBaris.status_cocok != "tidak_cocok",  # baris "menunggu" boleh dicocokkan ulang
                )
            )
        ).scalars()
    )
    grup = await _grup_order(session, saluran.id, [b.kode_pesanan for b in baris])
    hasil: list[Cocok] = []
    dilihat: set[str] = set()
    for b, k in zip(baris, kunci):
        if k in ada or k in dilihat:
            hasil.append(Cocok(b, "duplikat", k, alasan="sudah pernah diunggah"))
            continue
        dilihat.add(k)
        orders = grup.get(b.kode_pesanan.strip().upper(), [])
        ids = [o.id for o in orders]
        if b.jenis_baris != "pesanan":
            if orders:
                hasil.append(Cocok(b, "penyesuaian", k, ids))
            else:
                hasil.append(Cocok(b, "tidak_cocok", k, alasan="kode pesanan tidak ada di aplikasi"))
            continue
        if not orders:
            hasil.append(Cocok(b, "tidak_cocok", k, alasan="kode pesanan tidak ada di aplikasi"))
        elif any(o.status == STATUS_RETUR for o in orders):
            hasil.append(Cocok(b, "tidak_cocok", k, ids, alasan="order sudah ditandai retur"))
        elif any(o.status not in ("dikirim", "selesai") for o in orders):
            hasil.append(Cocok(b, "tidak_cocok", k, ids, alasan="order belum dikirim"))
        elif any(o.status_cair == STATUS_CAIR_CAIR for o in orders):
            hasil.append(Cocok(b, "duplikat", k, ids, alasan="order sudah cair"))
        else:
            harap = _perkiraan(orders)
            beda = b.jumlah_cair - harap
            hasil.append(Cocok(b, "cocok" if beda == 0 else "selisih", k, ids, harap, beda))
    return hasil


def _ringkas(saluran: BlSaluran, fmt, nama_file: str, hasil: pf.HasilFormat, cocok: list[Cocok]) -> PratinjauOut:
    kelompok = {k: KelompokOut() for k in ("cocok", "selisih", "tidak_cocok", "duplikat", "penyesuaian")}
    for c in cocok:
        kelompok[c.kelompok].jumlah += 1
        kelompok[c.kelompok].total_cair += c.baris.jumlah_cair
    simpan = [c for c in cocok if c.kelompok != "duplikat"]
    return PratinjauOut(
        saluran_id=saluran.id, format_id=fmt.id if fmt else None, format_versi=fmt.versi if fmt else None,
        nama_file=nama_file, kelompok=kelompok, bermasalah=len(hasil.masalah),
        baris=[
            _baris_out(c.baris, c.kelompok, order_id=c.order_ids[0] if c.order_ids else None, perkiraan_cair=c.perkiraan,
                       selisih=c.selisih, alasan=c.alasan)
            for c in cocok
        ],
        masalah=_masalah_out(hasil.masalah), jumlah_disimpan=len(simpan),
        total_dibukukan=sum((c.baris.jumlah_cair for c in simpan if c.kelompok in DIBUKUKAN), _NOL),
        neto=any(c.baris.mode_catat == "neto" for c in simpan),
    )


async def pratinjau(
    session: AsyncSession, saluran_id: str, isi: bytes, nama_file: str, format_id: str | None = None
) -> PratinjauOut:
    saluran = await _saluran(session, saluran_id)
    hasil, fmt = await baca_file_pencairan(session, saluran, isi, nama_file, format_id)
    return _ringkas(saluran, fmt, nama_file, hasil, await cocokkan(session, saluran, hasil.baris))


# ---- simpan & bukukan ------------------------------------------------------------------------------------------


async def _akun_saluran(session: AsyncSession, saluran: BlSaluran) -> BlAkunKas:
    akun = await session.get(BlAkunKas, saluran.akun_id) if saluran.akun_id else None
    if akun is None:
        raise _bad(f"Saluran {saluran.nama} belum dihubungkan ke akun saldo (Data master > Saluran)")
    return akun


async def _bukukan(session: AsyncSession, user: BlUser, unggahan: BlPencairanUnggahan, saluran: BlSaluran) -> None:
    """Satu set transaksi draf per tanggal cair (AB-MP-1): Penjualan bruto + Biaya marketplace per jenis potongan;
    baris tanpa rincian dicatat neto; baris negatif (retur/penyesuaian) mengurangi penjualan. Hasil bersih = jumlah cair."""
    akun = await _akun_saluran(session, saluran)
    kat_jual = KATEGORI_PENJUALAN_WEB if saluran.jenis == "web" else KATEGORI_PENJUALAN_MARKETPLACE
    rows = list(
        (
            await session.execute(
                select(BlPencairanBaris).where(
                    BlPencairanBaris.unggahan_id == unggahan.id, BlPencairanBaris.dibatalkan.is_(False),
                    BlPencairanBaris.status_cocok.in_(DIBUKUKAN),
                ).order_by(BlPencairanBaris.tanggal_cair)
            )
        ).scalars()
    )
    per_tanggal: dict = {}
    for r in rows:
        per_tanggal.setdefault(r.tanggal_cair, []).append(r)
    total = hj_total = pot_total = _NOL
    for tgl, grup in per_tanggal.items():
        jual = kurang = _NOL
        biaya: dict[str, Decimal] = {}
        neto = 0
        for r in grup:
            cair = Decimal(r.jumlah_cair)
            total += cair
            if cair < 0 or r.mode_catat == "neto" or r.harga_jual is None:
                neto += r.mode_catat == "neto" and cair >= 0
                if cair < 0:
                    kurang += -cair
                else:
                    jual += cair
                continue
            hj = Decimal(r.harga_jual)
            jual += hj
            hj_total += hj
            for nama, nilai in (r.rincian_biaya or {}).items():
                biaya[nama] = biaya.get(nama, _NOL) + Decimal(str(nilai))
                pot_total += Decimal(str(nilai))
            beda = hj - sum((Decimal(str(v)) for v in (r.rincian_biaya or {}).values()), _NOL) - cair
            if beda > 0:
                biaya["Selisih file"] = biaya.get("Selisih file", _NOL) + beda
            elif beda < 0:
                jual += -beda
        ket = f"Pencairan {saluran.nama} {tgl:%d-%m-%Y} ({len(grup)} baris)"
        if neto:
            ket += f" — {neto} baris neto, rincian biaya tidak ada"
        umum = dict(tanggal=tgl, akun=akun, ref_jenis=REF_PENCAIRAN, ref_id=unggahan.id, status_kirim=STATUS_DRAF)
        if jual:
            await _transaksi_otomatis(session, user, kategori=kat_jual, jenis="masuk", jumlah=jual, keterangan=ket, **umum)
        if kurang:
            await _transaksi_otomatis(
                session, user, kategori=kat_jual, jenis="keluar", jumlah=kurang,
                keterangan=f"Retur/penyesuaian pencairan {saluran.nama} {tgl:%d-%m-%Y}", **umum,
            )
        for nama, nilai in sorted(biaya.items()):
            if nilai > 0:
                await _transaksi_otomatis(
                    session, user, kategori=KATEGORI_BIAYA_MARKETPLACE, jenis="keluar", jumlah=nilai,
                    keterangan=f"Biaya marketplace {saluran.nama}: {nama} ({tgl:%d-%m-%Y})", **umum,
                )
    unggahan.total, unggahan.total_harga_jual, unggahan.total_potongan = total, hj_total, pot_total
    if rows:
        unggahan.tanggal = max(r.tanggal_cair for r in rows)
        unggahan.periode_dari = min(r.tanggal_cair for r in rows)
        unggahan.periode_sampai = unggahan.tanggal


async def simpan_baris(
    session: AsyncSession, user: BlUser, saluran: BlSaluran, cocok: list[Cocok], *, nama_file: str,
    fmt: BlFormatPenghasilan | None = None, sumber_sistem: str | None = None, sumber_ref: str | None = None,
) -> BlPencairanUnggahan:
    """Simpan baris standar baru sebagai satu unggahan draf + transaksi draf. Dipakai unggah file, entri manual iPaymu,
    dan (kelak) job sinkronisasi -- semuanya lewat tabel standar yang sama (KP-MP-4)."""
    baru = [c for c in cocok if c.kelompok != "duplikat"]
    if not baru:
        raise _bad("Tidak ada baris baru untuk disimpan (semua sudah pernah diunggah)", status.HTTP_409_CONFLICT)
    await _akun_saluran(session, saluran)
    for c in baru:
        if c.kelompok in DIBUKUKAN:
            await pastikan_bulan_terbuka(session, c.baris.tanggal_cair)
    # Baris "menunggu" (tidak cocok) lama dengan kunci sama digantikan baris baru.
    lama = (
        await session.execute(
            select(BlPencairanBaris).where(
                BlPencairanBaris.kunci_unik.in_([c.kunci for c in baru]), BlPencairanBaris.dibatalkan.is_(False)
            )
        )
    ).scalars()
    for r in lama:
        r.dibatalkan, r.kunci_unik = True, f"{r.kunci_unik}|diganti|{r.id}"
    await session.flush()
    tanggal = max(c.baris.tanggal_cair for c in baru)
    unggahan = BlPencairanUnggahan(
        saluran_id=saluran.id, format_id=fmt.id if fmt else None, format_versi=fmt.versi if fmt else None,
        nama_file=nama_file, tanggal=tanggal, jumlah_baris=len(baru), diunggah_oleh=user.id, status_kirim=STATUS_DRAF,
        sumber_sistem=sumber_sistem, sumber_ref=sumber_ref,
    )
    session.add(unggahan)
    await session.flush()
    for c in baru:
        b = c.baris
        session.add(
            BlPencairanBaris(
                unggahan_id=unggahan.id, saluran_id=saluran.id, kode_pesanan=b.kode_pesanan, tanggal_cair=b.tanggal_cair,
                harga_jual=b.harga_jual, potongan_biaya=b.potongan_biaya,
                rincian_biaya={k: str(v) for k, v in b.rincian_biaya.items()}, jumlah_cair=b.jumlah_cair,
                jenis_baris=b.jenis_baris, mode_catat=b.mode_catat, order_id=c.order_ids[0] if c.order_ids else None,
                status_cocok="cocok" if c.kelompok == "penyesuaian" else c.kelompok, selisih=c.selisih,
                kunci_unik=c.kunci, baris_file=b.baris_file, data_asli=b.data_asli or None,
                masalah=(b.catatan + ([c.alasan] if c.alasan else [])) or None,
            )
        )
    await session.flush()
    await _bukukan(session, user, unggahan, saluran)
    await catat_audit(
        session, user.id, "simpan", "pencairan", unggahan.id,
        sesudah={"saluran": saluran.nama, "baris": len(baru), "total": unggahan.total, "file": nama_file},
    )
    await session.flush()
    return unggahan


async def simpan(
    session: AsyncSession, user: BlUser, saluran_id: str, isi: bytes, nama_file: str, format_id: str | None = None
) -> BlPencairanUnggahan:
    saluran = await _saluran(session, saluran_id)
    hasil, fmt = await baca_file_pencairan(session, saluran, isi, nama_file, format_id)
    return await simpan_baris(session, user, saluran, await cocokkan(session, saluran, hasil.baris), nama_file=nama_file, fmt=fmt)


async def entri_manual(session: AsyncSession, user: BlUser, payload: PencairanManualIn) -> BlPencairanUnggahan:
    """Khusus Toko web/iPaymu (AB-MP-9): satu order per entri, dicatat bruto; harus cocok dengan order Toko web."""
    saluran = await _saluran(session, payload.saluran_id)
    if saluran.jenis != "web":
        raise _bad("Entri manual hanya untuk Toko web (iPaymu); pencairan marketplace lewat unggah file")
    harga, pot = payload.harga_jual, payload.potongan
    cair = payload.jumlah_cair if payload.jumlah_cair is not None else harga - pot
    b = pf.BarisStandar(
        baris_file=None, kode_pesanan=payload.kode_pesanan.strip(), tanggal_cair=payload.tanggal_cair, jumlah_cair=cair,
        harga_jual=harga, potongan_biaya=pot, rincian_biaya={"Potongan iPaymu": pot} if pot else {},
        catatan=[] if harga - pot == cair else [f"harga jual − potongan ({harga - pot}) ≠ jumlah cair ({cair})"],
    )
    [c] = await cocokkan(session, saluran, [b])
    if c.kelompok == "duplikat":
        raise _bad(f"Pencairan order {b.kode_pesanan} sudah tercatat ({c.alasan})", status.HTTP_409_CONFLICT)
    if c.kelompok == "tidak_cocok":
        raise _bad(f"Order Toko web {b.kode_pesanan} tidak bisa dicocokkan: {c.alasan}")
    return await simpan_baris(session, user, saluran, [c], nama_file="Entri manual iPaymu")


async def list_unggahan(session: AsyncSession, saluran_id: str | None = None) -> list[BlPencairanUnggahan]:
    stmt = select(BlPencairanUnggahan).order_by(BlPencairanUnggahan.created_at.desc())
    if saluran_id:
        stmt = stmt.where(BlPencairanUnggahan.saluran_id == saluran_id)
    return list((await session.execute(stmt)).scalars())


async def get_unggahan(session: AsyncSession, unggahan_id: str) -> BlPencairanUnggahan:
    u = await session.get(BlPencairanUnggahan, unggahan_id)
    if u is None:
        raise _bad("Unggahan pencairan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    return u


async def baris_unggahan(session: AsyncSession, unggahan_id: str) -> list[BlPencairanBaris]:
    return list(
        (
            await session.execute(
                select(BlPencairanBaris).where(BlPencairanBaris.unggahan_id == unggahan_id)
                .order_by(BlPencairanBaris.baris_file, BlPencairanBaris.kode_pesanan)
            )
        ).scalars()
    )


async def batal_unggahan(session: AsyncSession, user: BlUser, unggahan_id: str, alasan: str) -> BlPencairanUnggahan:
    """Batalkan unggahan (hanya dari halaman pencairan). Yang sudah dikirim: batalkan kirimannya dulu (AB-KR-4)."""
    u = await get_unggahan(session, unggahan_id)
    if u.dibatalkan:
        raise _bad("Unggahan ini sudah dibatalkan", status.HTTP_409_CONFLICT)
    _tolak_bila_terkirim(u)
    await _batalkan_transaksi_ref(session, REF_PENCAIRAN, u.id, alasan)
    for r in await baris_unggahan(session, u.id):
        if not r.dibatalkan:
            r.dibatalkan, r.kunci_unik = True, f"{r.kunci_unik}|batal|{r.id}"
    u.dibatalkan, u.dibatalkan_at, u.alasan_batal = True, datetime.now(timezone.utc), alasan.strip()
    await catat_audit(session, user.id, "batal", "pencairan", u.id, sebelum={"total": u.total}, alasan=alasan.strip())
    await session.flush()
    return u


async def hubungkan(session: AsyncSession, user: BlUser, baris_id: str, order_id: str) -> BlPencairanBaris:
    """Hubungkan baris tidak cocok (mis. salah ketik nomor) ke order yang benar, selama unggahannya masih draf."""
    r = await session.get(BlPencairanBaris, baris_id)
    if r is None or r.dibatalkan:
        raise _bad("Baris pencairan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    u = await get_unggahan(session, r.unggahan_id)
    if r.status_cocok != "tidak_cocok":
        raise _bad("Hanya baris tidak cocok yang bisa dihubungkan", status.HTTP_409_CONFLICT)
    if u.dibatalkan or u.status_kirim != STATUS_DRAF:
        raise _bad("Unggahan sudah dikirim/dibatalkan; unggah ulang setelah order diperbaiki", status.HTTP_409_CONFLICT)
    order = await session.get(BlOrder, order_id)
    if order is None or order.saluran_id != r.saluran_id or order.status == STATUS_BATAL:
        raise _bad("Order tidak ditemukan di saluran ini")
    grup = (await _grup_order(session, r.saluran_id, [order.no_order])).get(order.no_order.strip().upper(), [order])
    if r.jenis_baris == "pesanan":
        if any(o.status not in ("dikirim", "selesai") for o in grup):
            raise _bad("Order belum dikirim")
        if any(o.status_cair == STATUS_CAIR_CAIR for o in grup):
            raise _bad("Order sudah cair", status.HTTP_409_CONFLICT)
        await pastikan_bulan_terbuka(session, r.tanggal_cair)
        r.selisih = Decimal(r.jumlah_cair) - _perkiraan(grup)
        r.status_cocok = "cocok" if r.selisih == 0 else "selisih"
    else:
        r.status_cocok = "cocok"
    r.order_id = order.id
    r.masalah = [*(r.masalah or []), f"dihubungkan manual ke order {order.no_order or order.id}"]
    saluran = await _saluran(session, r.saluran_id)
    await _batalkan_transaksi_ref(session, REF_PENCAIRAN, u.id, "dibukukan ulang setelah baris dihubungkan")
    await session.flush()
    await _bukukan(session, user, u, saluran)
    await catat_audit(session, user.id, "hubungkan", "pencairan_baris", r.id, sesudah={"order_id": order.id})
    await session.flush()
    return r


# ---- kaitan dengan kiriman (dipanggil kiriman_services) -------------------------------------------------------


async def _baris_dibukukan(session: AsyncSession, unggahan_id: str) -> list[BlPencairanBaris]:
    return [
        r for r in await baris_unggahan(session, unggahan_id)
        if not r.dibatalkan and r.status_cocok in DIBUKUKAN and r.order_id
    ]


async def setelah_kirim(session: AsyncSession, unggahan: BlPencairanUnggahan) -> None:
    """Saat dikirim: order cocok ditandai cair + potongan aktual; baris retur menandai order retur (AB-BC-4)."""
    for r in await _baris_dibukukan(session, unggahan.id):
        order = await session.get(BlOrder, r.order_id)
        grup = (await _grup_order(session, r.saluran_id, [order.no_order])).get(order.no_order.strip().upper(), [order])
        if r.jenis_baris == "pesanan":
            jual = [OrderOut.model_validate(o).total_penjualan for o in grup]
            total_jual = sum(jual, _NOL)
            potongan = (Decimal(r.potongan_biaya) if r.potongan_biaya is not None else total_jual - Decimal(r.jumlah_cair))
            sisa = potongan
            for i, (o, j) in enumerate(zip(grup, jual)):
                bagian = sisa if i == len(grup) - 1 else (potongan * j / total_jual).quantize(Decimal("0.01")) if total_jual else _NOL
                sisa -= bagian
                o.status_cair, o.tgl_cair, o.pencairan_baris_id, o.potongan_aktual = STATUS_CAIR_CAIR, r.tanggal_cair, r.id, bagian
        elif r.jenis_baris == "retur":
            r.status_order_sebelum = grup[0].status
            for o in grup:
                if o.status != STATUS_RETUR:
                    o.status, o.tgl_retur, o.alasan_retur = STATUS_RETUR, r.tanggal_cair, ALASAN_RETUR_FILE


async def setelah_batal_kirim(session: AsyncSession, unggahan: BlPencairanUnggahan) -> None:
    """Kiriman dibatalkan: order kembali belum cair; retur dari file dipulihkan ke status sebelumnya (KP-MP-7)."""
    for r in await _baris_dibukukan(session, unggahan.id):
        if r.jenis_baris == "pesanan":
            for o in (await session.execute(select(BlOrder).where(BlOrder.pencairan_baris_id == r.id))).scalars():
                o.status_cair, o.tgl_cair, o.pencairan_baris_id, o.potongan_aktual = STATUS_CAIR_BELUM, None, None, None
        elif r.jenis_baris == "retur" and r.status_order_sebelum:
            order = await session.get(BlOrder, r.order_id)
            grup = (await _grup_order(session, r.saluran_id, [order.no_order])).get(order.no_order.strip().upper(), [order])
            for o in grup:
                if o.status == STATUS_RETUR and o.alasan_retur == ALASAN_RETUR_FILE:
                    o.status, o.tgl_retur, o.alasan_retur = r.status_order_sebelum, None, None
            r.status_order_sebelum = None
