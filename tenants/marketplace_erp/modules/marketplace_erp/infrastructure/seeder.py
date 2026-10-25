"""Idempotent schema creation + starter owner account for the isolated
marketplace_erp database. Triggered by GET /api/marketplace-erp/seed-now
(gated -- see marketplace_erp_router.authorize_marketplace_erp_seed) and,
for the column self-heal only, by main.py's lifespan via
ensure_marketplace_erp_schema().
"""
from __future__ import annotations

import logging
import os
import re
import secrets

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password, verify_password
from tenants.marketplace_erp.modules.marketplace_erp.application import akses, kategori
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database as mpe_database
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    DEFAULT_GUDANG_KODE,
    Gudang,
    UserMarketplaceErp,
)

logger = logging.getLogger(__name__)

# EmailStr (pydantic) rejects RFC 2606 reserved TLDs (.test/.example/...)
# as "special-use or reserved" -- use a non-reserved placeholder domain so
# this default account can actually log in through the /auth/login schema.
OWNER_EMAIL = "owner@marketplace-erp.internal"
DEFAULT_PASSWORD = "password123"

# Columns added after the table first shipped. create_all() never ALTERs an
# existing table, so Postgres databases created before these columns existed
# are self-healed here. Postgres-only syntax -- skipped on SQLite (tests
# build the schema fresh from metadata anyway).
_SELF_HEAL_COLUMNS = (
    # The AI product-designer playbook is stored as a Kelola Aplikasi text and is longer than 200 characters.
    "ALTER TABLE IF EXISTS mpe_pengaturan_ui ALTER COLUMN teks TYPE TEXT",
    # Kas Kecil: categories paid out of the petty cash (Operasional Eceran) instead of Kas Utama.
    "ALTER TABLE IF EXISTS mpe_kategori_transaksi ADD COLUMN IF NOT EXISTS kas_kecil BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE IF EXISTS mpe_ai_turns ADD COLUMN IF NOT EXISTS context_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS stok_referensi INTEGER",
    "ALTER TABLE IF EXISTS mpe_users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mpe_users "
    "ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
    # Usernames: the email is no longer required (it is an optional contact, also accepted at login).
    "ALTER TABLE IF EXISTS mpe_users ADD COLUMN IF NOT EXISTS username VARCHAR(64)",
    "ALTER TABLE IF EXISTS mpe_users ALTER COLUMN email DROP NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_mpe_users_username ON mpe_users (username)",
    # Tahap 3: manual pengiriman (courier/AWB) fields on the orders inbox.
    "ALTER TABLE IF EXISTS mpe_akun_marketplace ADD COLUMN IF NOT EXISTS terakhir_sinkron_pesanan TIMESTAMPTZ NULL",
    "ALTER TABLE IF EXISTS mpe_akun_marketplace ADD COLUMN IF NOT EXISTS watermark_sinkron_pesanan TIMESTAMPTZ NULL",
    "ALTER TABLE IF EXISTS mpe_akun_marketplace ADD COLUMN IF NOT EXISTS sinkron_penuh_pesanan_at TIMESTAMPTZ NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS status_marketplace VARCHAR(32) NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS metode_pengiriman VARCHAR(16) NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS resi_dicetak_at TIMESTAMPTZ NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS resi_dicetak_oleh VARCHAR(255) NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS kurir VARCHAR(64) NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS nomor_resi VARCHAR(128) NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS tanggal_kirim TIMESTAMPTZ NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS dipesan_at TIMESTAMPTZ NULL",
    "ALTER TABLE IF EXISTS mpe_item_pesanan ADD COLUMN IF NOT EXISTS model_name VARCHAR(255) NOT NULL DEFAULT ''",
    "ALTER TABLE IF EXISTS mpe_katalog_shopee ADD COLUMN IF NOT EXISTS detail_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS detail_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE IF EXISTS mpe_item_pesanan ADD COLUMN IF NOT EXISTS item_sku VARCHAR(128) NOT NULL DEFAULT ''",
    "ALTER TABLE IF EXISTS mpe_item_pesanan ADD COLUMN IF NOT EXISTS model_sku VARCHAR(128) NOT NULL DEFAULT ''",
    "ALTER TABLE IF EXISTS mpe_item_pesanan ADD COLUMN IF NOT EXISTS foto_url VARCHAR(1024) NULL",
    "ALTER TABLE IF EXISTS mpe_item_pesanan ADD COLUMN IF NOT EXISTS item_id_eksternal VARCHAR(32) NULL",
    "ALTER TABLE IF EXISTS mpe_item_pesanan ADD COLUMN IF NOT EXISTS model_id_eksternal VARCHAR(32) NULL",
    "CREATE INDEX IF NOT EXISTS ix_mpe_pesanan_dipesan_at ON mpe_pesanan (dipesan_at)",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS source_katalog_id VARCHAR(64)",
    # Product weight/size and lead time.
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS berat_gram INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS panjang_cm NUMERIC(8,1) NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS lebar_cm NUMERIC(8,1) NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS tinggi_cm NUMERIC(8,1) NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS preorder BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS hari_proses INTEGER NOT NULL DEFAULT 2",
)


async def _self_heal_columns(conn) -> None:
    if conn.dialect.name != "postgresql":
        return
    for stmt in _SELF_HEAL_COLUMNS:
        await conn.execute(text(stmt))


async def _flag_default_password_owner(session: AsyncSession) -> bool:
    """Mark the seeded owner as must_change_password while it still uses
    DEFAULT_PASSWORD. Returns True when a flag was newly set."""
    owner = (
        await session.execute(select(UserMarketplaceErp).where(UserMarketplaceErp.email == OWNER_EMAIL))
    ).scalar_one_or_none()
    if owner is None or owner.must_change_password:
        return False
    if verify_password(DEFAULT_PASSWORD, owner.password_hash):
        owner.must_change_password = True
        await session.flush()
        return True
    return False


async def _ensure_owner(session: AsyncSession) -> UserMarketplaceErp:
    owner = (
        await session.execute(select(UserMarketplaceErp).where(UserMarketplaceErp.email == OWNER_EMAIL))
    ).scalar_one_or_none()
    if owner:
        await _flag_default_password_owner(session)
        return owner
    owner = UserMarketplaceErp(
        nama="Owner",
        email=OWNER_EMAIL,
        password_hash=hash_password(DEFAULT_PASSWORD),
        role="owner",
        must_change_password=True,
        username=None,
    )
    session.add(owner)
    await session.flush()
    return owner


def basis_username(teks: str) -> str:
    """A valid username derived from free text (e.g. the part of an email before the @)."""
    dasar = re.sub(r"[^a-z0-9._-]+", ".", (teks or "").strip().lower()).strip("._-")
    dasar = dasar[:28] or "pengguna"
    if len(dasar) < 3:
        dasar = (dasar + "user")[:6]
    return dasar


async def username_unik(session: AsyncSession, dasar: str) -> str:
    """``dasar`` itself when free, else ``dasar2``, ``dasar3``..."""
    kandidat, nomor = dasar, 1
    while (
        await session.execute(select(UserMarketplaceErp.id).where(UserMarketplaceErp.username == kandidat))
    ).scalar_one_or_none() is not None:
        nomor += 1
        kandidat = f"{dasar[:28]}{nomor}"
    return kandidat


async def _isi_username_kosong(session: AsyncSession) -> int:
    """Accounts from before usernames existed get one from their email (``budi@x.id`` -> ``budi``)."""
    kosong = (
        await session.execute(
            select(UserMarketplaceErp)
            .where(or_(UserMarketplaceErp.username.is_(None), UserMarketplaceErp.username == ""))
            .order_by(UserMarketplaceErp.created_at)
        )
    ).scalars().all()
    for user in kosong:
        user.username = await username_unik(session, basis_username((user.email or user.nama or "").split("@")[0]))
        await session.flush()
    return len(kosong)


def _sandi_awal_admin() -> tuple[str, bool]:
    """(password, was it generated). MARKETPLACE_ERP_ADMIN_PASSWORD sets it; otherwise a random one is made and
    logged once. There is deliberately no fixed default for the most powerful account."""
    dari_env = (os.getenv("MARKETPLACE_ERP_ADMIN_PASSWORD") or "").strip()
    if len(dari_env) >= 8:
        return dari_env, False
    return secrets.token_urlsafe(12), True


async def _ensure_admin(session: AsyncSession) -> UserMarketplaceErp | None:
    """The ERP always has an admin. When there is none, one is created (username ``admin``) with a temporary password
    that must be changed at first login; the owner can then rename it and set its own password. Returns the new
    admin, or None when one already existed."""
    ada = (
        await session.execute(select(UserMarketplaceErp.id).where(UserMarketplaceErp.role == "admin").limit(1))
    ).scalar_one_or_none()
    if ada is not None:
        return None
    sandi, acak = _sandi_awal_admin()
    admin = UserMarketplaceErp(
        nama="Administrator",
        username=await username_unik(session, "admin"),
        email=None,
        password_hash=hash_password(sandi),
        role="admin",
        must_change_password=True,
    )
    session.add(admin)
    await session.flush()
    if acak:
        logger.warning(
            "marketplace_erp: admin account created. username=%s temporary password=%s (shown once; change it at "
            "first login, or set MARKETPLACE_ERP_ADMIN_PASSWORD before the first start)",
            admin.username, sandi,
        )
    else:
        logger.warning("marketplace_erp: admin account created. username=%s (password from MARKETPLACE_ERP_ADMIN_PASSWORD)", admin.username)
    return admin


async def _ensure_default_gudang(session: AsyncSession) -> Gudang:
    gudang = (
        await session.execute(select(Gudang).where(Gudang.kode == DEFAULT_GUDANG_KODE))
    ).scalar_one_or_none()
    if gudang:
        return gudang
    gudang = Gudang(kode=DEFAULT_GUDANG_KODE, nama="Gudang Utama")
    session.add(gudang)
    await session.flush()
    return gudang


async def _create_schema(engine) -> None:
    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Never wait forever for a table lock held by the running instance; the step fails and is retried next start.
            await conn.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
        await _self_heal_columns(conn)
    # Commit additive schema changes even when the encryption key is not ready.
    # Token migration itself remains atomic and fails closed.
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.token_crypto import migrate_token_storage
    async with engine.begin() as conn:
        await migrate_token_storage(conn)


async def ensure_marketplace_erp_schema() -> None:
    """Startup hook (main.py lifespan): create missing tables, add columns
    introduced after first deploy, and flag a still-default-password owner.

    No-op when DATABASE_URL_MARKETPLACE_ERP is not configured.
    """
    engine = mpe_database.engine
    if engine is None or mpe_database.SessionLocal is None:
        return
    await _create_schema(engine)
    async with mpe_database.SessionLocal() as session:
        await _isi_username_kosong(session)
        await _ensure_admin(session)
        await akses.pastikan_bawaan(session)
        await kategori.pastikan_bawaan(session)
        await kapitalkan_katalog_manual(session)
        if await _flag_default_password_owner(session):
            logger.warning(
                "marketplace_erp: seeded owner %s still uses the default password -- "
                "flagged must_change_password", OWNER_EMAIL,
            )
        await session.commit()


KAPITAL_ALASAN = "kapital-katalog-manual-2026-10"


def _kapital(payload: dict) -> dict:
    out = dict(payload)
    for field in ("produk", "sku", "ukuran", "catatan"):
        if isinstance(out.get(field), str):
            out[field] = out[field].upper()
    if isinstance(out.get("varian"), list):
        out["varian"] = [{**v, "nama": str(v.get("nama", "")).upper(), "nilai": str(v.get("nilai", "")).upper()} for v in out["varian"]]
    return out


async def kapitalkan_katalog_manual(session: AsyncSession) -> dict[str, int]:
    """One-time, idempotent: manual (non_kayu) catalog text to capitals, as new entries are saved.

    UPDATE only, never deletes. Each changed row's before-value goes to mpe_processing_price_backup first. Rows that
    would end up identical (name + variants) to another manual product are left as they are and logged.
    """
    import json

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingPrice, ProcessingPriceBackup

    rows = (await session.execute(select(ProcessingPrice).where(ProcessingPrice.kind == "non_kayu"))).scalars().all()
    data = {}
    for r in rows:
        try:
            data[r.id] = json.loads(r.payload_json)
        except ValueError:
            continue
    kunci = lambda p: (str(p.get("produk", "")).upper(), str(p.get("ukuran", "")).upper())  # noqa: E731
    pakai: dict[tuple, list[str]] = {}
    for pid, p in data.items():
        if not p.get("deleted"):
            pakai.setdefault(kunci(p), []).append(pid)
    bentrok = {pid for ids in pakai.values() if len(ids) > 1 for pid in ids}
    changed = 0
    for r in rows:
        p = data.get(r.id)
        if p is None or p.get("deleted"):
            continue
        baru = _kapital(p)
        if baru == p:
            continue
        if r.id in bentrok:
            logger.warning("marketplace_erp: katalog manual %s tidak dikapitalkan, bentrok dengan produk lain bernama sama", r.id)
            continue
        session.add(ProcessingPriceBackup(price_id=r.id, alasan=KAPITAL_ALASAN, payload_json=r.payload_json, version=r.version))
        r.payload_json = json.dumps(baru)
        r.version = r.version + 1
        changed += 1
    if changed or bentrok:
        logger.warning("marketplace_erp: katalog manual dikapitalkan: %d baris, %d dilewati karena bentrok", changed, len(bentrok))
    return {"diubah": changed, "bentrok": len(bentrok)}


async def seed_marketplace_erp(session: AsyncSession) -> dict[str, str]:
    engine = mpe_database.engine
    if engine is None:
        raise RuntimeError("DATABASE_URL_MARKETPLACE_ERP is not configured")
    await _create_schema(engine)

    owner = await _ensure_owner(session)
    await _isi_username_kosong(session)
    await _ensure_admin(session)
    await akses.pastikan_bawaan(session)
    await kategori.pastikan_bawaan(session)
    gudang = await _ensure_default_gudang(session)
    await session.commit()
    return {
        "owner_id": owner.id,
        "owner_email": owner.email,
        "gudang_default_id": gudang.id,
        "gudang_default_kode": gudang.kode,
    }
