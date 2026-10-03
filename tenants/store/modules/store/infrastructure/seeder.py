"""Idempotent schema creation + first owner account for the store database.

Triggered by GET /api/store/admin/seed-now (gated, see store_admin_router).
The owner's password comes from STORE_SEED_OWNER_PASSWORD -- there is no
default password in code, so a forgotten env var can never leave a
well-known login behind.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password
from tenants.store.modules.store.application.slug import slugify, with_suffix
from tenants.store.modules.store.infrastructure.database import StoreBase, engine
from tenants.store.modules.store.infrastructure.models import ROLE_OWNER, AdminStore, ProdukStore

logger = logging.getLogger(__name__)

# EmailStr rejects RFC 2606 reserved TLDs (.test/.example/...); ".internal"
# is accepted, so the placeholder owner can actually log in.
DEFAULT_OWNER_EMAIL = "owner@store.internal"


async def seed_store(session: AsyncSession) -> dict[str, str]:
    if engine is None:
        raise RuntimeError("DATABASE_URL_STORE is not configured")
    password = (os.getenv("STORE_SEED_OWNER_PASSWORD") or "").strip()
    if len(password) < 8:
        raise RuntimeError("STORE_SEED_OWNER_PASSWORD must be set (min 8 characters) to seed the store owner")

    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)

    email = (os.getenv("STORE_SEED_OWNER_EMAIL") or DEFAULT_OWNER_EMAIL).strip().lower()
    owner = (await session.execute(select(AdminStore).where(AdminStore.email == email))).scalar_one_or_none()
    if owner is None:
        owner = AdminStore(nama="Pemilik Toko", email=email, password_hash=hash_password(password), role=ROLE_OWNER)
        session.add(owner)
        await session.flush()
    return {"owner_id": owner.id, "owner_email": owner.email}


async def backfill_slugs(session: AsyncSession) -> int:
    """Give every product that has no slug yet a unique one (oldest first, so the oldest keeps the plain name)."""
    taken = {s for (s,) in (await session.execute(select(ProdukStore.slug).where(ProdukStore.slug.is_not(None)))).all()}
    rows = (
        (await session.execute(select(ProdukStore).where(ProdukStore.slug.is_(None)).order_by(ProdukStore.created_at)))
        .scalars()
        .all()
    )
    for produk in rows:
        base, n = slugify(produk.nama), 1
        while with_suffix(base, n) in taken:
            n += 1
        produk.slug = with_suffix(base, n)
        taken.add(produk.slug)
    await session.flush()
    return len(rows)


async def ensure_store_schema() -> None:
    """Startup hook (main.py lifespan): add what shipped after the first deploy.

    The store has no migration tool, so this mirrors marketplace_erp: idempotent ``IF NOT EXISTS`` statements,
    then a backfill. No-op when DATABASE_URL_STORE is not configured.
    """
    from tenants.store.modules.store.infrastructure import database as store_database

    engine, session_local = store_database.engine, store_database.SessionLocal
    if engine is None or session_local is None:
        return
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
        await conn.execute(text("ALTER TABLE store_produk ADD COLUMN IF NOT EXISTS slug VARCHAR(160)"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_store_produk_slug ON store_produk (slug)"))
        # Structured address (province > city > district > village + Kemendagri village code).
        for table, suffix in (("store_alamat", ""), ("store_pengiriman", "_tujuan")):
            for column, size in (("kecamatan", 128), ("kelurahan", 128), ("kode_wilayah", 16)):
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}{suffix} VARCHAR({size}) NOT NULL DEFAULT ''")
                )
        # Gallery / variants / weight, size and lead time (the new tables come from create_all above).
        for column, ddl in (
            ("berat_gram", "INTEGER NOT NULL DEFAULT 0"),
            ("panjang_cm", "NUMERIC(8,1) NOT NULL DEFAULT 0"),
            ("lebar_cm", "NUMERIC(8,1) NOT NULL DEFAULT 0"),
            ("tinggi_cm", "NUMERIC(8,1) NOT NULL DEFAULT 0"),
            ("preorder", "BOOLEAN NOT NULL DEFAULT false"),
            ("cod", "BOOLEAN NOT NULL DEFAULT false"),
            ("hari_proses", "INTEGER NOT NULL DEFAULT 2"),
        ):
            await conn.execute(text(f"ALTER TABLE store_produk ADD COLUMN IF NOT EXISTS {column} {ddl}"))
        await conn.execute(
            text("ALTER TABLE store_item_keranjang ADD COLUMN IF NOT EXISTS varian_id VARCHAR(64) REFERENCES store_produk_varian(id) ON DELETE CASCADE")
        )
        await conn.execute(text("ALTER TABLE store_item_keranjang DROP CONSTRAINT IF EXISTS uq_store_keranjang_user_produk"))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_store_keranjang_line "
                "ON store_item_keranjang (user_id, produk_id, COALESCE(varian_id, ''))"
            )
        )
        await conn.execute(
            text("ALTER TABLE store_pengiriman ADD COLUMN IF NOT EXISTS layanan_nama VARCHAR(128) NOT NULL DEFAULT ''")
        )
        await conn.execute(text("ALTER TABLE store_pengiriman ADD COLUMN IF NOT EXISTS biaya_cod NUMERIC(20,2) NOT NULL DEFAULT 0"))
        for column, size in (("biteship_order_id", 64), ("biteship_tracking_id", 128)):
            await conn.execute(text(f"ALTER TABLE store_pengiriman ADD COLUMN IF NOT EXISTS {column} VARCHAR({size})"))
        for column, ddl in (
            ("varian_id", "VARCHAR(64) REFERENCES store_produk_varian(id) ON DELETE SET NULL"),
            ("nama_varian", "VARCHAR(120) NOT NULL DEFAULT ''"),
            ("preorder", "BOOLEAN NOT NULL DEFAULT false"),
            ("hari_proses", "INTEGER NOT NULL DEFAULT 2"),
        ):
            await conn.execute(text(f"ALTER TABLE store_item_pesanan ADD COLUMN IF NOT EXISTS {column} {ddl}"))
        for column, ddl in (
            ("lampiran_key", "VARCHAR(255)"),
            ("lampiran_jenis", "VARCHAR(16)"),
            ("produk_id", "VARCHAR(64) REFERENCES store_produk(id) ON DELETE SET NULL"),
        ):
            await conn.execute(text(f"ALTER TABLE store_pesan_chat ADD COLUMN IF NOT EXISTS {column} {ddl}"))
    async with session_local() as session:
        filled = await backfill_slugs(session)
        await session.commit()
    if filled:
        logger.info("store: backfilled %d product slug(s)", filled)
