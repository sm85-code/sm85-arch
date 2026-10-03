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
    MAKS_FOTO_PRODUK,
    ProdukPatch,
    RegisterRequest,
    VarianIn,
    normalisasi_proses,
)

if TYPE_CHECKING:
    from tenants.store.modules.store.application.schemas import StaffIn, StaffPatch
from tenants.store.modules.store.application.slug import slugify, with_suffix
from tenants.store.modules.store.infrastructure.media_storage import media_url
from tenants.store.modules.store.infrastructure.shipping_biteship import ItemKirim, berat_default
from tenants.store.modules.store.infrastructure.shipping_biteship import buat_order as buat_order_kurir
from tenants.store.modules.store.infrastructure.shipping_biteship import lacak as lacak_kurir
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
    FotoProduk,
    ProdukStore,
    PembeliStore,
    VarianProduk,
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


def _foto_keys(produk: ProdukStore) -> list[tuple[str | None, str]]:
    """(foto id, object key) in gallery order. A product that predates the gallery has only its cover key."""
    if produk.foto:
        return [(f.id, f.foto_key) for f in produk.foto]
    return [(None, produk.foto_key)] if produk.foto_key else []


def harga_efektif(produk: ProdukStore, varian: VarianProduk | None) -> Decimal:
    return varian.harga if varian is not None and varian.harga is not None else produk.harga


def stok_total(produk: ProdukStore) -> int:
    """Stock a buyer can still get: the sum of the active variants when there are variants, else the product's own."""
    if produk.varian:
        return sum(v.stok for v in produk.varian if v.aktif)
    return produk.stok


def varian_out(v: VarianProduk, produk: ProdukStore) -> dict:
    foto = next((f for f in produk.foto if f.id == v.foto_id), None)
    return {
        "id": v.id,
        "nama": v.nama,
        "sku": v.sku,
        "harga": str(harga_efektif(produk, v)),
        "harga_sendiri": str(v.harga) if v.harga is not None else None,
        "stok": v.stok,
        "berat_gram": v.berat_gram if v.berat_gram is not None else produk.berat_gram,
        "panjang_cm": str(v.panjang_cm if v.panjang_cm is not None else produk.panjang_cm),
        "lebar_cm": str(v.lebar_cm if v.lebar_cm is not None else produk.lebar_cm),
        "tinggi_cm": str(v.tinggi_cm if v.tinggi_cm is not None else produk.tinggi_cm),
        "berat_gram_sendiri": v.berat_gram,
        "panjang_cm_sendiri": str(v.panjang_cm) if v.panjang_cm is not None else None,
        "lebar_cm_sendiri": str(v.lebar_cm) if v.lebar_cm is not None else None,
        "tinggi_cm_sendiri": str(v.tinggi_cm) if v.tinggi_cm is not None else None,
        "foto_id": v.foto_id,
        "foto_url": media_url(foto.foto_key) if foto else None,
        "aktif": v.aktif,
    }


def produk_out(produk: ProdukStore) -> dict:
    keys = _foto_keys(produk)
    aktif = [v for v in produk.varian if v.aktif]
    harga_semua = [harga_efektif(produk, v) for v in aktif] or [produk.harga]
    return {
        "id": produk.id,
        "slug": produk.slug,
        "nama": produk.nama,
        "deskripsi": produk.deskripsi,
        "kategori_id": produk.kategori_id,
        "kategori_nama": produk.kategori.nama if produk.kategori else None,
        "harga": str(produk.harga),
        "harga_min": str(min(harga_semua)),
        "harga_max": str(max(harga_semua)),
        "stok": stok_total(produk),
        "foto_key": produk.foto_key,
        "foto_url": media_url(produk.foto_key),
        "foto": [{"id": fid, "url": media_url(key)} for fid, key in keys],
        "aktif": produk.aktif,
        "sumber": produk.sumber,
        "berat_gram": produk.berat_gram,
        "panjang_cm": str(produk.panjang_cm),
        "lebar_cm": str(produk.lebar_cm),
        "tinggi_cm": str(produk.tinggi_cm),
        "preorder": produk.preorder,
        "hari_proses": produk.hari_proses,
        "varian": [varian_out(v, produk) for v in produk.varian],
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


_MUAT_PRODUK = (
    selectinload(ProdukStore.kategori),
    selectinload(ProdukStore.foto),
    selectinload(ProdukStore.varian),
)


async def list_produk(session: AsyncSession, *, hanya_aktif: bool = False) -> list[ProdukStore]:
    stmt = select(ProdukStore).options(*_MUAT_PRODUK).order_by(ProdukStore.created_at.desc())
    if hanya_aktif:
        stmt = stmt.where(ProdukStore.aktif.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def get_produk(session: AsyncSession, produk_id: str) -> ProdukStore:
    stmt = select(ProdukStore).where(ProdukStore.id == produk_id).options(*_MUAT_PRODUK)
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
        .options(*_MUAT_PRODUK)
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
    await session.refresh(produk, attribute_names=["kategori", "foto", "varian"])
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
    berat_gram: int | None = None,
    panjang_cm: Decimal | None = None,
    lebar_cm: Decimal | None = None,
    tinggi_cm: Decimal | None = None,
    preorder: bool | None = None,
    hari_proses: int | None = None,
) -> tuple[ProdukStore, bool]:
    """Create or refresh the store copy of an ERP product. Returns
    (produk, dibuat). Stock is store-owned: it is only set when the copy is
    first created, so republishing never overwrites stock the store already
    sold from. A photo is only added when a new one was copied; weight, size and
    lead time follow the ERP product when it has them (an unset ERP value leaves
    what the store already has)."""
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
        await session.flush()
        if foto_key:
            session.add(FotoProduk(produk_id=produk.id, foto_key=foto_key, urutan=0))
    else:
        produk.nama = nama
        produk.deskripsi = deskripsi
        produk.harga = harga
        produk.aktif = aktif
        if platform_asal:
            produk.platform_asal = platform_asal
        if foto_key:
            await session.refresh(produk, attribute_names=["foto"])
            await _tambah_foto_ke_galeri(session, produk, foto_key, melewati_batas=False)
    for nama_field, nilai in (
        ("berat_gram", berat_gram), ("panjang_cm", panjang_cm), ("lebar_cm", lebar_cm), ("tinggi_cm", tinggi_cm),
    ):
        if nilai is not None and nilai > 0:
            setattr(produk, nama_field, nilai)
    if preorder is not None:
        produk.preorder = preorder
        produk.hari_proses = normalisasi_proses(preorder, hari_proses if hari_proses is not None else produk.hari_proses)
    await session.flush()
    await session.refresh(produk, attribute_names=["kategori", "foto", "varian"])
    return produk, dibuat


async def update_produk(session: AsyncSession, produk_id: str, payload: ProdukPatch) -> ProdukStore:
    produk = await get_produk(session, produk_id)
    fields = payload.model_dump(exclude_unset=True)
    if "preorder" in fields or "hari_proses" in fields:
        preorder = fields.get("preorder", produk.preorder)
        try:
            fields["hari_proses"] = normalisasi_proses(preorder, fields.get("hari_proses", produk.hari_proses))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        fields["preorder"] = preorder
    for field, value in fields.items():
        setattr(produk, field, value)
    await session.flush()
    if "kategori_id" in fields:
        # Relationship was eager-loaded before the FK changed -- refresh so
        # produk_out() reflects the new kategori, not the stale cached one.
        await session.refresh(produk, attribute_names=["kategori"])
    return produk


async def _urut_berikut(produk: ProdukStore) -> int:
    return max((f.urutan for f in produk.foto), default=-1) + 1


async def _tambah_foto_ke_galeri(
    session: AsyncSession, produk: ProdukStore, foto_key: str, *, melewati_batas: bool = True
) -> FotoProduk:
    """Append a photo. A cover that predates the gallery is first made a gallery row so it is not lost."""
    if not produk.foto and produk.foto_key and produk.foto_key != foto_key:
        session.add(FotoProduk(produk_id=produk.id, foto_key=produk.foto_key, urutan=0))
        await session.flush()
        await session.refresh(produk, attribute_names=["foto"])
    if melewati_batas and len(produk.foto) >= MAKS_FOTO_PRODUK:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Maksimal {MAKS_FOTO_PRODUK} foto per produk")
    if any(f.foto_key == foto_key for f in produk.foto):
        return next(f for f in produk.foto if f.foto_key == foto_key)
    foto = FotoProduk(produk_id=produk.id, foto_key=foto_key, urutan=await _urut_berikut(produk))
    session.add(foto)
    await session.flush()
    await session.refresh(produk, attribute_names=["foto"])
    produk.foto_key = produk.foto[0].foto_key
    await session.flush()
    return foto


async def pastikan_bisa_tambah_foto(session: AsyncSession, produk_id: str) -> ProdukStore:
    """Checked before the upload so a full gallery never leaves an orphan object in storage."""
    produk = await get_produk(session, produk_id)
    jumlah = len(produk.foto) if produk.foto else (1 if produk.foto_key else 0)
    if jumlah >= MAKS_FOTO_PRODUK:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Maksimal {MAKS_FOTO_PRODUK} foto per produk")
    return produk


async def tambah_foto(session: AsyncSession, produk_id: str, foto_key: str) -> ProdukStore:
    produk = await get_produk(session, produk_id)
    await _tambah_foto_ke_galeri(session, produk, foto_key)
    await session.refresh(produk, attribute_names=["foto", "varian"])
    return produk


async def hapus_foto(session: AsyncSession, produk_id: str, foto_id: str) -> tuple[ProdukStore, str]:
    """Remove one photo; returns the object key so the caller can delete it from storage after commit."""
    produk = await get_produk(session, produk_id)
    foto = next((f for f in produk.foto if f.id == foto_id), None)
    if foto is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Foto tidak ditemukan")
    key = foto.foto_key
    await session.delete(foto)
    await session.flush()
    await session.refresh(produk, attribute_names=["foto", "varian"])
    produk.foto_key = produk.foto[0].foto_key if produk.foto else None
    await session.flush()
    return produk, key


async def urutkan_foto(session: AsyncSession, produk_id: str, urutan_id: list[str]) -> ProdukStore:
    produk = await get_produk(session, produk_id)
    ada = {f.id for f in produk.foto}
    if set(urutan_id) != ada or len(urutan_id) != len(ada):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Daftar foto tidak sesuai")
    posisi = {fid: i for i, fid in enumerate(urutan_id)}
    for f in produk.foto:
        f.urutan = posisi[f.id]
    await session.flush()
    await session.refresh(produk, attribute_names=["foto", "varian"])
    produk.foto_key = produk.foto[0].foto_key if produk.foto else None
    await session.flush()
    return produk


async def ganti_varian(session: AsyncSession, produk_id: str, items: list[VarianIn]) -> ProdukStore:
    """Replace the whole variant list. Rows with a known id are updated, new ones created, missing ones removed."""
    produk = await get_produk(session, produk_id)
    nama = [v.nama.strip().lower() for v in items]
    if len(set(nama)) != len(nama):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nama varian tidak boleh sama")
    foto_ids = {f.id for f in produk.foto}
    for v in items:
        if v.foto_id and v.foto_id not in foto_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Foto varian bukan milik produk ini")
    ada = {v.id: v for v in produk.varian}
    dipakai = {v.id for v in items if v.id}
    # Drop removed variants first so a rename never trips the unique (produk_id, nama) constraint.
    for row in list(produk.varian):
        if row.id not in dipakai:
            await session.delete(row)
    await session.flush()
    for i, v in enumerate(items):
        row = ada.get(v.id) if v.id else None
        if v.id and row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Varian tidak ditemukan")
        if row is None:
            row = VarianProduk(produk_id=produk.id)
            session.add(row)
        row.nama = v.nama.strip()
        row.sku = v.sku
        row.harga = v.harga
        row.stok = v.stok
        row.berat_gram = v.berat_gram
        row.panjang_cm = v.panjang_cm
        row.lebar_cm = v.lebar_cm
        row.tinggi_cm = v.tinggi_cm
        row.foto_id = v.foto_id
        row.aktif = v.aktif
        row.urutan = i
    await session.flush()
    await session.refresh(produk, attribute_names=["varian"])
    return produk


async def foto_masih_dipakai(session: AsyncSession, foto_key: str) -> bool:
    """True while any product or gallery row still points at this photo object."""
    total = (
        await session.execute(select(func.count()).select_from(ProdukStore).where(ProdukStore.foto_key == foto_key))
    ).scalar_one()
    total += (
        await session.execute(select(func.count()).select_from(FotoProduk).where(FotoProduk.foto_key == foto_key))
    ).scalar_one()
    return total > 0


async def delete_produk(session: AsyncSession, produk_id: str) -> list[str]:
    """Delete a product; returns every photo key it owned so the caller can clean storage after commit."""
    produk = await get_produk(session, produk_id)
    keys = [key for _, key in _foto_keys(produk)]
    await session.delete(produk)
    return keys


def keranjang_item_out(item: ItemKeranjang) -> dict:
    harga = harga_efektif(item.produk, item.varian)
    return {
        "id": item.id,
        "produk_id": item.produk_id,
        "varian_id": item.varian_id,
        "nama": item.produk.nama,
        "nama_varian": item.varian.nama if item.varian else None,
        "harga": str(harga),
        "qty": item.qty,
        "subtotal": str(harga * item.qty),
        "stok_tersedia": item.varian.stok if item.varian else item.produk.stok,
        "preorder": item.produk.preorder,
        "hari_proses": item.produk.hari_proses,
        "foto_url": media_url(item.produk.foto_key),
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
                "varian_id": it.varian_id,
                "nama_produk": it.nama_produk,
                "nama_varian": it.nama_varian or None,
                "harga_satuan": str(it.harga_satuan),
                "qty": it.qty,
                "subtotal": str(it.subtotal),
                "preorder": it.preorder,
                "hari_proses": it.hari_proses,
            }
            for it in pesanan.items
        ],
    }


async def get_keranjang(session: AsyncSession, user_id: str) -> list[ItemKeranjang]:
    stmt = (
        select(ItemKeranjang)
        .where(ItemKeranjang.user_id == user_id)
        .options(selectinload(ItemKeranjang.produk), selectinload(ItemKeranjang.varian))
    )
    return list((await session.execute(stmt)).scalars().all())


async def _muat_item(session: AsyncSession, item: ItemKeranjang) -> ItemKeranjang:
    await session.flush()
    await session.refresh(item, attribute_names=["produk", "varian"])
    return item


async def _cari_item(session: AsyncSession, user_id: str, ref: str) -> ItemKeranjang | None:
    """A cart line is addressed by its own id; the product id still works for a product without variants."""
    base = select(ItemKeranjang).where(ItemKeranjang.user_id == user_id)
    item = (await session.execute(base.where(ItemKeranjang.id == ref))).scalar_one_or_none()
    if item is None:
        item = (
            await session.execute(base.where(ItemKeranjang.produk_id == ref, ItemKeranjang.varian_id.is_(None)))
        ).scalar_one_or_none()
    return item


async def tambah_ke_keranjang(
    session: AsyncSession, user_id: str, produk_id: str, qty: int, varian_id: str | None = None
) -> ItemKeranjang:
    if qty < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Qty minimal 1")
    produk = await get_produk(session, produk_id)
    if not produk.aktif:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Produk tidak tersedia")
    varian = None
    if produk.varian:
        varian = next((v for v in produk.varian if v.id == varian_id), None) if varian_id else None
        if varian is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pilih varian terlebih dahulu")
        if not varian.aktif:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Varian tidak tersedia")
    elif varian_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Varian tidak ditemukan")

    stmt = select(ItemKeranjang).where(ItemKeranjang.user_id == user_id, ItemKeranjang.produk_id == produk_id)
    stmt = stmt.where(ItemKeranjang.varian_id == varian.id if varian else ItemKeranjang.varian_id.is_(None))
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item:
        item.qty += qty
    else:
        item = ItemKeranjang(user_id=user_id, produk_id=produk_id, varian_id=varian.id if varian else None, qty=qty)
        session.add(item)
    return await _muat_item(session, item)


async def ubah_qty_keranjang(session: AsyncSession, user_id: str, ref: str, qty: int) -> ItemKeranjang:
    if qty < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Qty minimal 1 (hapus item untuk qty 0)")
    item = await _cari_item(session, user_id, ref)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item keranjang tidak ditemukan")
    item.qty = qty
    return await _muat_item(session, item)


async def hapus_dari_keranjang(session: AsyncSession, user_id: str, ref: str) -> None:
    item = await _cari_item(session, user_id, ref)
    if item:
        await session.delete(item)
        await session.flush()


def _nama_baris(item: ItemKeranjang) -> str:
    return f"{item.produk.nama} ({item.varian.nama})" if item.varian else item.produk.nama


async def checkout(session: AsyncSession, user_id: str) -> PesananStore:
    items = await get_keranjang(session, user_id)
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Keranjang kosong")

    for item in items:
        stok = item.varian.stok if item.varian else item.produk.stok
        if not item.produk.aktif or (item.varian is not None and not item.varian.aktif):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"Produk '{_nama_baris(item)}' sudah tidak tersedia"
            )
        if stok < item.qty:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Stok '{_nama_baris(item)}' tidak cukup (tersisa {stok})",
            )

    pesanan = PesananStore(user_id=user_id, status="menunggu_pembayaran", total=Decimal("0"))
    session.add(pesanan)
    await session.flush()

    total = Decimal("0")
    for item in items:
        harga = harga_efektif(item.produk, item.varian)
        subtotal = harga * item.qty
        total += subtotal
        session.add(
            ItemPesanan(
                pesanan_id=pesanan.id,
                produk_id=item.produk_id,
                varian_id=item.varian_id,
                nama_produk=item.produk.nama,
                nama_varian=item.varian.nama if item.varian else "",
                harga_satuan=harga,
                qty=item.qty,
                subtotal=subtotal,
                preorder=item.produk.preorder,
                hari_proses=item.produk.hari_proses,
            )
        )
        # Conditional UPDATE so two concurrent checkouts can never push
        # stock below zero (the in-memory check above can be stale).
        if item.varian is not None:
            result = await session.execute(
                update(VarianProduk)
                .where(VarianProduk.id == item.varian_id, VarianProduk.stok >= item.qty)
                .values(stok=VarianProduk.stok - item.qty)
            )
        else:
            result = await session.execute(
                update(ProdukStore)
                .where(ProdukStore.id == item.produk_id, ProdukStore.stok >= item.qty)
                .values(stok=ProdukStore.stok - item.qty)
            )
        if result.rowcount != 1:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Stok '{_nama_baris(item)}' tidak cukup")
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
            if item.varian_id:
                # A variant deleted since the order was placed has nothing left to restock.
                await session.execute(
                    update(VarianProduk).where(VarianProduk.id == item.varian_id).values(stok=VarianProduk.stok + item.qty)
                )
            else:
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


async def tandai_dibayar_pesanan(session: AsyncSession, pesanan_id: str, jumlah: Decimal) -> PesananStore | None:
    """Mark an order paid after the payment gateway itself confirmed ``jumlah`` for it.

    Returns None for an unknown order. An amount below the order total never marks it paid (the order is
    returned unchanged); calling it again for an already paid order is harmless (the status flow allows
    the transition once)."""
    pesanan = await session.get(PesananStore, pesanan_id)
    if not pesanan:
        return None
    if jumlah < pesanan.total:
        return pesanan
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
        "layanan_nama": pengiriman.layanan_nama,
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
        "biteship": bool(pengiriman.biteship_order_id),
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
    # The order total is items + shipping; the price was set by the server, never by the buyer.
    pesanan = await get_pesanan(session, pesanan_id)
    if pesanan.status == "menunggu_pembayaran":  # once paid, the amount charged is fixed
        pesanan.total = sum((it.subtotal for it in pesanan.items), Decimal("0")) + payload.ongkir
    await session.flush()
    return pengiriman


def _item_kirim(produk: ProdukStore, varian: VarianProduk | None, qty: int, harga: Decimal, nama: str) -> ItemKirim:
    def pilih(nama_kolom: str):
        nilai = getattr(varian, nama_kolom) if varian is not None else None
        return nilai if nilai else getattr(produk, nama_kolom)

    return ItemKirim(
        nama=nama,
        nilai=int(harga),
        qty=qty,
        berat_gram=int(pilih("berat_gram") or 0) or berat_default(),
        panjang_cm=int(pilih("panjang_cm") or 0),
        lebar_cm=int(pilih("lebar_cm") or 0),
        tinggi_cm=int(pilih("tinggi_cm") or 0),
    )


async def item_kirim_keranjang(session: AsyncSession, user_id: str) -> list[ItemKirim]:
    return [
        _item_kirim(it.produk, it.varian, it.qty, harga_efektif(it.produk, it.varian), _nama_baris(it))
        for it in await get_keranjang(session, user_id)
    ]


async def item_kirim_pesanan(session: AsyncSession, pesanan: PesananStore) -> list[ItemKirim]:
    hasil: list[ItemKirim] = []
    for it in pesanan.items:
        produk = await session.get(ProdukStore, it.produk_id)
        if produk is None:
            continue
        varian = await session.get(VarianProduk, it.varian_id) if it.varian_id else None
        hasil.append(_item_kirim(produk, varian, it.qty, it.harga_satuan, it.nama_produk))
    return hasil


async def get_pengiriman(session: AsyncSession, pesanan_id: str) -> PengirimanStore:
    stmt = select(PengirimanStore).where(PengirimanStore.pesanan_id == pesanan_id)
    pengiriman = (await session.execute(stmt)).scalar_one_or_none()
    if not pengiriman:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data pengiriman tidak ditemukan")
    return pengiriman


# Biteship's status -> the path our own shipment status has to walk (see _TRANSISI_STATUS_PENGIRIMAN).
_JALUR_STATUS_BITESHIP = {
    "picked": ["dikirim"],
    "dropping_off": ["dikirim"],
    "delivered": ["dikirim", "diterima"],
    "on_hold": ["bermasalah"],
    "return_in_transit": ["bermasalah"],
    "returned": ["bermasalah"],
    "rejected": ["bermasalah"],
    "disposed": ["bermasalah"],
    "courier_not_found": ["bermasalah"],
    "cancelled": ["bermasalah"],
}


async def buat_order_biteship(session: AsyncSession, pesanan_id: str) -> PengirimanStore:
    """Book the courier for a paid order and keep the waybill. Safe to press twice: a second call is refused."""
    pesanan = await get_pesanan(session, pesanan_id)
    if pesanan.status not in ("dibayar", "diproses"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Pengiriman hanya bisa dibuat untuk pesanan yang sudah dibayar"
        )
    pengiriman = await get_pengiriman(session, pesanan_id)
    if pengiriman.biteship_order_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pengiriman ini sudah dibuat di Biteship")
    order = await buat_order_kurir(
        kurir=pengiriman.kurir,
        layanan=pengiriman.layanan,
        nama_penerima=pengiriman.nama_penerima,
        telepon_penerima=pengiriman.telepon_penerima,
        alamat_tujuan=pengiriman.alamat_tujuan,
        kode_pos_tujuan=pengiriman.kode_pos_tujuan,
        items=await item_kirim_pesanan(session, pesanan),
        catatan=f"Pesanan {pesanan.id[:8]}",
    )
    pengiriman.biteship_order_id = order.order_id
    pengiriman.biteship_tracking_id = order.tracking_id or None
    pengiriman.tracking_id = order.waybill_id or pengiriman.tracking_id
    if "diproses" in _TRANSISI_STATUS.get(pesanan.status, set()):
        pesanan.status = "diproses"
    await session.flush()
    return pengiriman


async def lacak_pengiriman(session: AsyncSession, pesanan_id: str) -> dict:
    """Ask Biteship where the parcel is and bring our own shipment status in line with the answer."""
    pengiriman = await get_pengiriman(session, pesanan_id)
    if not pengiriman.biteship_tracking_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Belum ada pelacakan untuk pengiriman ini")
    hasil = await lacak_kurir(pengiriman.biteship_tracking_id)
    if hasil.waybill_id and hasil.waybill_id != pengiriman.tracking_id:
        pengiriman.tracking_id = hasil.waybill_id
    for langkah in _JALUR_STATUS_BITESHIP.get(hasil.status, []):
        if pengiriman.status == langkah:
            continue
        if langkah not in _TRANSISI_STATUS_PENGIRIMAN.get(pengiriman.status, set()):
            break
        pengiriman = await ubah_status_pengiriman(session, pesanan_id, langkah)
    await session.flush()
    return {
        "status_kurir": hasil.status,
        "status": pengiriman.status,
        "tracking_id": pengiriman.tracking_id,
        "riwayat": [{"status": r.status, "catatan": r.catatan, "waktu": r.waktu} for r in hasil.riwayat],
    }


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
        .options(
            selectinload(PercakapanStore.pesan).selectinload(PesanChatStore.produk),
            selectinload(PercakapanStore.pembeli),
        )
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
    session: AsyncSession,
    percakapan_id: str,
    pengirim_id: str,
    isi: str,
    *,
    sebagai_admin: bool,
    produk_id: str | None = None,
    lampiran_key: str | None = None,
    lampiran_jenis: str | None = None,
) -> PesanChatStore:
    percakapan = await session.get(PercakapanStore, percakapan_id)
    if not percakapan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Percakapan tidak ditemukan")
    if produk_id and await session.get(ProdukStore, produk_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk tidak ditemukan")
    pesan = PesanChatStore(
        percakapan_id=percakapan_id,
        pengirim_id=pengirim_id,
        pengirim_admin=sebagai_admin,
        isi=isi,
        produk_id=produk_id,
        lampiran_key=lampiran_key,
        lampiran_jenis=lampiran_jenis,
    )
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
    produk = pesan.produk
    return {
        "id": pesan.id,
        "pengirim_admin": pesan.pengirim_admin,
        "isi": pesan.isi,
        "created_at": pesan.created_at.isoformat(),
        "lampiran": (
            {"jenis": pesan.lampiran_jenis, "url": media_url(pesan.lampiran_key)} if pesan.lampiran_key else None
        ),
        # A product card; None when nothing was shared or the product has since been deleted.
        "produk": (
            {
                "id": produk.id,
                "slug": produk.slug,
                "nama": produk.nama,
                "harga": str(produk.harga),
                "foto_url": media_url(produk.foto_key),
            }
            if produk
            else None
        ),
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
