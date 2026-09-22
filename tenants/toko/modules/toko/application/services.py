from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.security import hash_password, verify_password
from tenants.toko.modules.toko.application.schemas import ProdukIn, ProdukPatch, RegisterRequest
from tenants.toko.modules.toko.infrastructure.models import (
    STATUS_PESANAN,
    ItemKeranjang,
    ItemPesanan,
    PesananToko,
    ProdukToko,
    UserToko,
)

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


def user_out(user: UserToko) -> dict:
    return {"id": user.id, "nama": user.nama, "email": user.email, "role": user.role}


def produk_out(produk: ProdukToko) -> dict:
    return {
        "id": produk.id,
        "nama": produk.nama,
        "deskripsi": produk.deskripsi,
        "kategori": produk.kategori,
        "harga": str(produk.harga),
        "stok": produk.stok,
        "foto_url": produk.foto_url,
        "aktif": produk.aktif,
    }


async def authenticate(session: AsyncSession, email: str, password: str) -> UserToko:
    user = (await session.execute(select(UserToko).where(UserToko.email == email))).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email atau password salah")
    return user


async def register(session: AsyncSession, payload: RegisterRequest) -> UserToko:
    existing = (await session.execute(select(UserToko).where(UserToko.email == payload.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email sudah terdaftar")
    user = UserToko(
        nama=payload.nama,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="pembeli",
    )
    session.add(user)
    await session.flush()
    return user


async def list_produk(session: AsyncSession, *, hanya_aktif: bool = False) -> list[ProdukToko]:
    stmt = select(ProdukToko).order_by(ProdukToko.created_at.desc())
    if hanya_aktif:
        stmt = stmt.where(ProdukToko.aktif.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def get_produk(session: AsyncSession, produk_id: str) -> ProdukToko:
    produk = await session.get(ProdukToko, produk_id)
    if not produk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk tidak ditemukan")
    return produk


async def create_produk(session: AsyncSession, payload: ProdukIn) -> ProdukToko:
    produk = ProdukToko(**payload.model_dump())
    session.add(produk)
    await session.flush()
    return produk


async def update_produk(session: AsyncSession, produk_id: str, payload: ProdukPatch) -> ProdukToko:
    produk = await get_produk(session, produk_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(produk, field, value)
    await session.flush()
    return produk


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


def pesanan_out(pesanan: PesananToko) -> dict:
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


async def checkout(session: AsyncSession, user_id: str) -> PesananToko:
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

    pesanan = PesananToko(user_id=user_id, status="menunggu_pembayaran", total=Decimal("0"))
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
        item.produk.stok -= item.qty
        await session.delete(item)

    pesanan.total = total
    await session.flush()
    await session.refresh(pesanan, attribute_names=["items"])
    return pesanan


async def get_pesanan(session: AsyncSession, pesanan_id: str) -> PesananToko:
    stmt = (
        select(PesananToko)
        .where(PesananToko.id == pesanan_id)
        .options(selectinload(PesananToko.items))
    )
    pesanan = (await session.execute(stmt)).scalar_one_or_none()
    if not pesanan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    return pesanan


async def list_pesanan_milik(session: AsyncSession, user_id: str) -> list[PesananToko]:
    stmt = (
        select(PesananToko)
        .where(PesananToko.user_id == user_id)
        .options(selectinload(PesananToko.items))
        .order_by(PesananToko.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_semua_pesanan(session: AsyncSession) -> list[PesananToko]:
    stmt = select(PesananToko).options(selectinload(PesananToko.items)).order_by(PesananToko.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def ubah_status_pesanan(session: AsyncSession, pesanan_id: str, status_baru: str) -> PesananToko:
    if status_baru not in STATUS_PESANAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status tidak dikenal")
    pesanan = await get_pesanan(session, pesanan_id)
    if status_baru not in _TRANSISI_STATUS.get(pesanan.status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tidak bisa ubah status dari '{pesanan.status}' ke '{status_baru}'",
        )
    pesanan.status = status_baru
    await session.flush()
    return pesanan
