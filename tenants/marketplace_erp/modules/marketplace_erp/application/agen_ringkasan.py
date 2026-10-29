"""Compact read-only Shopee ads snapshot for one shop.

The ads bot truncates long tool results, so this keeps a hard cap on campaigns,
keywords and products. It never calls a write endpoint.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_iklan

WIB = timezone(timedelta(hours=7))
MAKS_KAMPANYE = 25
MAKS_KATA = 8
MAKS_PRODUK = 30
MAKS_NAMA = 80
AKSI_BACA = frozenset({
    "saldo", "toggle", "tarif", "daftar_kampanye", "pengaturan_kampanye", "kinerja_harian", "kinerja_cpc_jam",
})


def angka(nilai):
    """JSON number. Non-numeric and non-finite values become 0."""
    try:
        n = float(nilai)
    except (TypeError, ValueError):
        return 0
    if n != n or n in (float("inf"), float("-inf")):
        return 0
    if abs(n) < 1e15 and n == int(n):
        return int(n)
    return round(n, 2)


def _potong(teks, batas=MAKS_NAMA):
    return str(teks or "")[:batas]


def ringkas_toggle(data: dict | None) -> dict:
    resp = data.get("response") if isinstance(data, dict) else None
    saklar: dict[str, bool] = {}
    if isinstance(resp, dict):
        for kunci, nilai in resp.items():
            if isinstance(nilai, bool):
                saklar[str(kunci)[:40]] = nilai
            elif isinstance(nilai, dict):
                for anak, isi in nilai.items():
                    if isinstance(isi, bool):
                        saklar[f"{kunci}.{anak}"[:60]] = isi
    return {
        "campaign_surge": saklar.get("campaign_surge"),
        "auto_top_up": saklar.get("auto_top_up"),
        "saklar": saklar,
    }


def ringkas_tarif(data) -> dict:
    resp = data.get("response") if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        return {}
    keluar = {}
    for kunci, nilai in resp.items():
        if isinstance(nilai, bool) or not isinstance(nilai, (int, float)):
            continue
        if nilai != nilai or nilai in (float("inf"), float("-inf")):
            continue
        keluar[str(kunci)[:40]] = angka(nilai)
        if len(keluar) >= 8:
            break
    return keluar


def _kata(manual: dict) -> tuple[list[dict], int]:
    mentah = [
        {
            "kata": _potong(k.get("keyword")),
            "status": k.get("status"),
            "tipe": k.get("match_type"),
            "bid": angka(k.get("bid_price_per_click")),
        }
        for k in (manual.get("selected_keywords") or [])
        if isinstance(k, dict) and k.get("keyword") and k.get("status") != "deleted"
    ]
    return mentah[:MAKS_KATA], max(0, len(mentah) - MAKS_KATA)


def _negatif(manual: dict) -> list[str]:
    mentah = manual.get("negative_keyword_list") or manual.get("negative_keywords") or []
    if isinstance(mentah, str):
        mentah = [mentah]
    keluar = []
    for item in mentah:
        if isinstance(item, str) and item.strip():
            keluar.append(item.strip()[:MAKS_NAMA])
        elif isinstance(item, dict) and item.get("keyword") and item.get("status") != "deleted":
            keluar.append(str(item["keyword"]).strip()[:MAKS_NAMA])
        if len(keluar) >= MAKS_KATA:
            break
    return keluar


def _kinerja(baris: dict | None) -> dict | None:
    if not baris:
        return None
    ada = False
    jumlah = {"impression": 0, "clicks": 0, "expense": 0.0, "direct_order": 0, "direct_gmv": 0.0}
    for hari in baris.get("metrics_list") or []:
        if not isinstance(hari, dict):
            continue
        ada = True
        jumlah["impression"] += int(hari.get("impression") or 0)
        jumlah["clicks"] += int(hari.get("clicks") or 0)
        jumlah["expense"] += float(hari.get("expense") or 0)
        jumlah["direct_order"] += int(hari.get("direct_order") or 0)
        jumlah["direct_gmv"] += float(hari.get("direct_gmv") or 0)
    if not ada:
        return None
    biaya = jumlah["expense"]
    return {
        "impression": jumlah["impression"],
        "clicks": jumlah["clicks"],
        "expense": angka(biaya),
        "direct_order": jumlah["direct_order"],
        "direct_gmv": angka(jumlah["direct_gmv"]),
        "roas": angka(jumlah["direct_gmv"] / biaya) if biaya else None,
    }


def ringkas_kampanye(pengaturan: dict, kinerja: dict | None) -> tuple[dict, int]:
    info = pengaturan.get("common_info") or {}
    manual = pengaturan.get("manual_bidding_info") or {}
    auto = pengaturan.get("auto_bidding_info") or {}
    kata, sisa = _kata(manual)
    return {
        "campaign_id": str(pengaturan.get("campaign_id")),
        "nama": _potong(info.get("ad_name") or f"Kampanye {pengaturan.get('campaign_id')}"),
        "jenis": info.get("ad_type"),
        "status": info.get("campaign_status"),
        "bidding": info.get("bidding_method"),
        "anggaran": angka(info.get("campaign_budget")) if info.get("campaign_budget") not in (None, "") else None,
        "roas_target": angka(auto.get("roas_target")) if auto.get("roas_target") not in (None, "") else None,
        "item_id": [str(i) for i in (info.get("item_id_list") or [])][:MAKS_PRODUK],
        "kata_kunci": kata,
        "kata_negatif": _negatif(manual),
        "kinerja": _kinerja(kinerja),
    }, sisa


def ringkas_jam(data) -> list[dict]:
    if isinstance(data, list):
        response = data
    else:
        response = data.get("response") if isinstance(data, dict) else None
    baris = []
    if isinstance(response, list):
        for row in response:
            if not isinstance(row, dict):
                continue
            if type(row.get("hour")) is int:
                baris.append(row)
            else:
                for kamp in row.get("campaign_list") or []:
                    if isinstance(kamp, dict):
                        baris.extend(m for m in (kamp.get("metrics_list") or []) if isinstance(m, dict))
    elif isinstance(response, dict):
        baris = [m for m in (response.get("metrics_list") or []) if isinstance(m, dict)]
    jumlah: dict[int, dict] = {}
    for row in baris:
        jam = row.get("hour")
        if type(jam) is not int or not 0 <= jam <= 23:
            continue
        slot = jumlah.setdefault(jam, {"hour": jam, "impression": 0, "clicks": 0, "expense": 0.0, "broad_order": 0, "broad_gmv": 0.0})
        slot["impression"] += int(row.get("impression") or 0)
        slot["clicks"] += int(row.get("clicks") or 0)
        slot["expense"] += float(row.get("expense") or 0)
        slot["broad_order"] += int(row.get("broad_order") or 0)
        slot["broad_gmv"] += float(row.get("broad_gmv") or 0)
    return [
        {**slot, "expense": angka(slot["expense"]), "broad_gmv": angka(slot["broad_gmv"])}
        for _, slot in sorted(jumlah.items())
    ]


def _peta_kampanye(data) -> list[dict]:
    resp = data.get("response") if isinstance(data, dict) else None
    if isinstance(resp, dict):
        daftar = resp.get("campaign_list") or []
    elif isinstance(resp, list):
        daftar = [c for grup in resp if isinstance(grup, dict) for c in (grup.get("campaign_list") or [])]
    else:
        daftar = []
    return [c for c in daftar if isinstance(c, dict)]


def _peta_kinerja(data) -> dict[str, dict]:
    return {str(c["campaign_id"]): c for c in _peta_kampanye(data) if c.get("campaign_id") is not None}


def susun(*, kampanye_mentah, kinerja, saldo, toggle, tarif, jam, produk, catatan, ada_lagi) -> dict:
    mentah = [c for c in kampanye_mentah if isinstance(c, dict)]
    dipilih = mentah[:MAKS_KAMPANYE]
    sisa_kata = 0
    kampanye = []
    urutan_item: list[str] = []
    for row in dipilih:
        cid = str(row.get("campaign_id"))
        ringkas, sisa = ringkas_kampanye(row, kinerja.get(cid))
        sisa_kata += sisa
        kampanye.append(ringkas)
        urutan_item.extend(ringkas["item_id"])
    unik = list(dict.fromkeys(urutan_item))
    daftar_produk = []
    for item_id in unik[:MAKS_PRODUK]:
        info = produk.get(item_id) or {}
        nama = info.get("nama")
        daftar_produk.append({"item_id": item_id, "nama": _potong(nama) if nama else None, "harga": info.get("harga")})
    return {
        "saldo": saldo,
        "pengaturan_toko": ringkas_toggle(toggle),
        "tarif": ringkas_tarif(tarif),
        "kampanye": kampanye,
        "produk": daftar_produk,
        "performa_jam": ringkas_jam(jam),
        "dipotong": {"kampanye": bool(ada_lagi or len(mentah) > MAKS_KAMPANYE), "kata_kunci": sisa_kata, "produk": len(unik) > MAKS_PRODUK},
        "catatan": catatan,
        "api_usang": sorted(shopee_iklan.USANG),
        "hari": 7,
    }


async def _ambil(session, akun, aksi: str, params: dict | None = None) -> dict:
    try:
        data = await shopee_iklan.panggil(session, akun, aksi, params=params)
    except HTTPException as exc:
        return {"_gagal": _potong(exc.detail, 180)}
    return data if isinstance(data, dict) else {"response": data}


def _saldo(data: dict):
    resp = data.get("response") if isinstance(data, dict) else None
    if not isinstance(resp, dict) or resp.get("total_balance") is None:
        return None
    return angka(resp.get("total_balance"))


async def _produk_lokal(session, akun_id: str, item_ids: list[str]) -> dict:
    if session is None or not item_ids:
        return {}
    from sqlalchemy import select

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee

    rows = (
        await session.execute(
            select(KatalogShopee).where(KatalogShopee.akun_id == akun_id, KatalogShopee.item_id.in_(item_ids[:MAKS_PRODUK]))
        )
    ).scalars()
    return {
        r.item_id: {"nama": r.nama, "harga": angka(r.harga_min) if r.harga_min is not None else None}
        for r in rows
    }


async def ringkasan_toko(session, akun) -> dict:
    """One shop: balance, toggles (campaign surge, auto top-up), campaigns, keywords, products, today's hourly ads."""
    if getattr(akun, "platform", None) != "shopee":
        raise HTTPException(status_code=409, detail="API iklan ini khusus toko Shopee.")
    catatan: list[str] = []
    saldo_raw = await _ambil(session, akun, "saldo")
    toggle = await _ambil(session, akun, "toggle")
    tarif = await _ambil(session, akun, "tarif")
    daftar = await _ambil(session, akun, "daftar_kampanye", {"ad_type": "all", "offset": 0, "limit": 100})
    for nama, data in (("saldo", saldo_raw), ("toggle", toggle), ("tarif", tarif), ("daftar_kampanye", daftar)):
        if isinstance(data, dict) and data.get("_gagal"):
            catatan.append(f"{nama}: {data['_gagal']}")
    resp = daftar.get("response") if isinstance(daftar.get("response"), dict) else {}
    ids = [int(c["campaign_id"]) for c in (resp.get("campaign_list") or []) if isinstance(c, dict) and c.get("campaign_id")]
    ada_lagi = bool(resp.get("has_next_page")) or len(ids) > MAKS_KAMPANYE
    ids = ids[:MAKS_KAMPANYE]
    sampai = datetime.now(WIB).date()
    dari = sampai - timedelta(days=6)
    pengaturan: dict = {"response": {"campaign_list": []}}
    kinerja: dict = {"response": []}
    jam: dict = {"response": []}
    if ids:
        daftar_id = ",".join(str(i) for i in ids)
        pengaturan = await _ambil(session, akun, "pengaturan_kampanye", {"info_type_list": "1,2,3", "campaign_id_list": daftar_id})
        kinerja = await _ambil(
            session, akun, "kinerja_harian",
            {"start_date": dari.strftime("%d-%m-%Y"), "end_date": sampai.strftime("%d-%m-%Y"), "campaign_id_list": daftar_id},
        )
        if pengaturan.get("_gagal"):
            catatan.append(f"pengaturan_kampanye: {pengaturan['_gagal']}")
        if kinerja.get("_gagal"):
            catatan.append(f"kinerja_harian: {kinerja['_gagal']}")
    jam = await _ambil(session, akun, "kinerja_cpc_jam", {"performance_date": sampai.strftime("%d-%m-%Y")})
    if jam.get("_gagal"):
        catatan.append(f"kinerja_cpc_jam: {jam['_gagal']}")
    mentah = _peta_kampanye(pengaturan)
    item_ids = list(dict.fromkeys(
        str(i) for row in mentah[:MAKS_KAMPANYE] for i in ((row.get("common_info") or {}).get("item_id_list") or [])
    ))
    produk = await _produk_lokal(session, akun.id, item_ids)
    hasil = susun(
        kampanye_mentah=mentah, kinerja=_peta_kinerja(kinerja), saldo=_saldo(saldo_raw),
        toggle=toggle if not toggle.get("_gagal") else {}, tarif=tarif if not tarif.get("_gagal") else {},
        jam=jam if not jam.get("_gagal") else {}, produk=produk, catatan=catatan, ada_lagi=ada_lagi,
    )
    hasil["akun_id"] = akun.id
    hasil["nama_toko"] = _potong(getattr(akun, "nama_toko", ""))
    hasil["request_id"] = saldo_raw.get("request_id") if not saldo_raw.get("_gagal") else None
    return hasil
