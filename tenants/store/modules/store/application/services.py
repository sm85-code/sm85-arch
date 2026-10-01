from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.security import hash_password, verify_password
from tenants.store.modules.store.application.schemas import (
    AlamatIn,
    AlamatPatch,
    KategoriIn,
    PengaturanPatch,
    PengirimanIn,
    ProdukIn,
    ProdukPatch,
    RegisterRequest,
)

if TYPE_CHECKING:
    from tenants.store.modules.store.application.schemas import StaffIn, StaffPatch
from tenants.store.modules.store.application.slug import slugify, with_suffix
from tenants.store.modules.store.infrastructure.media_storage import media_url
from tenants.store.modules.store.infrastructure.models import (
    ROLE_ADMIN,
    ROLE_OWNER,
    STATUS_PENGIRIMAN,
    STATUS_PESANAN,
    AdminStore,
    AlamatStore,
    ItemKeranjang,
    ItemPesanan,
    KategoriStore,
    PercakapanStore,
    PengaturanStore,
    PengirimanStore,
    PesanChatStore,
    PesananStore,
    ProdukStore,
    PembeliStore,
)

# Transisi status pengiriman yang diizinkan.
_TRANSISI_STATUS_PENGIRIMAN = {
    "menunggu_pickup": {"dikirim", "bermasalah"},
    "dikirim": {"diterima", "bermasalah"},
    "diterima": set(),
    "bermasalah": {"dikirim"},
}

# Status pesanan yang dihitung sebagai penjualan sah untuk laporan --
# menunggu_pembayaran & dibatalkan tidak dihitung (belum tentu jadi uang).
_STATUS_TERHITUNG_PENJUALAN = ("dibayar", "diproses", "dikirim", "selesai")

# Transisi status yang diizinkan. dibatalkan bisa dari status manapun
# sebelum selesai/dikirim (pembatalan setelah dikirim harus lewat proses
# retur, bukan sekadar ubah status -- di luar scope modul ini).
_TRANSISI_STATUS = {
    "menunggu_pembayaran": {"dibayar", "dibatalkan"},
    "dibayar": {"diproses", "dibatalkan"},
    "diproses": {"dikirim"},
    "dikirim": {"selesai"},
    "selesai": set(),
    "dibatalkan": set(),
}


def pembeli_out(user: PembeliStore) -> dict:
    return {"id": user.id, "nama": user.nama, "email": user.email}


def admin_out(user: AdminStore) -> dict:
    return {"id": user.id, "nama": user.nama, "email": user.email, "role": user.role}


def produk_out(produk: ProdukStore) -> dict:
    return {
        "id": produk.id,
        "slug": produk.slug,
        "nama": produk.nama,
        "deskripsi": produk.deskripsi,
        "kategori_id": produk.kategori_id,
        "kategori_nama": produk.kategori.nama if produk.kategori else None,
        "harga": str(produk.harga),
        "stok": produk.stok,
        "foto_key": produk.foto_key,
        "foto_url": media_url(produk.foto_key),
        "aktif": produk.aktif,
        "sumber": produk.sumber,
    }


async def authenticate_admin(session: AsyncSession, email: str, password: str) -> AdminStore:
    user = (await session.execute(select(AdminStore).where(AdminStore.email == email))).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email atau password salah")
    return user


async def change_admin_password(session: AsyncSession, user: AdminStore, current: str, new: str) -> AdminStore:
    if not verify_password(current, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password saat ini salah")
    if current == new:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password baru harus berbeda dari yang lama")
    user.password_hash = hash_password(new)
    await session.flush()
    return user


async def authenticate_buyer(session: AsyncSession, email: str, password: str) -> PembeliStore:
    user = (await session.execute(select(PembeliStore).where(PembeliStore.email == email))).scalar_one_or_none()
    # Google-only accounts have no password_hash; verify_password returns
    # False for an empty hash, so they cannot log in with a password.
    if not user or not verify_password(password, user.password_hash or ""):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email atau password salah")
    return user


async def register(session: AsyncSession, payload: RegisterRequest) -> PembeliStore:
    existing = (await session.execute(select(PembeliStore).where(PembeliStore.email == payload.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email sudah terdaftar")
    user = PembeliStore(nama=payload.nama, email=payload.email, password_hash=hash_password(payload.password))
    session.add(user)
    await session.flush()
    return user


async def list_produk(session: AsyncSession, *, hanya_aktif: bool = False) -> list[ProdukStore]:
    stmt = select(ProdukStore).options(selectinload(ProdukStore.kategori)).order_by(ProdukStore.created_at.desc())
    if hanya_aktif:
        stmt = stmt.where(ProdukStore.aktif.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def get_produk(session: AsyncSession, produk_id: str) -> ProdukStore:
    stmt = select(ProdukStore).where(ProdukStore.id == produk_id).options(selectinload(ProdukStore.kategori))
    produk = (await session.execute(stmt)).scalar_one_or_none()
    if not produk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk tidak ditemukan")
    return produk


async def unique_slug(session: AsyncSession, nama: str, *, exclude_id: str | None = None) -> str:
    """slugify(nama), made unique among products: base, base-2, base-3 ..."""
    base = slugify(nama)
    n = 1
    while True:
        candidate = with_suffix(base, n)
        stmt = select(ProdukStore.id).where(ProdukStore.slug == candidate)
        if exclude_id:
            stmt = stmt.where(ProdukStore.id != exclude_id)
        if (await session.execute(stmt.limit(1))).first() is None:
            return candidate
        n += 1


async def heal_slugs(session: AsyncSession) -> bool:
    """Self-heal: give slug-less products (rows that predate slugs) their slug while the catalog is read.

    Startup already backfills, but this makes the public catalog converge even if that step did not run or lost a
    race. A savepoint keeps a lost race (another request filled the same slugs first) from poisoning the session.
    Returns True when something was missing, i.e. the caller should (re)load its rows.
    """
    from sqlalchemy.exc import IntegrityError

    from tenants.store.modules.store.infrastructure.seeder import backfill_slugs

    missing = (await session.execute(select(func.count()).select_from(ProdukStore).where(ProdukStore.slug.is_(None)))).scalar_one()
    if not missing:
        return False
    try:
        async with session.begin_nested():
            await backfill_slugs(session)
    except IntegrityError:
        pass
    session.expire_all()
    return True


async def list_produk_publik(session: AsyncSession) -> list[ProdukStore]:
    await heal_slugs(session)
    return await list_produk(session, hanya_aktif=True)


async def get_produk_by_ref(session: AsyncSession, ref: str) -> ProdukStore:
    """Public lookup: the URL segment is the slug; old links that carry the id keep working."""
    stmt = (
        select(ProdukStore)
        .where(or_(ProdukStore.slug == ref, ProdukStore.id == ref))
        .options(selectinload(ProdukStore.kategori))
    )
    produk = (await session.execute(stmt)).scalars().first()
    if not produk:
        # A slug that does not exist yet may belong to a product that predates slugs: fill them in and look again.
        if await heal_slugs(session):
            produk = (await session.execute(stmt)).scalars().first()
    if not produk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk tidak ditemukan")
    return produk


async def create_produk(session: AsyncSession, payload: ProdukIn) -> ProdukStore:
    produk = ProdukStore(**payload.model_dump(), slug=await unique_slug(session, payload.nama))
    session.add(produk)
    await session.flush()
    # produk_out() reads produk.kategori; load it here, a lazy load inside the
    # async session would raise MissingGreenlet.
    await session.refresh(produk, attribute_names=["kategori"])
    return produk


async def upsert_produk_dari_erp(
    session: AsyncSession,
    *,
    erp_produk_id: str,
    nama: str,
    deskripsi: str,
    harga: Decimal,
    stok: int,
    platform_asal: str | None,
    foto_key: str | None,
    aktif: bool,
) -> tuple[ProdukStore, bool]:
    """Create or refresh the store copy of an ERP product. Returns
    (produk, dibuat). Stock is store-owned: it is only set when the copy is
    first created, so republishing never overwrites stock the store already
    sold from. A photo is only replaced when a new one was copied."""
    produk = (
        await session.execute(select(ProdukStore).where(ProdukStore.erp_produk_id == erp_produk_id))
    ).scalar_one_or_none()
    dibuat = produk is None
    if produk is None:
        produk = ProdukStore(
            nama=nama,
            slug=await unique_slug(session, nama),
            deskripsi=deskripsi,
            harga=harga,
            stok=max(stok, 0),
            foto_key=foto_key,
            aktif=aktif,
            sumber="erp",
            erp_produk_id=erp_produk_id,
            platform_asal=platform_asal,
        )
        session.add(produk)
    else:
        produk.nama = nama
        produk.deskripsi = deskripsi
        produk.harga = harga
        produk.aktif = aktif
        if platform_asal:
            produk.platform_asal = platform_asal
        if foto_key:
            produk.foto_key = foto_key
    await session.flush()
    await session.refresh(produk, attribute_names=["kategori"])
    return produk, dibuat


async def update_produk(session: AsyncSession, produk_id: str, payload: ProdukPatch) -> ProdukStore:
    produk = await get_produk(session, produk_id)
    fields = payload.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(produk, field, value)
    await session.flush()
    if "kategori_id" in fields:
        # Relationship was eager-loaded before the FK changed -- refresh so
        # produk_out() reflects the new kategori, not the stale cached one.
        await session.refresh(produk, attribute_names=["kategori"])
    return produk


async def set_foto_produk(session: AsyncSession, produk_id: str, foto_key: str) -> ProdukStore:
    produk = await get_produk(session, produk_id)
    produk.foto_key = foto_key
    await session.flush()
    return produk


async def foto_masih_dipakai(session: AsyncSession, foto_key: str) -> bool:
    """True while any product still points at this photo object."""
    total = (
        await session.execute(select(func.count()).select_from(ProdukStore).where(ProdukStore.foto_key == foto_key))
    ).scalar_one()
    return total > 0


async def delete_produk(session: AsyncSession, produk_id: str) -> None:
    produk = await get_produk(session, produk_id)
    await session.delete(produk)


def keranjang_item_out(item: ItemKeranjang) -> dict:
    return {
        "produk_id": item.produk_id,
        "nama": item.produk.nama,
        "harga": str(item.produk.harga),
        "qty": item.qty,
        "subtotal": str(item.produk.harga * item.qty),
        "stok_tersedia": item.produk.stok,
    }


def pesanan_out(pesanan: PesananStore) -> dict:
    return {
        "id": pesanan.id,
        "status": pesanan.status,
        "total": str(pesanan.total),
        "metode_pembayaran": pesanan.metode_pembayaran,
        "created_at": pesanan.created_at.isoformat(),
        "items": [
            {
                "produk_id": it.produk_id,
                "nama_produk": it.nama_produk,
                "harga_satuan": str(it.harga_satuan),
                "qty": it.qty,
                "subtotal": str(it.subtotal),
            }
            for it in pesanan.items
        ],
    }


async def get_keranjang(session: AsyncSession, user_id: str) -> list[ItemKeranjang]:
    stmt = (
        select(ItemKeranjang)
        .where(ItemKeranjang.user_id == user_id)
        .options(selectinload(ItemKeranjang.produk))
    )
    return list((await session.execute(stmt)).scalars().all())


async def tambah_ke_keranjang(session: AsyncSession, user_id: str, produk_id: str, qty: int) -> ItemKeranjang:
    if qty < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Qty minimal 1")
    produk = await get_produk(session, produk_id)
    if not produk.aktif:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Produk tidak tersedia")

    stmt = select(ItemKeranjang).where(ItemKeranjang.user_id == user_id, ItemKeranjang.produk_id == produk_id)
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item:
        item.qty += qty
    else:
        item = ItemKeranjang(user_id=user_id, produk_id=produk_id, qty=qty)
        session.add(item)
    await session.flush()
    await session.refresh(item, attribute_names=["produk"])
    return item


async def ubah_qty_keranjang(session: AsyncSession, user_id: str, produk_id: str, qty: int) -> ItemKeranjang:
    if qty < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Qty minimal 1 (hapus item untuk qty 0)")
    stmt = select(ItemKeranjang).where(ItemKeranjang.user_id == user_id, ItemKeranjang.produk_id == produk_id)
    item = (await session.execute(stmt)).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item keranjang tidak ditemukan")
    item.qty = qty
    await session.flush()
    await session.refresh(item, attribute_names=["produk"])
    return item


async def hapus_dari_keranjang(session: AsyncSession, user_id: str, produk_id: str) -> None:
    stmt = select(ItemKeranjang).where(ItemKeranjang.user_id == user_id, ItemKeranjang.produk_id == produk_id)
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item:
        await session.delete(item)
        await session.flush()


async def checkout(session: AsyncSession, user_id: str) -> PesananStore:
    items = await get_keranjang(session, user_id)
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Keranjang kosong")

    for item in items:
        if not item.produk.aktif:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"Produk '{item.produk.nama}' sudah tidak tersedia"
            )
        if item.produk.stok < item.qty:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Stok '{item.produk.nama}' tidak cukup (tersisa {item.produk.stok})",
            )

    pesanan = PesananStore(user_id=user_id, status="menunggu_pembayaran", total=Decimal("0"))
    session.add(pesanan)
    await session.flush()

    total = Decimal("0")
    for item in items:
        subtotal = item.produk.harga * item.qty
        total += subtotal
        session.add(
            ItemPesanan(
                pesanan_id=pesanan.id,
                produk_id=item.produk_id,
                nama_produk=item.produk.nama,
                harga_satuan=item.produk.harga,
                qty=item.qty,
                subtotal=subtotal,
            )
        )
        # Conditional UPDATE so two concurrent checkouts can never push
        # stock below zero (the in-memory check above can be stale).
        result = await session.execute(
            update(ProdukStore)
            .where(ProdukStore.id == item.produk_id, ProdukStore.stok >= item.qty)
            .values(stok=ProdukStore.stok - item.qty)
        )
        if result.rowcount != 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"Stok '{item.produk.nama}' tidak cukup"
            )
        await session.delete(item)

    pesanan.total = total
    await session.flush()
    await session.refresh(pesanan, attribute_names=["items"])
    return pesanan


async def get_pesanan(session: AsyncSession, pesanan_id: str) -> PesananStore:
    stmt = (
        select(PesananStore)
        .where(PesananStore.id == pesanan_id)
        .options(selectinload(PesananStore.items))
    )
    pesanan = (await session.execute(stmt)).scalar_one_or_none()
    if not pesanan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    return pesanan


async def list_pesanan_milik(session: AsyncSession, user_id: str) -> list[PesananStore]:
    stmt = (
        select(PesananStore)
        .where(PesananStore.user_id == user_id)
        .options(selectinload(PesananStore.items))
        .order_by(PesananStore.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_semua_pesanan(session: AsyncSession) -> list[PesananStore]:
    stmt = select(PesananStore).options(selectinload(PesananStore.items)).order_by(PesananStore.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def ubah_status_pesanan(session: AsyncSession, pesanan_id: str, status_baru: str) -> PesananStore:
    if status_baru not in STATUS_PESANAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status tidak dikenal")
    pesanan = await get_pesanan(session, pesanan_id)
    if status_baru not in _TRANSISI_STATUS.get(pesanan.status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tidak bisa ubah status dari '{pesanan.status}' ke '{status_baru}'",
        )
    pesanan.status = status_baru
    if status_baru == "dibatalkan":
        # checkout() took the stock when the order was placed; give it back.
        for item in pesanan.items:
            await session.execute(
                update(ProdukStore).where(ProdukStore.id == item.produk_id).values(stok=ProdukStore.stok + item.qty)
            )
    await session.flush()
    return pesanan


async def catat_metode_pembayaran(session: AsyncSession, pesanan_id: str, *, metode: str, gateway_ref: str | None) -> PesananStore:
    """Dipanggil setelah adapter payment gateway (mis. iPaymu) berhasil
    membuat sesi pembayaran, sebelum pengguna diarahkan ke checkout_url."""
    pesanan = await get_pesanan(session, pesanan_id)
    pesanan.metode_pembayaran = metode
    pesanan.gateway_ref = gateway_ref
    await session.flush()
    return pesanan


async def tandai_dibayar_dari_webhook(session: AsyncSession, gateway_ref: str) -> PesananStore | None:
    """Dipanggil dari endpoint webhook payment gateway. Mencari pesanan
    berdasarkan gateway_ref (bukan pesanan_id, karena provider tidak selalu
    tahu ID internal kita) lalu jalankan transisi status yang sama seperti
    ubah_status_pesanan -- supaya aturan transisi tetap satu tempat."""
    stmt = select(PesananStore).where(PesananStore.gateway_ref == gateway_ref)
    pesanan = (await session.execute(stmt)).scalar_one_or_none()
    if not pesanan:
        return None
    if "dibayar" in _TRANSISI_STATUS.get(pesanan.status, set()):
        pesanan.status = "dibayar"
        await session.flush()
    return pesanan


def pengiriman_out(pengiriman: PengirimanStore) -> dict:
    return {
        "id": pengiriman.id,
        "pesanan_id": pengiriman.pesanan_id,
        "kurir": pengiriman.kurir,
        "layanan": pengiriman.layanan,
        "ongkir": str(pengiriman.ongkir),
        "nama_penerima": pengiriman.nama_penerima,
        "telepon_penerima": pengiriman.telepon_penerima,
        "alamat_tujuan": pengiriman.alamat_tujuan,
        "kelurahan_tujuan": pengiriman.kelurahan_tujuan,
        "kecamatan_tujuan": pengiriman.kecamatan_tujuan,
        "kota_tujuan": pengiriman.kota_tujuan,
        "provinsi_tujuan": pengiriman.provinsi_tujuan,
        "kode_pos_tujuan": pengiriman.kode_pos_tujuan,
        "kode_wilayah_tujuan": pengiriman.kode_wilayah_tujuan,
        "tracking_id": pengiriman.tracking_id,
        "status": pengiriman.status,
    }


async def buat_pengiriman_lokal(session: AsyncSession, pesanan_id: str, payload: PengirimanIn) -> PengirimanStore:
    """Catat data pengiriman (termasuk ongkir yang dipilih pembeli dari hasil
    cek-ongkir) di database kita. Pemanggilan API Biteship yang sesungguhnya
    (assign kurir, dapat tracking_id) ada di infrastructure/
    shipping_biteship.py -- belum terhubung, lihat docstring di sana."""
    await get_pesanan(session, pesanan_id)
    existing = (
        await session.execute(select(PengirimanStore).where(PengirimanStore.pesanan_id == pesanan_id))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pesanan ini sudah punya data pengiriman")

    pengiriman = PengirimanStore(pesanan_id=pesanan_id, **payload.model_dump())
    session.add(pengiriman)
    await session.flush()
    return pengiriman


async def get_pengiriman(session: AsyncSession, pesanan_id: str) -> PengirimanStore:
    stmt = select(PengirimanStore).where(PengirimanStore.pesanan_id == pesanan_id)
    pengiriman = (await session.execute(stmt)).scalar_one_or_none()
    if not pengiriman:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data pengiriman tidak ditemukan")
    return pengiriman


async def ubah_status_pengiriman(
    session: AsyncSession, pesanan_id: str, status_baru: str, tracking_id: str | None = None
) -> PengirimanStore:
    if status_baru not in STATUS_PENGIRIMAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status pengiriman tidak dikenal")
    pengiriman = await get_pengiriman(session, pesanan_id)
    if status_baru not in _TRANSISI_STATUS_PENGIRIMAN.get(pengiriman.status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tidak bisa ubah status pengiriman dari '{pengiriman.status}' ke '{status_baru}'",
        )
    pengiriman.status = status_baru
    if tracking_id:
        pengiriman.tracking_id = tracking_id
    await session.flush()

    # Status pengiriman & status pesanan disinkronkan otomatis, supaya admin
    # tidak perlu update keduanya terpisah secara manual: pengiriman
    # "dikirim" mendorong pesanan ke "dikirim", dan "diterima" mendorong
    # pesanan ke "selesai" -- masing-masing hanya kalau pesanan sedang di
    # status yang mengizinkan transisi itu (lihat _TRANSISI_STATUS).
    status_pesanan_target = {"dikirim": "dikirim", "diterima": "selesai"}.get(status_baru)
    if status_pesanan_target:
        pesanan = await get_pesanan(session, pesanan_id)
        if status_pesanan_target in _TRANSISI_STATUS.get(pesanan.status, set()):
            pesanan.status = status_pesanan_target
            await session.flush()

    return pengiriman


async def laporan_penjualan(session: AsyncSession, dari: date, sampai: date) -> dict:
    """Total penjualan per hari, dalam rentang [dari, sampai] inklusif.
    Hanya menghitung pesanan berstatus dibayar/diproses/dikirim/selesai --
    lihat _STATUS_TERHITUNG_PENJUALAN."""
    stmt = (
        select(
            func.date(PesananStore.created_at).label("tanggal"),
            func.count(PesananStore.id).label("jumlah_pesanan"),
            func.sum(PesananStore.total).label("total_penjualan"),
        )
        .where(
            PesananStore.status.in_(_STATUS_TERHITUNG_PENJUALAN),
            func.date(PesananStore.created_at) >= dari,
            func.date(PesananStore.created_at) <= sampai,
        )
        .group_by(func.date(PesananStore.created_at))
        .order_by(func.date(PesananStore.created_at))
    )
    rows = (await session.execute(stmt)).all()
    harian = [
        {
            "tanggal": r.tanggal if isinstance(r.tanggal, str) else r.tanggal.isoformat(),
            "jumlah_pesanan": r.jumlah_pesanan,
            "total_penjualan": str(r.total_penjualan or Decimal("0")),
        }
        for r in rows
    ]
    grand_total = sum((Decimal(h["total_penjualan"]) for h in harian), Decimal("0"))
    return {"dari": dari.isoformat(), "sampai": sampai.isoformat(), "harian": harian, "grand_total": str(grand_total)}


async def laporan_produk_terlaris(session: AsyncSession, dari: date, sampai: date, limit: int = 10) -> list[dict]:
    stmt = (
        select(
            ItemPesanan.produk_id,
            ItemPesanan.nama_produk,
            func.sum(ItemPesanan.qty).label("total_qty"),
            func.sum(ItemPesanan.subtotal).label("total_omzet"),
        )
        .join(PesananStore, PesananStore.id == ItemPesanan.pesanan_id)
        .where(
            PesananStore.status.in_(_STATUS_TERHITUNG_PENJUALAN),
            func.date(PesananStore.created_at) >= dari,
            func.date(PesananStore.created_at) <= sampai,
        )
        .group_by(ItemPesanan.produk_id, ItemPesanan.nama_produk)
        .order_by(func.sum(ItemPesanan.qty).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "produk_id": r.produk_id,
            "nama_produk": r.nama_produk,
            "total_qty": r.total_qty,
            "total_omzet": str(r.total_omzet or Decimal("0")),
        }
        for r in rows
    ]


async def laporan_ringkasan_status(session: AsyncSession) -> dict:
    stmt = select(PesananStore.status, func.count(PesananStore.id)).group_by(PesananStore.status)
    rows = (await session.execute(stmt)).all()
    return {status_: jumlah for status_, jumlah in rows}


# --- Kategori ---------------------------------------------------------


def kategori_out(kategori: KategoriStore) -> dict:
    return {"id": kategori.id, "nama": kategori.nama}


async def list_kategori(session: AsyncSession) -> list[KategoriStore]:
    stmt = select(KategoriStore).order_by(KategoriStore.nama)
    return list((await session.execute(stmt)).scalars().all())


async def create_kategori(session: AsyncSession, payload: KategoriIn) -> KategoriStore:
    existing = (
        await session.execute(select(KategoriStore).where(KategoriStore.nama == payload.nama))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Kategori sudah ada")
    kategori = KategoriStore(nama=payload.nama)
    session.add(kategori)
    await session.flush()
    return kategori


async def delete_kategori(session: AsyncSession, kategori_id: str) -> None:
    kategori = await session.get(KategoriStore, kategori_id)
    if not kategori:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kategori tidak ditemukan")
    # Produk yang masih pakai kategori ini otomatis jadi kategori_id=NULL
    # (ON DELETE SET NULL di model) -- tidak ikut terhapus.
    await session.delete(kategori)


# --- Alamat (buku alamat pembeli) --------------------------------------


def alamat_out(alamat: AlamatStore) -> dict:
    return {
        "id": alamat.id,
        "label": alamat.label,
        "nama_penerima": alamat.nama_penerima,
        "telepon_penerima": alamat.telepon_penerima,
        "alamat_lengkap": alamat.alamat_lengkap,
        "kota": alamat.kota,
        "provinsi": alamat.provinsi,
        "kode_pos": alamat.kode_pos,
        "kecamatan": alamat.kecamatan,
        "kelurahan": alamat.kelurahan,
        "kode_wilayah": alamat.kode_wilayah,
        "utama": alamat.utama,
    }


async def list_alamat(session: AsyncSession, user_id: str) -> list[AlamatStore]:
    stmt = select(AlamatStore).where(AlamatStore.user_id == user_id).order_by(AlamatStore.utama.desc(), AlamatStore.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def _unset_other_utama(session: AsyncSession, user_id: str, kecuali_id: str | None = None) -> None:
    stmt = select(AlamatStore).where(AlamatStore.user_id == user_id, AlamatStore.utama.is_(True))
    if kecuali_id:
        stmt = stmt.where(AlamatStore.id != kecuali_id)
    for row in (await session.execute(stmt)).scalars().all():
        row.utama = False


async def create_alamat(session: AsyncSession, user_id: str, payload: AlamatIn) -> AlamatStore:
    is_first = len((await session.execute(select(AlamatStore).where(AlamatStore.user_id == user_id)))
                    .scalars().all()) == 0
    alamat = AlamatStore(user_id=user_id, **payload.model_dump())
    if is_first:
        alamat.utama = True  # alamat pertama otomatis jadi utama
    session.add(alamat)
    await session.flush()
    if alamat.utama:
        await _unset_other_utama(session, user_id, kecuali_id=alamat.id)
        await session.flush()
    return alamat


async def _get_alamat_milik(session: AsyncSession, user_id: str, alamat_id: str) -> AlamatStore:
    alamat = await session.get(AlamatStore, alamat_id)
    if not alamat or alamat.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alamat tidak ditemukan")
    return alamat


async def update_alamat(session: AsyncSession, user_id: str, alamat_id: str, payload: AlamatPatch) -> AlamatStore:
    alamat = await _get_alamat_milik(session, user_id, alamat_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(alamat, field, value)
    await session.flush()
    if alamat.utama:
        await _unset_other_utama(session, user_id, kecuali_id=alamat.id)
        await session.flush()
    return alamat


async def delete_alamat(session: AsyncSession, user_id: str, alamat_id: str) -> None:
    alamat = await _get_alamat_milik(session, user_id, alamat_id)
    await session.delete(alamat)


# --- Login Google --------------------------------------------------------


async def login_or_register_google(session: AsyncSession, *, google_sub: str, email: str, nama: str) -> PembeliStore:
    """Cari user berdasarkan google_sub dulu (identitas stabil), baru fallback
    ke email untuk menautkan akun password biasa yang emailnya sama dengan
    akun Google-nya. Kalau tidak ada keduanya, daftarkan akun baru."""
    user = (
        await session.execute(select(PembeliStore).where(PembeliStore.google_sub == google_sub))
    ).scalar_one_or_none()
    if user:
        return user

    user = (await session.execute(select(PembeliStore).where(PembeliStore.email == email))).scalar_one_or_none()
    if user:
        user.google_sub = google_sub
        await session.flush()
        return user

    user = PembeliStore(nama=nama, email=email, google_sub=google_sub)
    session.add(user)
    await session.flush()
    return user


# --- Chat (support chat per pembeli) -----------------------------------


async def get_or_create_percakapan(session: AsyncSession, user_id: str) -> PercakapanStore:
    stmt = select(PercakapanStore).where(PercakapanStore.user_id == user_id)
    percakapan = (await session.execute(stmt)).scalar_one_or_none()
    if not percakapan:
        percakapan = PercakapanStore(user_id=user_id)
        session.add(percakapan)
        await session.flush()
    return percakapan


async def get_percakapan(session: AsyncSession, percakapan_id: str) -> PercakapanStore:
    stmt = (
        select(PercakapanStore)
        .where(PercakapanStore.id == percakapan_id)
        .options(selectinload(PercakapanStore.pesan), selectinload(PercakapanStore.pembeli))
    )
    percakapan = (await session.execute(stmt)).scalar_one_or_none()
    if not percakapan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Percakapan tidak ditemukan")
    return percakapan


async def list_percakapan_admin(session: AsyncSession) -> list[PercakapanStore]:
    stmt = (
        select(PercakapanStore)
        .options(selectinload(PercakapanStore.pembeli))
        .order_by(PercakapanStore.updated_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def kirim_pesan(
    session: AsyncSession, percakapan_id: str, pengirim_id: str, isi: str, *, sebagai_admin: bool
) -> PesanChatStore:
    percakapan = await session.get(PercakapanStore, percakapan_id)
    if not percakapan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Percakapan tidak ditemukan")
    pesan = PesanChatStore(percakapan_id=percakapan_id, pengirim_id=pengirim_id, pengirim_admin=sebagai_admin, isi=isi)
    session.add(pesan)
    percakapan.unread_admin = not sebagai_admin
    percakapan.unread_pembeli = sebagai_admin
    await session.flush()
    # percakapan.pesan may already sit in the identity map from an earlier
    # selectinload in this session -- expire so the next read includes this
    # new message instead of the cached collection.
    session.expire(percakapan, ["pesan"])
    return pesan


async def tandai_dibaca(session: AsyncSession, percakapan_id: str, sebagai_admin: bool) -> None:
    percakapan = await session.get(PercakapanStore, percakapan_id)
    if not percakapan:
        return
    if sebagai_admin:
        percakapan.unread_admin = False
    else:
        percakapan.unread_pembeli = False
    await session.flush()


def pesan_out(pesan: PesanChatStore) -> dict:
    return {
        "id": pesan.id,
        "pengirim_admin": pesan.pengirim_admin,
        "isi": pesan.isi,
        "created_at": pesan.created_at.isoformat(),
    }


# --- Pengaturan (global settings) --------------------------------------


def pengaturan_out(pengaturan: PengaturanStore) -> dict:
    return {
        "metode_proses_pesanan": pengaturan.metode_proses_pesanan,
        "updated_at": pengaturan.updated_at.isoformat(),
    }


async def get_or_create_pengaturan(session: AsyncSession) -> PengaturanStore:
    pengaturan = await session.get(PengaturanStore, PengaturanStore.SINGLETON_ID)
    if not pengaturan:
        pengaturan = PengaturanStore(id=PengaturanStore.SINGLETON_ID)
        session.add(pengaturan)
        await session.flush()
    return pengaturan


async def update_pengaturan(session: AsyncSession, payload: PengaturanPatch) -> PengaturanStore:
    pengaturan = await get_or_create_pengaturan(session)
    pengaturan.metode_proses_pesanan = payload.metode_proses_pesanan
    await session.flush()
    return pengaturan


def percakapan_out(percakapan: PercakapanStore, *, dengan_pesan: bool = False) -> dict:
    out = {
        "id": percakapan.id,
        "user_id": percakapan.user_id,
        "nama_pembeli": percakapan.pembeli.nama if percakapan.pembeli else None,
        "unread_admin": percakapan.unread_admin,
        "unread_pembeli": percakapan.unread_pembeli,
        "updated_at": percakapan.updated_at.isoformat(),
    }
    if dengan_pesan:
        out["pesan"] = [pesan_out(p) for p in percakapan.pesan]
    return out


# --- Staff (admin accounts) -- owner only, see store_admin_router.py ------


def staff_out(user: AdminStore) -> dict:
    return {**admin_out(user), "created_at": user.created_at.isoformat()}


async def list_staff(session: AsyncSession) -> list[AdminStore]:
    return list((await session.execute(select(AdminStore).order_by(AdminStore.created_at.desc()))).scalars().all())


async def create_staff(session: AsyncSession, payload: "StaffIn") -> AdminStore:
    existing = (await session.execute(select(AdminStore).where(AdminStore.email == payload.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email sudah terdaftar")
    user = AdminStore(
        nama=payload.nama,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=ROLE_ADMIN,
    )
    session.add(user)
    await session.flush()
    return user


async def get_staff(session: AsyncSession, staff_id: str) -> AdminStore:
    user = await session.get(AdminStore, staff_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff tidak ditemukan")
    return user


async def update_staff(session: AsyncSession, staff_id: str, payload: "StaffPatch") -> AdminStore:
    user = await get_staff(session, staff_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await session.flush()
    return user


async def delete_staff(session: AsyncSession, staff_id: str) -> None:
    user = await get_staff(session, staff_id)
    if (user.role or "").strip().lower() == ROLE_OWNER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Akun owner tidak bisa dihapus")
    await session.delete(user)
    await session.flush()
