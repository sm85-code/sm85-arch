"""Business logic -- bumi_lestari Tahap 2: katalog, pemasok, saluran, pelanggan, order."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import kolom_core
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    HargaGrosirIn,
    OrderIn,
    OrderPatch,
    OrderReturIn,
    OrderStatusIn,
    PelangganIn,
    PelangganPatch,
    PemasokIn,
    PemasokPatch,
    ProdukIn,
    ProdukPatch,
    SaluranIn,
    SaluranPatch,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.audit_core import catat_audit
from tenants.bumi_lestari.modules.bumi_lestari.application.services import _hari_ini, get_profil, pastikan_bulan_terbuka
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAkunKas
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pembayaran import (
    BlPembayaranPemasok,
    BlPembayaranPemasokItem,
    BlPenerimaanReseller,
    BlPenerimaanResellerItem,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import (
    JENIS_PACKING,
    JENIS_PEMASOK,
    JENIS_PRODUK,
    JENIS_SALURAN,
    JENIS_SALURAN_CAIR,
    STATUS_BATAL,
    STATUS_CAIR_CAIR,
    STATUS_RETUR,
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
        sku=sku, nama=payload.nama.strip(), jenis_produk=payload.jenis_produk, ukuran=payload.ukuran.strip(),
        harga_jual=payload.harga_jual, biaya_pokok_default=payload.biaya_pokok_default,
    )
    await kolom_core.terapkan(session, "produk", produk, payload.kolom_tambahan, baru=True)
    session.add(produk)
    await session.flush()
    return produk


async def update_produk(session: AsyncSession, produk_id: str, payload: ProdukPatch) -> BlProduk:
    produk = await session.get(BlProduk, produk_id)
    if produk is None:
        raise _bad("Produk tidak ditemukan", status.HTTP_404_NOT_FOUND)
    for kolom, nilai in payload.model_dump(exclude_unset=True, exclude={"kolom_tambahan"}).items():
        setattr(produk, kolom, nilai.strip() if isinstance(nilai, str) else nilai)
    await kolom_core.terapkan(session, "produk", produk, payload.kolom_tambahan, baru=False)
    await session.flush()
    return produk


# --- Pemasok, saluran, pelanggan, harga grosir --------------------------------------------


async def _kode_berikut(session: AsyncSession, model, *kondisi) -> str:
    """Kode urut 3 digit berikutnya ("001", "002", ...) untuk nomor PO/invoice."""
    stmt = select(model.kode).where(*kondisi)
    angka = [int(k) for k in (await session.execute(stmt)).scalars() if k.isdigit()]
    return f"{max(angka, default=0) + 1:03d}"


async def list_pemasok(session: AsyncSession) -> list[BlPemasok]:
    stmt = select(BlPemasok).where(BlPemasok.aktif.is_(True)).order_by(BlPemasok.nama)
    return list((await session.execute(stmt)).scalars())


async def create_pemasok(session: AsyncSession, payload: PemasokIn) -> BlPemasok:
    if payload.jenis not in JENIS_PEMASOK:
        raise _bad(f"Jenis pemasok harus salah satu dari: {', '.join(JENIS_PEMASOK)}")
    kode = payload.kode.strip() or await _kode_berikut(session, BlPemasok, BlPemasok.jenis == payload.jenis)
    pemasok = BlPemasok(
        nama=payload.nama.strip(), jenis=payload.jenis, kode=kode, kontak=payload.kontak.strip(), no_wa=payload.no_wa.strip(),
        nama_bank=payload.nama_bank.strip(), no_rekening=payload.no_rekening.strip(),
        atas_nama=payload.atas_nama.strip(), catatan=payload.catatan.strip(),
    )
    await kolom_core.terapkan(session, "pemasok", pemasok, payload.kolom_tambahan, baru=True)
    session.add(pemasok)
    await session.flush()
    return pemasok


async def _ubah(session: AsyncSession, model, row_id: str, payload, label: str, *, kode_unik: bool = False):
    """PATCH generik: hanya kolom yang dikirim yang diubah; teks di-strip; kode/nama harus tetap unik."""
    row = await session.get(model, row_id)
    if row is None:
        raise _bad(f"{label} tidak ditemukan", status.HTTP_404_NOT_FOUND)
    data = payload.model_dump(exclude_unset=True, exclude={"kolom_tambahan"})
    entitas = {BlPemasok: "pemasok", BlPelanggan: "pelanggan"}.get(model)
    if entitas:
        await kolom_core.terapkan(session, entitas, row, getattr(payload, "kolom_tambahan", None), baru=False)
    for kolom, nilai in data.items():
        if nilai is None and kolom != "akun_id":
            continue
        nilai = nilai.strip() if isinstance(nilai, str) else nilai
        if kolom in ("kode", "nama") and kode_unik and nilai != getattr(row, kolom):
            await _pastikan_unik(session, model, getattr(model, kolom), nilai, kolom.capitalize())
        setattr(row, kolom, nilai)
    await session.flush()
    return row


async def update_pemasok(session: AsyncSession, pemasok_id: str, payload: PemasokPatch) -> BlPemasok:
    pemasok = await session.get(BlPemasok, pemasok_id)
    if pemasok is None:
        raise _bad("Pemasok tidak ditemukan", status.HTTP_404_NOT_FOUND)
    kode = payload.kode
    if kode is not None and kode.strip() != pemasok.kode:  # kode PO tidak boleh kembar di antara pemasok sejenis
        dobel = await session.execute(
            select(BlPemasok.id).where(BlPemasok.kode == kode.strip(), BlPemasok.jenis == pemasok.jenis)
        )
        if dobel.first():
            raise _bad("Kode sudah dipakai pemasok lain", status.HTTP_409_CONFLICT)
    return await _ubah(session, BlPemasok, pemasok_id, payload, "Pemasok")


async def update_pelanggan(session: AsyncSession, pelanggan_id: str, payload: PelangganPatch) -> BlPelanggan:
    pelanggan = await session.get(BlPelanggan, pelanggan_id)
    if pelanggan is None:
        raise _bad("Pelanggan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    kode = payload.kode
    if kode is not None and kode.strip() != pelanggan.kode:
        if (await session.execute(select(BlPelanggan.id).where(BlPelanggan.kode == kode.strip()))).first():
            raise _bad("Kode sudah dipakai pelanggan lain", status.HTTP_409_CONFLICT)
    return await _ubah(session, BlPelanggan, pelanggan_id, payload, "Pelanggan")


async def update_saluran(session: AsyncSession, saluran_id: str, payload: SaluranPatch) -> BlSaluran:
    if payload.akun_id and await session.get(BlAkunKas, payload.akun_id) is None:
        raise _bad("Akun kas tidak ditemukan", status.HTTP_404_NOT_FOUND)
    return await _ubah(session, BlSaluran, saluran_id, payload, "Saluran", kode_unik=True)


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
    kode = payload.kode.strip() or await _kode_berikut(session, BlPelanggan)
    pelanggan = BlPelanggan(
        nama=payload.nama.strip(), kode=kode, alamat=payload.alamat.strip(), kontak=payload.kontak.strip(),
        no_wa=payload.no_wa.strip(),
        catatan=payload.catatan.strip(),
    )
    await kolom_core.terapkan(session, "pelanggan", pelanggan, payload.kolom_tambahan, baru=True)
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
    row.harga_packing_biasa = payload.harga_packing_biasa
    row.harga_packing_kayu = payload.harga_packing_kayu
    await session.flush()
    return row


async def list_harga_grosir(session: AsyncSession, pelanggan_id: str | None = None) -> list[BlHargaGrosir]:
    stmt = select(BlHargaGrosir)
    if pelanggan_id:
        stmt = stmt.where(BlHargaGrosir.pelanggan_id == pelanggan_id)
    return list((await session.execute(stmt)).scalars())


# --- Order -------------------------------------------------------------------------------


# Kolom yang tetap boleh diubah setelah order dibayar (tidak memengaruhi uang).
KOLOM_BEBAS_SETELAH_DIBAYAR = frozenset({"nama_pembeli", "warna", "catatan"})


async def status_bayar_order(session: AsyncSession, order_ids: list[str] | None = None) -> tuple[set[str], set[str]]:
    """(order yang ada di pembayaran tukang aktif, order yang ada di penerimaan penjual lain aktif) --
    draf maupun terkirim (spesifikasi AB-OR-4, AB-KR-7)."""
    q_tukang = (
        select(BlPembayaranPemasokItem.order_id)
        .join(BlPembayaranPemasok, BlPembayaranPemasok.id == BlPembayaranPemasokItem.pembayaran_id)
        .where(BlPembayaranPemasok.dibatalkan.is_(False))
    )
    q_jual = (
        select(BlPenerimaanResellerItem.order_id)
        .join(BlPenerimaanReseller, BlPenerimaanReseller.id == BlPenerimaanResellerItem.penerimaan_id)
        .where(BlPenerimaanReseller.dibatalkan.is_(False))
    )
    if order_ids is not None:
        q_tukang = q_tukang.where(BlPembayaranPemasokItem.order_id.in_(order_ids))
        q_jual = q_jual.where(BlPenerimaanResellerItem.order_id.in_(order_ids))
    return set((await session.execute(q_tukang)).scalars()), set((await session.execute(q_jual)).scalars())


async def lengkapi_status_bayar(session: AsyncSession, orders: list[BlOrder]) -> list[BlOrder]:
    """Tempel badge dibayar_tukang / dibayar_penjual_lain (atribut sementara, dibaca OrderOut)."""
    tukang, jual = await status_bayar_order(session, [o.id for o in orders])
    for o in orders:
        o.dibayar_tukang = o.id in tukang
        o.dibayar_penjual_lain = o.id in jual
    return orders


def _pesan_terkunci(order: BlOrder) -> str:
    asal = "pembayaran tukang/supplier" if order.dibayar_tukang else "penerimaan penjual lain"
    return f"Order sudah masuk {asal}; keluarkan dulu dari draf (atau batalkan kirimannya) untuk mengubahnya"


async def _cek_no_order(
    session: AsyncSession, saluran: BlSaluran, no_order: str, tanggal_order: date, kecuali_id: str | None = None
) -> None:
    """Order marketplace wajib nomor pesanan, unik per saluran. Satu pesanan bisa berisi beberapa barang (beberapa
    baris order): baris dengan nomor & tanggal order yang sama dianggap pesanan yang sama."""
    if saluran.jenis != "marketplace":
        return
    if not no_order:
        raise _bad(f"Nomor pesanan wajib diisi untuk order {saluran.nama}", 422)
    stmt = select(BlOrder.tanggal_order).where(
        BlOrder.saluran_id == saluran.id, BlOrder.no_order == no_order, BlOrder.status != STATUS_BATAL
    )
    if kecuali_id:
        stmt = stmt.where(BlOrder.id != kecuali_id)
    for (tgl,) in (await session.execute(stmt)).all():
        if tgl != tanggal_order:
            raise _bad(f"Nomor pesanan {no_order} sudah dipakai di {saluran.nama}", status.HTTP_409_CONFLICT)


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


def _cek_jenis_packing(jenis: str) -> None:
    if jenis not in JENIS_PACKING:
        raise _bad(f"Jenis packing harus salah satu dari: {', '.join(JENIS_PACKING)}")


def _harga_packing_grosir(grosir: BlHargaGrosir | None, jenis: str) -> Decimal:
    if grosir is None:
        return Decimal("0")
    return Decimal(grosir.harga_packing_kayu if jenis == "kayu" else grosir.harga_packing_biasa)


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
    _cek_jenis_packing(payload.jenis_packing)
    harga_packing = payload.harga_packing
    if harga_packing is None:
        harga_packing = _harga_packing_grosir(grosir, payload.jenis_packing)
    biaya_proses = payload.biaya_proses
    if biaya_proses is None:
        # Flat per order, dari profil UMKM; hanya untuk order penjual lain (saluran lain sudah all-in).
        biaya_proses = Decimal((await get_profil(session)).biaya_proses_order) if payload.pelanggan_id else Decimal("0")
    biaya = payload.biaya_pokok if payload.biaya_pokok is not None else Decimal(produk.biaya_pokok_default) * payload.qty
    butuh_cat = produk.jenis_produk == "kayu" if payload.butuh_cat is None else payload.butuh_cat
    if produk.jenis_produk == "non_kayu":
        butuh_cat = False
    if not butuh_cat:
        harga_cat_jasa = Decimal("0")  # order polos: tidak ada komponen cat
    tanggal_order = payload.tanggal_order or _hari_ini()
    await _cek_no_order(session, saluran, payload.no_order.strip(), tanggal_order)

    order = BlOrder(
        no_order=payload.no_order.strip(),
        tanggal_order=tanggal_order,
        saluran_id=saluran.id,
        pelanggan_id=payload.pelanggan_id,
        nama_pembeli=payload.nama_pembeli.strip(),
        produk_id=produk.id,
        qty=payload.qty,
        harga_satuan=harga,
        harga_cat_jasa=harga_cat_jasa,
        jenis_packing=payload.jenis_packing,
        harga_packing=harga_packing,
        biaya_proses=biaya_proses,
        warna=payload.warna.strip(),
        potongan_marketplace=payload.potongan_marketplace,
        pemasok_id=payload.pemasok_id,
        biaya_pokok=biaya,
        butuh_cat=butuh_cat,
        status="dipesan",
        catatan=payload.catatan.strip(),
    )
    await kolom_core.terapkan(session, "order", order, payload.kolom_tambahan, baru=True)
    session.add(order)
    await session.flush()
    order.dibayar_tukang = order.dibayar_penjual_lain = False
    return order


async def get_order(session: AsyncSession, order_id: str) -> BlOrder:
    order = await session.get(BlOrder, order_id)
    if order is None:
        raise _bad("Order tidak ditemukan", status.HTTP_404_NOT_FOUND)
    await lengkapi_status_bayar(session, [order])
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
    status_cair: str | None = None,
) -> list[BlOrder]:
    stmt = select(BlOrder).order_by(BlOrder.tanggal_order.desc(), BlOrder.created_at.desc())
    if status_order:
        stmt = stmt.where(BlOrder.status == status_order)
    if status_cair:
        stmt = stmt.where(BlOrder.status_cair == status_cair)
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
    return await lengkapi_status_bayar(session, list((await session.execute(stmt)).scalars()))


async def update_order(session: AsyncSession, order_id: str, payload: OrderPatch) -> BlOrder:
    order = await get_order(session, order_id)
    if order.status in ("selesai", STATUS_BATAL, STATUS_RETUR):
        raise _bad("Order yang sudah selesai/batal/retur tidak bisa diubah", status.HTTP_409_CONFLICT)
    data = payload.model_dump(exclude_unset=True, exclude={"kolom_tambahan"})
    await kolom_core.terapkan(session, "order", order, payload.kolom_tambahan, baru=False)
    if order.dibayar_tukang or order.dibayar_penjual_lain:
        def _berubah(kolom, nilai):
            lama = getattr(order, kolom)
            if isinstance(nilai, str):
                nilai = nilai.strip()
            if isinstance(lama, Decimal) and nilai is not None:
                return Decimal(lama) != Decimal(nilai)
            return lama != nilai
        terkunci = [
            k for k, v in data.items()
            if k not in KOLOM_BEBAS_SETELAH_DIBAYAR and (v is not None or k == "pemasok_id") and _berubah(k, v)
        ]
        if terkunci:
            raise _bad(_pesan_terkunci(order), status.HTTP_409_CONFLICT)
    if data.get("no_order") is not None:
        saluran = await session.get(BlSaluran, order.saluran_id)
        await _cek_no_order(session, saluran, data["no_order"].strip(), order.tanggal_order, kecuali_id=order.id)
    produk = await session.get(BlProduk, order.produk_id)
    if "pemasok_id" in data:
        await _cek_pemasok(session, produk, data["pemasok_id"])
    if data.get("butuh_cat") is not None:
        if produk.jenis_produk == "non_kayu" and data["butuh_cat"]:
            raise _bad("Produk non kayu tidak dicat")
        if order.status in ("dicat", "dikirim"):
            raise _bad("Status order sudah melewati langkah pengecatan")
    if data.get("jenis_packing") is not None:
        _cek_jenis_packing(data["jenis_packing"])
        if "harga_packing" not in data and order.pelanggan_id:
            # Ganti jenis packing tanpa harga eksplisit -> ambil harga grosir jenis baru.
            grosir = (
                await session.execute(
                    select(BlHargaGrosir).where(
                        BlHargaGrosir.produk_id == order.produk_id, BlHargaGrosir.pelanggan_id == order.pelanggan_id
                    )
                )
            ).scalar_one_or_none()
            order.harga_packing = _harga_packing_grosir(grosir, data["jenis_packing"])
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
    if order.status in ("selesai", STATUS_BATAL, STATUS_RETUR):
        raise _bad("Order sudah selesai/batal/retur", status.HTTP_409_CONFLICT)
    if payload.status == STATUS_BATAL:
        if order.dibayar_tukang or order.dibayar_penjual_lain:
            raise _bad(_pesan_terkunci(order), status.HTTP_409_CONFLICT)
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


async def retur_order(session: AsyncSession, order_id: str, payload: OrderReturIn, user_id: str | None = None) -> BlOrder:
    """Retur sebelum cair (AB-BC-3): order keluar dari belum cair; biaya pokoknya dilaporkan sebagai Kerugian retur
    (lihat laba_core.reklas_retur). Retur setelah cair datang dari baris retur/penyesuaian file pencairan."""
    order = await get_order(session, order_id)
    saluran = await session.get(BlSaluran, order.saluran_id)
    if saluran is None or saluran.jenis not in JENIS_SALURAN_CAIR:
        raise _bad("Retur hanya untuk order marketplace/Toko web")
    if order.status == STATUS_RETUR:
        raise _bad("Order sudah ditandai retur", status.HTTP_409_CONFLICT)
    if order.status not in ("dikirim", "selesai"):
        raise _bad("Order belum dikirim; batalkan saja bila tidak jadi", status.HTTP_409_CONFLICT)
    if order.status_cair == STATUS_CAIR_CAIR:
        raise _bad(
            "Order sudah cair; retur setelah cair dicatat dari file pencairan (baris retur/penyesuaian)",
            status.HTTP_409_CONFLICT,
        )
    tanggal = payload.tanggal or _hari_ini()
    if order.tgl_dikirim and tanggal < order.tgl_dikirim:
        raise _bad("Tanggal retur tidak boleh sebelum tanggal kirim")
    await pastikan_bulan_terbuka(session, tanggal)
    sebelum = {"status": order.status}
    order.status = STATUS_RETUR
    order.tgl_retur = tanggal
    order.alasan_retur = payload.alasan.strip()
    order.kembali_stok = payload.kembali_stok
    await catat_audit(
        session, user_id, "retur", "order", order.id, sebelum=sebelum,
        sesudah={"status": STATUS_RETUR, "tgl_retur": tanggal, "kembali_stok": payload.kembali_stok},
        alasan=order.alasan_retur,
    )
    await session.flush()
    return order
