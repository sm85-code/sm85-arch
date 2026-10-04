"""Tarik pencairan Shopee dari marketplace_erp ke draf pencairan Bumi Lestari.

Hanya saluran yang punya akun_erp_id. Toko yang tidak dipasangkan tidak ikut.
Potongan adalah jumlah seluruh komponen biaya escrow, termasuk selisih ongkir.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.pencairan_format import BarisStandar
from tenants.bumi_lestari.modules.bumi_lestari.application.pencairan_services import Cocok, kunci_unik, simpan_baris
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlSaluran
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace, SettlementPesanan

SUMBER = "marketplace_erp"
# Nama toko yang memang masuk laporan Bumi Lestari. Yang lain tidak disarankan.
MASUK = {
    "azfa furniture official",
    "bumi lestari indonesia",
    "bumi tani ind",
    "cahaya langit ind",
    "majapahit store ind",
    "restu bumi ind",
}
BUKAN_BIAYA = {
    "order_original_price", "original_price", "escrow_amount", "escrow_amount_after_adjustment",
    "buyer_total_amount", "buyer_paid_shipping_fee", "cost_of_goods_sold",
}


def _uang(nilai) -> Decimal:
    try:
        return Decimal(str(nilai if nilai not in (None, "") else 0))
    except Exception:
        return Decimal("0")


def rincian_biaya(baris: SettlementPesanan) -> dict[str, Decimal]:
    """Semua komponen biaya escrow jadi satu peta. Ongkir negatif = selisih yang ditanggung toko."""
    try:
        mentah = json.loads(baris.rincian or "{}")
    except json.JSONDecodeError:
        mentah = {}
    hasil: dict[str, Decimal] = {}
    for kunci, nilai in mentah.items():
        if kunci in BUKAN_BIAYA or isinstance(nilai, (dict, list)):
            continue
        angka = _uang(nilai)
        if angka > 0 and any(s in kunci for s in ("fee", "commission", "charge", "ams", "tax", "coin", "campaign")):
            hasil[kunci] = angka
    for nama, nilai in (
        ("komisi", baris.komisi), ("layanan", baris.layanan), ("transaksi", baris.transaksi),
    ):
        if _uang(nilai) > 0:
            hasil[nama] = _uang(nilai)
    if _uang(baris.ongkir) < 0:
        hasil["selisih_ongkir"] = abs(_uang(baris.ongkir))
    if _uang(baris.penyesuaian) > 0:
        hasil["penyesuaian"] = _uang(baris.penyesuaian)
    return hasil


def baris_dari(settlement: SettlementPesanan) -> BarisStandar:
    biaya = rincian_biaya(settlement)
    potongan = sum(biaya.values(), Decimal("0"))
    tgl = settlement.dirilis_at.date() if settlement.dirilis_at else date.today()
    return BarisStandar(
        baris_file=0,
        kode_pesanan=settlement.order_sn,
        tanggal_cair=tgl,
        jumlah_cair=_uang(settlement.jumlah_cair),
        harga_jual=_uang(settlement.penjualan) or (_uang(settlement.jumlah_cair) + potongan),
        potongan_biaya=potongan,
        rincian_biaya=biaya,
        data_asli={"settlement_id": settlement.id, "akun_id": settlement.akun_id},
    )


async def daftar_toko(erp: AsyncSession) -> list[dict]:
    rows = (await erp.execute(select(AkunMarketplace).where(AkunMarketplace.platform == "shopee"))).scalars().all()
    return [
        {"id": a.id, "nama": a.nama_toko, "shop_id": a.id_toko_eksternal, "masuk": a.nama_toko.strip().lower() in MASUK}
        for a in rows
    ]


async def pasangkan(session: AsyncSession, saluran_id: str, akun_erp_id: str | None) -> BlSaluran:
    saluran = await session.get(BlSaluran, saluran_id)
    if saluran is None:
        raise ValueError("Saluran tidak ditemukan")
    saluran.akun_erp_id = akun_erp_id or None
    await session.flush()
    return saluran


async def tarik(
    session: AsyncSession, erp: AsyncSession, user: BlUser, *, hari: int = 15
) -> dict:
    """Salin settlement toko yang dipasangkan menjadi draf pencairan. Ulang tidak menduplikasi."""
    sejak = datetime.now(timezone.utc) - timedelta(days=hari)
    saluran = (await session.execute(select(BlSaluran).where(BlSaluran.akun_erp_id.is_not(None)))).scalars().all()
    hasil = []
    for sal in saluran:
        rows = (
            await erp.execute(
                select(SettlementPesanan).where(
                    SettlementPesanan.akun_id == sal.akun_erp_id,
                    SettlementPesanan.dirilis_at >= sejak,
                )
            )
        ).scalars().all()
        cocok = []
        for row in rows:
            b = baris_dari(row)
            cocok.append(Cocok(baris=b, kelompok="tidak_cocok", kunci=kunci_unik(sal.id, b), alasan="dari ERP"))
        if not cocok:
            hasil.append({"saluran": sal.nama, "baris": 0})
            continue
        try:
            unggahan = await simpan_baris(
                session, user, sal, cocok,
                nama_file=f"ERP {sal.nama}",
                sumber_sistem=SUMBER,
                sumber_ref=f"erp:{sal.akun_erp_id}:{sejak.date().isoformat()}",
            )
            hasil.append({"saluran": sal.nama, "baris": unggahan.jumlah_baris})
        except Exception as exc:
            hasil.append({"saluran": sal.nama, "baris": 0, "catatan": str(exc)})
    return {"saluran": hasil}
