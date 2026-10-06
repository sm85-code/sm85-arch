"""Real PostgreSQL migration, trigger and concurrent allocation checks."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.keu_migration import INVARIANTS, ensure_keu_schema, sql
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_keu import KEU_MODELS

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL tidak tersedia")


@pytest_asyncio.fixture
async def pg():
    raw = os.environ["TEST_DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://")
    admin = create_async_engine(raw)
    schema = "keu_test_" + uuid4().hex[:12]
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(raw, connect_args={"server_settings": {"search_path": schema}}, pool_size=2, max_overflow=0)
    try:
        async with engine.begin() as conn:
            legacy = [t for t in BumiLestariBase.metadata.sorted_tables if not t.name.startswith("keu_")]
            await conn.run_sync(lambda sync: BumiLestariBase.metadata.create_all(sync, tables=legacy))
            # Execute the generated SQL with a native connection: validates the actual artifact too.
            native = await conn.get_raw_connection()
            script = sql().replace("BEGIN;\n\n", "", 1).rsplit("COMMIT;", 1)[0]
            await native.driver_connection.execute(script)
            await ensure_keu_schema(conn)  # repeat startup is safe
            assert (await conn.execute(text("SELECT count(*) FROM keu_vendor_slot"))).scalar_one() == 8
            assert (await conn.execute(text("SELECT count(*) FROM keu_vendor_slot WHERE vendor_id IS NOT NULL"))).scalar_one() == 0
            await conn.execute(text("INSERT INTO bl_users(id,nama,email,password_hash,role,aktif,must_change_password,created_at) VALUES('u','Test','test@local','hash','owner',true,false,now())"))
            await conn.execute(text("INSERT INTO bl_kategori(id,nama,jenis,aktif) VALUES('k','Penjualan','pemasukan',true)"))
            await conn.execute(text("INSERT INTO keu_saluran(id,nama,sistem,akun_ref,aktif) VALUES('s','Manual','manual','manual',true)"))
            await conn.execute(text("INSERT INTO keu_produk(id,sku,nama,jenis) VALUES('p','P','Kayu','kayu')"))
            await conn.execute(text("INSERT INTO keu_vendor(id,nama,jenis) VALUES('v1','Test 1','tukang_kayu'),('v2','Test 2','tukang_kayu')"))
            await conn.execute(text("UPDATE keu_vendor_slot SET vendor_id='v1' WHERE jenis='tukang_kayu' AND nomor=1"))
            await conn.execute(text("UPDATE keu_vendor_slot SET vendor_id='v2' WHERE jenis='tukang_kayu' AND nomor=2"))
            await conn.execute(text("INSERT INTO keu_pesanan(id,saluran_id,sumber_ref,nomor,tanggal,status_sumber,total_sumber) VALUES('o','s','o','o','2026-10-06','manual',20)"))
            await conn.execute(text("INSERT INTO keu_item(id,pesanan_id,sumber_ref,produk_id,nama_snapshot,qty,harga_satuan,subtotal_sumber) VALUES('i','o','i','p','Kayu',2,10,20)"))
            await conn.execute(text("INSERT INTO keu_akun(id,kode,nama,jenis) VALUES('a','KAS','Kas','kas')"))
            await conn.execute(text("INSERT INTO keu_settlement(id,saluran_id,sumber_ref,tanggal_cair,bruto,potongan,penyesuaian,neto) VALUES('st','s','st','2026-10-06',20,2,0,18)"))
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def test_generated_sql_is_checked_in_and_only_adds_keu_tables():
    assert Path("keu_initial.sql").read_text() == sql()
    assert len(KEU_MODELS) == 15
    assert all(m.__tablename__.startswith("keu_") for m in KEU_MODELS)
    assert len(INVARIANTS) == 24


@pytest.mark.asyncio
async def test_concurrent_allocations_cannot_exceed_quantity(pg):
    maker = async_sessionmaker(pg)
    async def allocate(key, vendor):
        try:
            async with maker.begin() as s:
                await s.execute(text("INSERT INTO keu_alokasi_vendor(id,item_id,vendor_id,qty,biaya_satuan,dibuat_oleh) VALUES(:id,'i',:v,2,5,'u')"), {"id": key, "v": vendor})
            return True
        except IntegrityError:
            return False
    result = await asyncio.gather(allocate("al1", "v1"), allocate("al2", "v2"))
    assert sorted(result) == [False, True]
    async with pg.begin() as conn:
        assert (await conn.execute(text("SELECT sum(qty) FROM keu_alokasi_vendor"))).scalar_one() == 2
    with pytest.raises(IntegrityError):
        async with pg.begin() as conn:
            await conn.execute(text("UPDATE keu_item SET qty=1 WHERE id='i'"))
    with pytest.raises(IntegrityError):
        async with pg.begin() as conn:
            await conn.execute(text("UPDATE keu_vendor SET jenis='supplier' WHERE id='v1'"))


@pytest.mark.asyncio
async def test_reconciliation_and_posted_records_immutable(pg):
    with pytest.raises(IntegrityError):
        async with pg.begin() as conn:
            await conn.execute(text("UPDATE keu_settlement SET status='terkirim' WHERE id='st'"))
    async with pg.begin() as conn:
        await conn.execute(text("INSERT INTO keu_alokasi_settlement(id,settlement_id,item_id,jumlah) VALUES('as','st','i',18)"))
        await conn.execute(text("UPDATE keu_settlement SET status='terkirim' WHERE id='st'"))
        await conn.execute(text("INSERT INTO keu_transaksi(id,saluran_id,sumber_ref,tanggal,akun_id,kategori_id,jenis,jumlah,dibuat_oleh,status) VALUES('tx','s','tx','2026-10-06','a','k','masuk',18,'u','terkirim')"))
    for statement in ["UPDATE keu_settlement SET neto=19,bruto=21 WHERE id='st'", "UPDATE keu_alokasi_settlement SET jumlah=17 WHERE id='as'", "DELETE FROM keu_transaksi WHERE id='tx'", "UPDATE keu_transaksi SET jumlah=19 WHERE id='tx'"]:
        with pytest.raises(IntegrityError):
            async with pg.begin() as conn:
                await conn.execute(text(statement))
