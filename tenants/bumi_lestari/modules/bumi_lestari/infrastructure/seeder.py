"""Idempotent schema creation + starter data for the bumi_lestari database.

seed-now creates the first owner (password from BUMI_LESTARI_SEED_OWNER_PASSWORD --
there is no default password), the default akun kas (incl. kas kecil with plafon
Rp 3.000.000) and default kategori.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import database as bl_database
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlSaluran
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_pembayaran  # noqa: F401  (register tables)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pembayaran import BlLangganan
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    KODE_DANA_CADANGAN,
    KODE_KAS_IKLAN,
    KODE_KAS_KECIL,
    KODE_KAS_UTAMA,
    KODE_SALDO_SHOPEE,
    PLAFON_KAS_IKLAN_DEFAULT,
    PLAFON_KAS_KECIL_DEFAULT,
    BlAkunKas,
    BlKategori,
    BlProporsiBagiHasil,
    BlTransaksi,
    BlUser,
)

DEFAULT_OWNER_EMAIL = "owner@bumi-lestari.internal"
DEFAULT_ADMIN_EMAIL = "admin@bumi-lestari.internal"

DEFAULT_AKUN = (
    (KODE_KAS_UTAMA, "Kas utama", "kas", None),
    (KODE_SALDO_SHOPEE, "Saldo Shopee", "ewallet", None),
    (KODE_KAS_KECIL, "Kas kecil", "kas_kecil", PLAFON_KAS_KECIL_DEFAULT),
    (KODE_KAS_IKLAN, "Kas iklan", "kas_iklan", PLAFON_KAS_IKLAN_DEFAULT),
    (KODE_DANA_CADANGAN, "Dana cadangan (gaji & langganan)", "kas", None),
)

DEFAULT_LANGGANAN = ("Listrik", "Air", "Wifi", "Kebersihan", "Iuran BUMDES", "Langganan Komplace")

DEFAULT_KATEGORI = (
    ("Penjualan marketplace", "pemasukan"),
    ("Penjualan toko web", "pemasukan"),
    ("Penjualan reseller", "pemasukan"),
    ("Pemasukan lain", "pemasukan"),
    ("Biaya produksi / pembelian barang", "pengeluaran"),
    ("Gaji karyawan", "pengeluaran"),
    ("Bagi hasil", "pengeluaran"),
    ("Transport", "pengeluaran"),
    ("Packing", "pengeluaran"),
    ("Operasional", "pengeluaran"),
    ("Biaya iklan", "pengeluaran"),
    ("Langganan & utilitas", "pengeluaran"),
    ("Prive", "pengeluaran"),
    ("Pengeluaran lain", "pengeluaran"),
    # Fase 1 (spesifikasi 6.2)
    ("Setoran modal", "pemasukan"),
    ("Biaya marketplace", "pengeluaran"),
    ("Kerugian retur", "pengeluaran"),
)

# Entri pembuka modal Owner (spesifikasi AB-MD-1): dicatat sekali oleh seed.
SETORAN_MODAL_AWAL = Decimal("20000000")
TANGGAL_SETORAN_MODAL_AWAL = date(2026, 9, 1)

# Kolom yang ditambahkan setelah tabel pertama kali dibuat. create_all tidak mengubah tabel yang sudah ada,
# jadi Postgres yang sudah berjalan disusulkan lewat ALTER idempoten (pola sama dengan tenant store).
_ALTER_POSTGRES = (
    "ALTER TABLE bl_users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS status_kirim VARCHAR(16) NOT NULL DEFAULT 'terkirim'",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS kiriman_id VARCHAR(64)",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS sumber_sistem VARCHAR(32)",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS sumber_ref VARCHAR(255)",
    "ALTER TABLE bl_pembayaran_pemasok ADD COLUMN IF NOT EXISTS status_kirim VARCHAR(16) NOT NULL DEFAULT 'terkirim'",
    "ALTER TABLE bl_pembayaran_pemasok ADD COLUMN IF NOT EXISTS kiriman_id VARCHAR(64)",
    "ALTER TABLE bl_penerimaan_reseller ADD COLUMN IF NOT EXISTS status_kirim VARCHAR(16) NOT NULL DEFAULT 'terkirim'",
    "ALTER TABLE bl_penerimaan_reseller ADD COLUMN IF NOT EXISTS kiriman_id VARCHAR(64)",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS sumber_sistem VARCHAR(32)",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS sumber_ref VARCHAR(255)",
    "ALTER TABLE bl_produk ADD COLUMN IF NOT EXISTS sumber_sistem VARCHAR(32)",
    "ALTER TABLE bl_produk ADD COLUMN IF NOT EXISTS sumber_ref VARCHAR(255)",
    "CREATE INDEX IF NOT EXISTS ix_bl_transaksi_status_kirim ON bl_transaksi (status_kirim)",
    "CREATE INDEX IF NOT EXISTS ix_bl_transaksi_kiriman_id ON bl_transaksi (kiriman_id)",
    "CREATE INDEX IF NOT EXISTS ix_bl_pembayaran_pemasok_kiriman_id ON bl_pembayaran_pemasok (kiriman_id)",
    "CREATE INDEX IF NOT EXISTS ix_bl_penerimaan_reseller_kiriman_id ON bl_penerimaan_reseller (kiriman_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bl_transaksi_sumber ON bl_transaksi (sumber_sistem, sumber_ref)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bl_order_sumber ON bl_order (sumber_sistem, sumber_ref)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bl_produk_sumber ON bl_produk (sumber_sistem, sumber_ref)",
)


async def _self_heal_columns(conn) -> None:
    """Tambahkan kolom/indeks baru ke tabel lama (Postgres; idempoten)."""
    if conn.dialect.name != "postgresql":
        return
    for stmt in _ALTER_POSTGRES:
        await conn.execute(text(stmt))


async def _create_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(BumiLestariBase.metadata.create_all)
        await _self_heal_columns(conn)


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
    # Langganan bulanan awal (nominal diisi dari halaman Langganan; 0 = belum diisi, belum disisihkan).
    if (await session.execute(select(BlLangganan.id))).first() is None:
        for nama in DEFAULT_LANGGANAN:
            session.add(BlLangganan(nama=nama, jumlah_bulanan=0))
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
    await session.flush()
    await _seed_setoran_modal(session, owner)
    await session.commit()
    return {"owner_email": email, "status": "ok"}


async def _seed_setoran_modal(session: AsyncSession, owner: BlUser) -> None:
    """Setoran modal Owner Rp 20 juta, 1 Sep 2026, ke Kas utama -- hanya bila belum ada setoran modal sama sekali."""
    kategori = (await session.execute(select(BlKategori).where(BlKategori.nama == "Setoran modal"))).scalar_one()
    sudah = (
        await session.execute(
            select(BlTransaksi.id).where(BlTransaksi.kategori_id == kategori.id, BlTransaksi.dibatalkan.is_(False))
        )
    ).first()
    if sudah:
        return
    kas = (await session.execute(select(BlAkunKas).where(BlAkunKas.kode == KODE_KAS_UTAMA))).scalar_one()
    session.add(
        BlTransaksi(
            tanggal=TANGGAL_SETORAN_MODAL_AWAL, akun_id=kas.id, kategori_id=kategori.id, jenis="masuk",
            jumlah=SETORAN_MODAL_AWAL, keterangan="Setoran modal Owner (entri pembuka)", dibuat_oleh=owner.id,
            status_kirim="terkirim",
        )
    )

