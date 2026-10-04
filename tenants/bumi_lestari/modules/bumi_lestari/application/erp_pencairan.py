"""Tarik pencairan Shopee dari marketplace_erp ke draf pencairan Bumi Lestari.

Hanya saluran yang punya akun_erp_id. Toko yang tidak dipasangkan tidak ikut.
Potongan adalah jumlah seluruh komponen biaya escrow, termasuk selisih ongkir.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import order_services
from tenants.bumi_lestari.modules.bumi_lestari.application.pencairan_format import BarisStandar
from tenants.bumi_lestari.modules.bumi_lestari.application.pencairan_services import Cocok, kunci_unik, simpan_baris
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import OrderIn
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlOrder, BlProduk, BlSaluran
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pencairan import BlPencairanBaris
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    AkunMarketplace,
    Pesanan,
    SettlementPesanan,
)

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
    sudah = " ".join(hasil)
    for nama, nilai, kunci in (
        ("komisi", baris.komisi, "commission"),
        ("layanan", baris.layanan, "service_fee"),
        ("transaksi", baris.transaksi, "transaction"),
    ):
        if _uang(nilai) > 0 and kunci not in sudah:
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
        {"id": a.id, "nama": a.nama_toko, "shop_id": a.id_toko_eksternal, "masuk": a.nama_toko.strip().lower() in MASUK or ("azfa furniture" in a.nama_toko.lower() and "digital" not in a.nama_toko.lower())}
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


SKU_BELUM = "ERP-BELUM"


async def _produk_sementara(session: AsyncSession) -> BlProduk:
    ada = (await session.execute(select(BlProduk).where(BlProduk.sku == SKU_BELUM))).scalar_one_or_none()
    if ada:
        return ada
    produk = BlProduk(sku=SKU_BELUM, nama="Pesanan ERP, produk belum dipetakan", jenis_produk="kayu", sumber_sistem=SUMBER, sumber_ref=SKU_BELUM)
    session.add(produk)
    await session.flush()
    return produk


async def tarik_order(session: AsyncSession, erp: AsyncSession, *, hari: int = 30) -> dict:
    """Buat order Bumi Lestari dari pesanan ERP toko yang dipasangkan. Nomor pesanan = nomor Shopee."""
    sejak = datetime.now(timezone.utc) - timedelta(days=hari)
    saluran = (await session.execute(select(BlSaluran).where(BlSaluran.akun_erp_id.is_not(None)))).scalars().all()
    produk = await _produk_sementara(session)
    dibuat = 0
    for sal in saluran:
        pesanan = (
            await erp.execute(
                select(Pesanan)
                .where(Pesanan.akun_id == sal.akun_erp_id, Pesanan.dipesan_at >= sejak)
                .options(selectinload(Pesanan.items))
            )
        ).scalars().all()
        for pesan in pesanan:
            item = pesan.items or [None]
            for baris in item:
                ref = f"mpe_item_pesanan:{baris.id if baris else pesan.id}"
                sudah = (
                    await session.execute(select(BlOrder.id).where(BlOrder.sumber_ref == ref))
                ).scalar_one_or_none()
                if sudah:
                    continue
                nama = (baris.nama_produk if baris else "") or "dari ERP"
                produk_id = await _produk_dari_peta(session, nama) or produk.id
                order = await order_services.create_order(session, OrderIn(
                    no_order=pesan.id_eksternal,
                    tanggal_order=(pesan.dipesan_at or pesan.created_at).date(),
                    saluran_id=sal.id,
                    nama_pembeli=pesan.nama_pembeli or "",
                    produk_id=produk_id,
                    qty=baris.qty if baris else 1,
                    harga_satuan=baris.harga_satuan if baris else pesan.total,
                    catatan=nama,
                ))
                order.sumber_sistem = SUMBER
                order.sumber_ref = ref
                dibuat += 1
        menunggu = (
            await session.execute(
                select(BlPencairanBaris).where(
                    BlPencairanBaris.saluran_id == sal.id,
                    BlPencairanBaris.status_cocok == "tidak_cocok",
                    BlPencairanBaris.dibatalkan.is_(False),
                )
            )
        ).scalars().all()
        if menunggu:
            kode = {r.kode_pesanan.strip().upper() for r in menunggu}
            orders = (
                await session.execute(
                    select(BlOrder).where(BlOrder.saluran_id == sal.id, func.upper(BlOrder.no_order).in_(kode))
                )
            ).scalars().all()
            by_no = {o.no_order.strip().upper(): o.id for o in orders}
            for r in menunggu:
                oid = by_no.get(r.kode_pesanan.strip().upper())
                if oid:
                    r.order_id = oid
                    r.status_cocok = "cocok"
    await session.flush()
    return {"order": dibuat}



async def _pastikan_peta(session: AsyncSession) -> None:
    from sqlalchemy import text
    await session.execute(text(
        "CREATE TABLE IF NOT EXISTS bl_peta_nama (id VARCHAR(64) PRIMARY KEY, nama VARCHAR(255) NOT NULL UNIQUE, produk_id VARCHAR(64) NOT NULL)"
    ))


async def _produk_dari_peta(session: AsyncSession, nama: str) -> str | None:
    await _pastikan_peta(session)
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlPetaNama
    baris = (await session.execute(select(BlPetaNama).where(BlPetaNama.nama == nama.strip()))).scalar_one_or_none()
    return baris.produk_id if baris else None


async def daftar_belum_peta(session: AsyncSession) -> list[dict]:
    await _pastikan_peta(session)
    sementara = await _produk_sementara(session)
    baris = (await session.execute(select(BlOrder).where(BlOrder.produk_id == sementara.id))).scalars().all()
    hitung: dict[str, int] = {}
    for o in baris:
        nama = (o.catatan or "").strip() or "tanpa nama"
        hitung[nama] = hitung.get(nama, 0) + 1
    return [{"nama": n, "jumlah": j} for n, j in sorted(hitung.items())]


async def simpan_peta(session: AsyncSession, nama: str, produk_id: str) -> dict:
    await _pastikan_peta(session)
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlPetaNama, BlProduk
    produk = await session.get(BlProduk, produk_id)
    if not produk or produk.sku == SKU_BELUM:
        raise ValueError("Pilih jenis katalog, bukan produk sementara")
    ada = (await session.execute(select(BlPetaNama).where(BlPetaNama.nama == nama.strip()))).scalar_one_or_none()
    if ada:
        ada.produk_id = produk_id
    else:
        session.add(BlPetaNama(nama=nama.strip(), produk_id=produk_id))
    sementara = await _produk_sementara(session)
    orders = (await session.execute(select(BlOrder).where(BlOrder.produk_id == sementara.id, BlOrder.catatan == nama.strip()))).scalars().all()
    for o in orders:
        o.produk_id = produk_id
        if produk.jenis_produk != "kayu":
            o.butuh_cat = False
    await session.flush()
    return {"nama": nama.strip(), "order": len(orders), "jenis": produk.jenis_produk}
