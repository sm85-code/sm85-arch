"""Postgres-only: bumi_lestari self-heal menambah kolom Fase 1 (status_kirim, session_version, sumber_*) ke
database yang dibuat sebelum kolom itu ada (create_all tidak pernah ALTER). Skip bila TEST_DATABASE_URL kosong;
job integration-pg CI mengisinya. Berjalan di schema sementara."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL tidak tersedia")

_KOLOM_BARU = {
    "bl_users": ("session_version",),
    "bl_transaksi": ("status_kirim", "kiriman_id", "sumber_sistem", "sumber_ref"),
    "bl_pembayaran_pemasok": ("status_kirim", "kiriman_id"),
    "bl_penerimaan_reseller": ("status_kirim", "kiriman_id"),
    "bl_order": ("sumber_sistem", "sumber_ref"),
    "bl_produk": ("sumber_sistem", "sumber_ref"),
}


def _asyncpg_url() -> str:
    url = os.getenv("TEST_DATABASE_URL") or ""
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):].split("?", 1)[0]
    return url


@pytest.mark.asyncio
async def test_self_heal_adds_fase1_columns_with_safe_defaults():
    from sqlalchemy.ext.asyncio import create_async_engine

    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_pembayaran  # noqa: F401
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import seeder
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase

    schema = f"bl_selfheal_{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(_asyncpg_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await conn.run_sync(BumiLestariBase.metadata.create_all)
            # Bentuk lama: buang kolom Fase 1, lalu isi satu transaksi lama.
            for idx in ("uq_bl_transaksi_sumber", "uq_bl_order_sumber", "uq_bl_produk_sumber"):
                await conn.execute(text(f"DROP INDEX IF EXISTS {idx}"))
            for tabel, kolom in _KOLOM_BARU.items():
                for k in kolom:
                    await conn.execute(text(f"ALTER TABLE {tabel} DROP COLUMN {k}"))
            await conn.execute(text("INSERT INTO bl_akun_kas (id, kode, nama, jenis, saldo_awal, aktif) VALUES ('a1','KAS','Kas','kas',0,true)"))
            await conn.execute(text("INSERT INTO bl_kategori (id, nama, jenis, aktif) VALUES ('k1','Pemasukan lain','pemasukan',true)"))
            await conn.execute(text(
                "INSERT INTO bl_transaksi (id, tanggal, akun_id, kategori_id, jenis, jumlah, keterangan, dibuat_oleh, dibatalkan) "
                "VALUES ('t1', '2026-09-01', 'a1', 'k1', 'masuk', 1000, '', 'u', false)"
            ))
            await seeder._self_heal_columns(conn)
            await seeder._self_heal_columns(conn)  # idempoten
            for tabel, kolom in _KOLOM_BARU.items():
                ada = {
                    r[0] for r in await conn.execute(text(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema = :s AND table_name = :t"
                    ), {"s": schema, "t": tabel})
                }
                assert set(kolom) <= ada, tabel
            status = (await conn.execute(text("SELECT status_kirim FROM bl_transaksi WHERE id='t1'"))).scalar_one()
            assert status == "terkirim"  # entri lama tetap masuk laporan
    except (OperationalError, InterfaceError, OSError, TimeoutError) as exc:
        pytest.skip(f"PostgreSQL integration unavailable: {exc}")
    except DBAPIError as exc:
        if "connect" in str(exc).lower():
            pytest.skip(f"PostgreSQL integration unavailable: {exc}")
        raise
    finally:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        except Exception:
            pass
        await engine.dispose()
