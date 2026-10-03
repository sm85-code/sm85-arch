"""Business logic -- bumi_lestari Tahap 2: katalog, pemasok, saluran, pelanggan, order."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    HargaGrosirIn,
    OrderIn,
    OrderPatch,
    OrderStatusIn,
    PelangganIn,
    PemasokIn,
    ProdukIn,
    ProdukPatch,
    SaluranIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.services import _hari_ini
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAkunKas
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import (
    JENIS_PEMASOK,
    JENIS_PRODUK,
    JENIS_SALURAN,
    STATUS_BATAL,
    STATUS_ORDER_KAYU,
    STATUS_ORDER_NON_KAYU,
    BlHargaGrosir,
    BlOrder,
    BlPelanggan,
    BlPemasok,
    BlProduk,
    BlSaluran,
)

# Kolom tanggal yang terisi otomatis saat order masuk status tertentu.
_KOLOM_TANGGAL = {
    "dikerjakan": "tgl_pesan_pemasok",
    "diambil": "tgl_diambil",
    "diterima": "tgl_diambil",
    "dicat": "tgl_dicat",
    "dikirim": "tgl_dikirim",
    "selesai": "tgl_selesai",
}
# Jenis pemasok yang sah per jenis produk.
_PEMASOK_UNTUK = {"kayu": "tukang_kayu", "non_kayu": "supplier"}


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


async def _get_or_404(session: AsyncSession, model, row_id: str | None, label: str):
    row = await session.get(model, row_id) if row_id else None
    if row is None or getattr(row, "aktif", True) is False:
        raise _bad(f"{label} tidak ditemukan", status.HTTP_404_NOT_FOUND)
    return row


async def _pastikan_unik(session: AsyncSession, model, kolom, nilai: str, label: str) -> None:
    if (await session.execute(select(model.id).where(kolom == nilai))).first():
        raise _bad(f"{label} sudah dipakai", status.HTTP_409_CONFLICT)


# --- Katalog produk -----------------------------------------------------------------


async def list_produk(session: AsyncSession, jenis_produk: str | None = None) -> list[BlProduk]:
    stmt = select(BlProduk).where(BlProduk.aktif.is_(True)).order_by(BlProduk.sku)
    if jenis_produk:
        stmt = stmt.where(BlProduk.jenis_produk == jenis_produk)
    return list((await session.execute(stmt)).scalars())


async def create_produk(session: AsyncSession, payload: ProdukIn) -> BlProduk:
    if payload.jenis_produk not in JENIS_PRODUK:
        raise _bad(f"Jenis produk harus salah satu dari: {', '.join(JENIS_PRODUK)}")
    sku = payload.sku.strip().upper()
    await _pastikan_unik(session, BlProduk, BlProduk.sku, sku, "SKU")
    produk = BlProduk(
        sku=sku, nama=payload.nama.strip(), jenis_produk=payload.jenis_produk,
        harga_jual=payload.harga_jual, biaya_pokok_default=payload.biaya_pokok_default,
    )
    session.add(produk)
    await session.flush()
    return produk


async def update_produk(session: AsyncSession, produk_id: str, payload: ProdukPatch) -> BlProduk:
    produk = await session.get(BlProduk, produk_id)
    if produk is None:
        raise _bad("Produk tidak ditemukan", status.HTTP_404_NOT_FOUND)
    for kolom, nilai in payload.model_dump(exclude_unset=True).items():
        setattr(produk, kolom, nilai.strip() if isinstance(nilai, str) else nilai)
    await session.flush()
    return produk


# --- Pemasok, saluran, pelanggan, harga grosir --------------------------------------------


async def list_pemasok(session: AsyncSession) -> list[BlPemasok]:
    stmt = select(BlPemasok).where(BlPemasok.aktif.is_(True)).order_by(BlPemasok.nama)
    return list((await session.execute(stmt)).scalars())


async def create_pemasok(session: AsyncSession, payload: PemasokIn) -> BlPemasok:
    if payload.jenis not in JENIS_PEMASOK:
        raise _bad(f"Jenis pemasok harus salah satu dari: {', '.join(JENIS_PEMASOK)}")
    pemasok = BlPemasok(
        nama=payload.nama.strip(), jenis=payload.jenis, kontak=payload.kontak.strip(), catatan=payload.catatan.strip()
    )
    session.add(pemasok)
    await session.flush()
    return pemasok


async def list_saluran(session: AsyncSession) -> list[BlSaluran]:
    stmt = select(BlSaluran).where(BlSaluran.aktif.is_(True)).order_by(BlSaluran.nama)
    return list((await session.execute(stmt)).scalars())


async def create_saluran(session: AsyncSession, payload: SaluranIn) -> BlSaluran:
    if payload.jenis not in JENIS_SALURAN:
        raise _bad(f"Jenis saluran harus salah satu dari: {', '.join(JENIS_SALURAN)}")
    nama = payload.nama.strip()
    await _pastikan_unik(session, BlSaluran, BlSaluran.nama, nama, "Nama saluran")
    if payload.akun_id and await session.get(BlAkunKas, payload.akun_id) is None:
        raise _bad("Akun kas tidak ditemukan", status.HTTP_404_NOT_FOUND)
    saluran = BlSaluran(nama=nama, jenis=payload.jenis, akun_id=payload.akun_id)
    session.add(saluran)
    await session.flush()
    return saluran


async def list_pelanggan(session: AsyncSession) -> list[BlPelanggan]:
    stmt = select(BlPelanggan).where(BlPelanggan.aktif.is_(True)).order_by(BlPelanggan.nama)
    return list((await session.execute(stmt)).scalars())


async def create_pelanggan(session: AsyncSession, payload: PelangganIn) -> BlPelanggan:
    pelanggan = BlPelanggan(
        nama=payload.nama.strip(), kontak=payload.kontak.strip(), tempo_hari=payload.tempo_hari,
        catatan=payload.catatan.strip(),
    )
    session.add(pelanggan)
    await session.flush()
    return pelanggan


async def set_harga_grosir(session: AsyncSession, payload: HargaGrosirIn) -> BlHargaGrosir:
    await _get_or_404(session, BlProduk, payload.produk_id, "Produk")
    await _get_or_404(session, BlPelanggan, payload.pelanggan_id, "Pelanggan")
    row = (
        await session.execute(
            select(BlHargaGrosir).where(
                BlHargaGrosir.produk_id == payload.produk_id, BlHargaGrosir.pelanggan_id == payload.pelanggan_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = BlHargaGrosir(produk_id=payload.produk_id, pelanggan_id=payload.pelanggan_id)
        session.add(row)
    row.harga = payload.harga
    row.harga_cat_jasa = payload.harga_cat_jasa
    row.biaya_proses = payload.biaya_proses
    await session.flush()
    return row


async def list_harga_grosir(session: AsyncSession, pelanggan_id: str | None = None) -> list[BlHargaGrosir]:
    stmt = select(BlHargaGrosir)
    if pelanggan_id:
        stmt = stmt.where(BlHargaGrosir.pelanggan_id == pelanggan_id)
    return list((await session.execute(stmt)).scalars())


# --- Order -------------------------------------------------------------------------------


def alur_status(produk: BlProduk, butuh_cat: bool) -> tuple[str, ...]:
    if produk.jenis_produk == "non_kayu":
        return STATUS_ORDER_NON_KAYU
    return STATUS_ORDER_KAYU if butuh_cat else tuple(s for s in STATUS_ORDER_KAYU if s != "dicat")


async def _cek_pemasok(session: AsyncSession, produk: BlProduk, pemasok_id: str | None) -> None:
    if pemasok_id is None:
        return
    pemasok = await _get_or_404(session, BlPemasok, pemasok_id, "Pemasok")
    if pemasok.jenis != _PEMASOK_UNTUK[produk.jenis_produk]:
        raise _bad(f"Produk {produk.jenis_produk} harus dipasok oleh {_PEMASOK_UNTUK[produk.jenis_produk]}")


async def create_order(session: AsyncSession, payload: OrderIn) -> BlOrder:
    produk = await _get_or_404(session, BlProduk, payload.produk_id, "Produk")
    saluran = await _get_or_404(session, BlSaluran, payload.saluran_id, "Saluran")
    if saluran.jenis == "reseller" and not payload.pelanggan_id:
        raise _bad("Order saluran reseller wajib memilih pelanggan")
    if payload.pelanggan_id:
        await _get_or_404(session, BlPelanggan, payload.pelanggan_id, "Pelanggan")
    await _cek_pemasok(session, produk, payload.pemasok_id)

    # Harga: nilai eksplisit > harga grosir pelanggan (barang, cat+jasa, biaya proses) > harga katalog.
    grosir = None
    if payload.pelanggan_id:
        grosir = (
            await session.execute(
                select(BlHargaGrosir).where(
                    BlHargaGrosir.produk_id == produk.id, BlHargaGrosir.pelanggan_id == payload.pelanggan_id
                )
            )
        ).scalar_one_or_none()
    harga = payload.harga_satuan
    if harga is None:
        harga = Decimal(grosir.harga) if grosir else Decimal(produk.harga_jual)
    harga_cat_jasa = payload.harga_cat_jasa
    if harga_cat_jasa is None:
        harga_cat_jasa = Decimal(grosir.harga_cat_jasa) if grosir else Decimal("0")
    biaya_proses = payload.biaya_proses
    if biaya_proses is None:
        biaya_proses = Decimal(grosir.biaya_proses) if grosir else Decimal("0")
    biaya = payload.biaya_pokok if payload.biaya_pokok is not None else Decimal(produk.biaya_pokok_default) * payload.qty
    butuh_cat = produk.jenis_produk == "kayu" if payload.butuh_cat is None else payload.butuh_cat
    if produk.jenis_produk == "non_kayu":
        butuh_cat = False
    if not butuh_cat:
        harga_cat_jasa = Decimal("0")  # order polos: tidak ada komponen cat

    order = BlOrder(
        no_order=payload.no_order.strip(),
        tanggal_order=payload.tanggal_order or _hari_ini(),
        saluran_id=saluran.id,
        pelanggan_id=payload.pelanggan_id,
        nama_pembeli=payload.nama_pembeli.strip(),
        produk_id=produk.id,
        qty=payload.qty,
        harga_satuan=harga,
        harga_cat_jasa=harga_cat_jasa,
        biaya_proses=biaya_proses,
        warna=payload.warna.strip(),
        potongan_marketplace=payload.potongan_marketplace,
        pemasok_id=payload.pemasok_id,
        biaya_pokok=biaya,
        butuh_cat=butuh_cat,
        status="dipesan",
        catatan=payload.catatan.strip(),
    )
    session.add(order)
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: str) -> BlOrder:
    order = await session.get(BlOrder, order_id)
    if order is None:
        raise _bad("Order tidak ditemukan", status.HTTP_404_NOT_FOUND)
    return order


async def list_order(
    session: AsyncSession,
    *,
    status_order: str | None = None,
    saluran_id: str | None = None,
    pelanggan_id: str | None = None,
    jenis_produk: str | None = None,
    dari: date | None = None,
    sampai: date | None = None,
) -> list[BlOrder]:
    stmt = select(BlOrder).order_by(BlOrder.tanggal_order.desc(), BlOrder.created_at.desc())
    if status_order:
        stmt = stmt.where(BlOrder.status == status_order)
    if saluran_id:
        stmt = stmt.where(BlOrder.saluran_id == saluran_id)
    if pelanggan_id:
        stmt = stmt.where(BlOrder.pelanggan_id == pelanggan_id)
    if jenis_produk:
        stmt = stmt.join(BlProduk, BlProduk.id == BlOrder.produk_id).where(BlProduk.jenis_produk == jenis_produk)
    if dari:
        stmt = stmt.where(BlOrder.tanggal_order >= dari)
    if sampai:
        stmt = stmt.where(BlOrder.tanggal_order <= sampai)
    return list((await session.execute(stmt)).scalars())


async def update_order(session: AsyncSession, order_id: str, payload: OrderPatch) -> BlOrder:
    order = await get_order(session, order_id)
    if order.status in ("selesai", STATUS_BATAL):
        raise _bad("Order yang sudah selesai/batal tidak bisa diubah", status.HTTP_409_CONFLICT)
    data = payload.model_dump(exclude_unset=True)
    produk = await session.get(BlProduk, order.produk_id)
    if "pemasok_id" in data:
        await _cek_pemasok(session, produk, data["pemasok_id"])
    if data.get("butuh_cat") is not None:
        if produk.jenis_produk == "non_kayu" and data["butuh_cat"]:
            raise _bad("Produk non kayu tidak dicat")
        if order.status in ("dicat", "dikirim"):
            raise _bad("Status order sudah melewati langkah pengecatan")
    for kolom, nilai in data.items():
        if nilai is None and kolom != "pemasok_id":
            continue
        setattr(order, kolom, nilai.strip() if isinstance(nilai, str) else nilai)
    if not order.butuh_cat:
        order.harga_cat_jasa = Decimal("0")  # polos: tidak ada komponen cat
    await session.flush()
    return order


async def ubah_status_order(session: AsyncSession, order_id: str, payload: OrderStatusIn) -> BlOrder:
    """Majukan satu langkah sesuai alur produk, atau batalkan (sebelum selesai)."""
    order = await get_order(session, order_id)
    if order.status in ("selesai", STATUS_BATAL):
        raise _bad("Order sudah selesai/batal", status.HTTP_409_CONFLICT)
    if payload.status == STATUS_BATAL:
        order.status = STATUS_BATAL
        await session.flush()
        return order
    produk = await session.get(BlProduk, order.produk_id)
    alur = alur_status(produk, order.butuh_cat)
    berikut = alur[alur.index(order.status) + 1]
    if payload.status != berikut:
        raise _bad(f"Status berikutnya harus '{berikut}' (alur: {' → '.join(alur)})")
    if berikut in ("dikerjakan", "diambil") and order.pemasok_id is None:
        raise _bad("Pilih pemasok (tukang kayu) lebih dulu")
    if berikut == "diterima" and order.pemasok_id is None:
        raise _bad("Pilih pemasok (supplier) lebih dulu")
    order.status = berikut
    if berikut in _KOLOM_TANGGAL:
        setattr(order, _KOLOM_TANGGAL[berikut], payload.tanggal or _hari_ini())
    await session.flush()
    return order
