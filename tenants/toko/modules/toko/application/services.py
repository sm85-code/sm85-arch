from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password, verify_password
from tenants.toko.modules.toko.application.schemas import ProdukIn, ProdukPatch, RegisterRequest
from tenants.toko.modules.toko.infrastructure.models import ProdukToko, UserToko


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
