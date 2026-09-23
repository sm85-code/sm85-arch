"""Idempotent starter rows for the isolated toko database."""
from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password
from tenants.toko.modules.erp.infrastructure import models as _erp_models  # noqa: F401 -- registers toko_erp_* tables on TokoBase.metadata
from tenants.toko.modules.toko.infrastructure.database import TokoBase, engine
from tenants.toko.modules.toko.infrastructure.models import UserToko


# NOTE: auth schemas validate email with pydantic's EmailStr, which rejects
# RFC 2606 reserved TLDs (.test/.example/.invalid/.localhost) as
# "special-use or reserved" -- an address on one of those domains can never
# pass login/register validation. Use a non-reserved placeholder domain so
# these default accounts are actually usable.
OWNER_EMAIL = "owner@toko.internal"
ADMIN_EMAIL = "admin@toko.internal"
DEFAULT_PASSWORD = "password123"


async def _ensure_columns(conn) -> None:
    # Columns added after the tables were first created -- ALTER TABLE IF
    # EXISTS/ADD COLUMN IF NOT EXISTS makes this a no-op on a fresh install
    # (create_all already includes them) and safe to re-run on a deploy that
    # already has the old schema.
    await conn.execute(text("ALTER TABLE IF EXISTS toko_users ADD COLUMN IF NOT EXISTS google_sub VARCHAR(255) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS toko_users ALTER COLUMN password_hash DROP NOT NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS toko_produk ADD COLUMN IF NOT EXISTS kategori_id VARCHAR(64) NULL"))
    # Provenance kolom untuk produk yang di-copy dari katalog ERP marketplace
    # (lihat modules/erp/application/services.py::copy_produk_ke_web) --
    # nullable & additive, produk lama/produk toko-web murni tetap NULL.
    await conn.execute(
        text("ALTER TABLE IF EXISTS toko_produk ADD COLUMN IF NOT EXISTS sumber_erp_produk_id VARCHAR(64) NULL")
    )
    await conn.execute(text("ALTER TABLE IF EXISTS toko_produk ADD COLUMN IF NOT EXISTS platform_asal VARCHAR(16) NULL"))
    # akun_id: menautkan baris ERP ke AkunMarketplace (toko_erp_akun) yang
    # baru -- nullable & additive, baris lama tetap NULL, lihat
    # modules/erp/infrastructure/models.py.
    await conn.execute(text("ALTER TABLE IF EXISTS toko_erp_produk ADD COLUMN IF NOT EXISTS akun_id VARCHAR(64) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS toko_erp_pesanan ADD COLUMN IF NOT EXISTS akun_id VARCHAR(64) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS toko_erp_percakapan ADD COLUMN IF NOT EXISTS akun_id VARCHAR(64) NULL"))


async def _migrate_free_text_kategori(session: AsyncSession) -> None:
    """One-time backfill: the old `toko_produk.kategori` free-text column
    (if it still exists from before KategoriToko existed) gets turned into
    real KategoriToko rows, and matching products get their new
    kategori_id set. Safe to re-run -- skips products that already have a
    kategori_id, and does nothing once the old column is gone or empty."""
    has_old_column = (
        await session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'toko_produk' AND column_name = 'kategori'"
            )
        )
    ).scalar_one_or_none()
    if not has_old_column:
        return

    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT kategori FROM toko_produk "
                "WHERE kategori_id IS NULL AND kategori IS NOT NULL AND kategori != ''"
            )
        )
    ).all()
    for (nama,) in rows:
        kategori_id = (
            await session.execute(text("SELECT id FROM toko_kategori WHERE nama = :nama"), {"nama": nama})
        ).scalar_one_or_none()
        if not kategori_id:
            kategori_id = str(uuid.uuid4())
            await session.execute(
                text("INSERT INTO toko_kategori (id, nama) VALUES (:id, :nama)"),
                {"id": kategori_id, "nama": nama},
            )
        await session.execute(
            text("UPDATE toko_produk SET kategori_id = :kid WHERE kategori = :nama AND kategori_id IS NULL"),
            {"kid": kategori_id, "nama": nama},
        )
    await session.flush()


async def _ensure_user(session: AsyncSession, *, email: str, nama: str, role: str) -> UserToko:
    user = (await session.execute(select(UserToko).where(UserToko.email == email))).scalar_one_or_none()
    if not user:
        user = UserToko(nama=nama, email=email, password_hash=hash_password(DEFAULT_PASSWORD), role=role)
        session.add(user)
        await session.flush()
    return user


async def seed_toko(session: AsyncSession) -> dict[str, str]:
    if engine is None:
        raise RuntimeError("DATABASE_URL_TOKO is not configured")
    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)
        await _ensure_columns(conn)

    await _migrate_free_text_kategori(session)

    owner = await _ensure_user(session, email=OWNER_EMAIL, nama="Pemilik Toko", role="owner")
    admin = await _ensure_user(session, email=ADMIN_EMAIL, nama="Admin Toko", role="admin_toko")

    return {"owner_id": owner.id, "admin_id": admin.id}
