"""Idempotent schema creation + starter data for the bumi_lestari database.

seed-now creates the first owner (password from BUMI_LESTARI_SEED_OWNER_PASSWORD --
there is no default password), the default akun kas (incl. kas kecil with plafon
Rp 3.000.000) and default kategori.
"""
from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import database as bl_database
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlSaluran
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    KODE_KAS_KECIL,
    KODE_KAS_UTAMA,
    KODE_SALDO_SHOPEE,
    PLAFON_KAS_KECIL_DEFAULT,
    BlAkunKas,
    BlKategori,
    BlProporsiBagiHasil,
    BlUser,
)

DEFAULT_OWNER_EMAIL = "owner@bumi-lestari.internal"
DEFAULT_ADMIN_EMAIL = "admin@bumi-lestari.internal"

DEFAULT_AKUN = (
    (KODE_KAS_UTAMA, "Kas utama", "kas", None),
    (KODE_SALDO_SHOPEE, "Saldo Shopee", "ewallet", None),
    (KODE_KAS_KECIL, "Kas kecil", "kas_kecil", PLAFON_KAS_KECIL_DEFAULT),
)

DEFAULT_KATEGORI = (
    ("Penjualan marketplace", "pemasukan"),
    ("Penjualan toko web", "pemasukan"),
    ("Penjualan reseller", "pemasukan"),
    ("Pemasukan lain", "pemasukan"),
    ("Biaya produksi (tukang)", "pengeluaran"),
    ("Transport", "pengeluaran"),
    ("Packing", "pengeluaran"),
    ("Operasional", "pengeluaran"),
    ("Prive", "pengeluaran"),
    ("Pengeluaran lain", "pengeluaran"),
)


async def _create_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(BumiLestariBase.metadata.create_all)


async def ensure_bumi_lestari_schema() -> None:
    """Startup hook (main.py lifespan). No-op when DATABASE_URL_BUMI_LESTARI is unset."""
    if bl_database.engine is None:
        return
    await _create_schema(bl_database.engine)


async def seed_bumi_lestari(session: AsyncSession) -> dict[str, str]:
    engine = bl_database.engine
    if engine is None:
        raise RuntimeError("DATABASE_URL_BUMI_LESTARI is not configured")
    await _create_schema(engine)

    email = (os.getenv("BUMI_LESTARI_SEED_OWNER_EMAIL") or DEFAULT_OWNER_EMAIL).strip()
    owner = (await session.execute(select(BlUser).where(BlUser.email == email))).scalar_one_or_none()
    if owner is None:
        password = os.getenv("BUMI_LESTARI_SEED_OWNER_PASSWORD") or ""
        if len(password) < 8:
            raise RuntimeError("BUMI_LESTARI_SEED_OWNER_PASSWORD (min 8 karakter) wajib diisi untuk membuat owner")
        owner = BlUser(
            nama="Owner", email=email, password_hash=hash_password(password), role="owner", must_change_password=True
        )
        session.add(owner)

    # Admin (di atas owner): dibuat hanya bila BUMI_LESTARI_SEED_ADMIN_PASSWORD diisi.
    admin_email = (os.getenv("BUMI_LESTARI_SEED_ADMIN_EMAIL") or DEFAULT_ADMIN_EMAIL).strip()
    admin_password = os.getenv("BUMI_LESTARI_SEED_ADMIN_PASSWORD") or ""
    if len(admin_password) >= 8 and (
        await session.execute(select(BlUser.id).where(BlUser.email == admin_email))
    ).first() is None:
        session.add(
            BlUser(
                nama="Admin", email=admin_email, password_hash=hash_password(admin_password),
                role="admin", must_change_password=True,
            )
        )

    for kode, nama, jenis, plafon in DEFAULT_AKUN:
        if (await session.execute(select(BlAkunKas.id).where(BlAkunKas.kode == kode))).first() is None:
            session.add(BlAkunKas(kode=kode, nama=nama, jenis=jenis, plafon=plafon))
    for nama, jenis in DEFAULT_KATEGORI:
        if (await session.execute(select(BlKategori.id).where(BlKategori.nama == nama))).first() is None:
            session.add(BlKategori(nama=nama, jenis=jenis))
    await session.flush()
    # Saluran awal; saluran lain (Tokopedia, penjual lain, dll.) ditambah lewat POST /saluran.
    if (await session.execute(select(BlSaluran.id))).first() is None:
        shopee = (await session.execute(select(BlAkunKas).where(BlAkunKas.kode == KODE_SALDO_SHOPEE))).scalar_one()
        session.add(BlSaluran(nama="Shopee", jenis="marketplace", akun_id=shopee.id))
        session.add(BlSaluran(nama="Toko web", jenis="web"))
    # Nilai awal proporsi bagi hasil -- hanya dibuat sekali; selanjutnya diubah dari halaman profil UMKM.
    if (await session.execute(select(BlProporsiBagiHasil.id))).first() is None:
        for penerima, persen in (("admin", 40), ("owner", 60)):
            session.add(BlProporsiBagiHasil(penerima=penerima, persen=persen))
    await session.commit()
    return {"owner_email": email, "status": "ok"}

