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
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_keu import KEU_MODELS, KeuTransaksi

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
            await conn.execute(text("INSERT INTO keu_vendor(id,kode,nama,jenis) VALUES('v1','V1','Test 1','tukang_kayu'),('v2','V2','Test 2','tukang_kayu')"))
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
            await conn.execute(text("UPDATE keu_vendor SET jenis='supplier' WHERE id=(SELECT vendor_id FROM keu_alokasi_vendor LIMIT 1)"))


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


@pytest.mark.asyncio
async def test_product_additive_migration_preserves_legacy_and_is_repeatable(pg):
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.keu_product_migration import VERSION, sql as product_sql
    assert Path("keu_product_metadata.sql").read_text() == product_sql()
    async with pg.begin() as conn:
        for column in ["sku_induk", "nama_asli", "gambar_url", "varian_list", "harga_jual", "status"]:
            await conn.execute(text(f"ALTER TABLE keu_produk DROP COLUMN {column} CASCADE"))
        await conn.execute(text("ALTER TABLE keu_produk ALTER COLUMN jenis SET NOT NULL"))
        await conn.execute(text("UPDATE keu_produk SET biaya_acuan=123.45"))
        await conn.execute(text("DELETE FROM keu_schema_versions WHERE version=:v"), {"v": VERSION})
        native = await conn.get_raw_connection()
        script = product_sql().replace("BEGIN;\n\n", "", 1).rsplit("COMMIT;", 1)[0]
        await native.driver_connection.execute(script)
        await native.driver_connection.execute(script)
        await ensure_keu_schema(conn)
        row = (await conn.execute(text("SELECT id,sku,nama,nama_asli,jenis,biaya_acuan,status,aktif,varian_list FROM keu_produk"))).mappings().one()
        assert row["id"] == "p" and row["sku"] == "P" and row["nama_asli"] == row["nama"] == "Kayu"
        assert str(row["biaya_acuan"]) == "123.45" and row["status"] == "master" and row["aktif"]
        assert row["varian_list"] == []
    for statement in ["UPDATE keu_produk SET varian_list='{}'::jsonb", "UPDATE keu_produk SET harga_jual=-1", "UPDATE keu_produk SET jenis=NULL WHERE status='master'"]:
        with pytest.raises(IntegrityError):
            async with pg.begin() as conn:
                await conn.execute(text(statement))


@pytest.mark.asyncio
async def test_concurrent_source_products_share_one_child_sku(pg):
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_services as svc, schemas_keu as sc
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
    maker = async_sessionmaker(pg, expire_on_commit=False)
    async def create():
        async with maker.begin() as session:
            user = await session.get(BlUser, "u")
            line = sc.ItemIn(sumber_ref="source", nama_snapshot="Source", qty=1, harga_satuan="10", subtotal_sumber="10",
                produk_sumber=sc.ProdukSumberIn(sku="CONCURRENT", nama_asli="Source"))
            return await svc.source_product(session, user, line)
    keys = await asyncio.gather(create(), create())
    assert keys[0] == keys[1]
    async with pg.begin() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM keu_produk WHERE sku='CONCURRENT'"))).scalar_one() == 1
        # An unconfirmed draft is blocked even when raw SQL assigns a product kind.
        await conn.execute(text("UPDATE keu_produk SET jenis='kayu' WHERE sku='CONCURRENT'"))
        await conn.execute(text("UPDATE keu_item SET produk_id=:p WHERE id='i'"), {"p": keys[0]})
    with pytest.raises(IntegrityError):
        async with pg.begin() as conn:
            await conn.execute(text("INSERT INTO keu_alokasi_vendor(id,item_id,vendor_id,qty,biaya_satuan,dibuat_oleh) VALUES('blocked','i','v1',1,5,'u')"))


@pytest.mark.asyncio
async def test_dynamic_vendor_migration_preserves_legacy_ids_codes_and_history(pg):
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.keu_vendor_migration import VERSION, sql as vendor_sql
    assert Path("keu_dynamic_vendors.sql").read_text() == vendor_sql()
    async with pg.begin() as conn:
        await conn.execute(text("INSERT INTO keu_alokasi_vendor(id,item_id,vendor_id,qty,biaya_satuan,dibuat_oleh) VALUES('historic','i','v1',1,5,'u')"))
        await conn.execute(text("INSERT INTO keu_vendor(id,kode,nama,jenis,kontak,aktif) VALUES('legacy-free','MANUAL','Bebas','supplier','08123',false)"))
        for column in ["kode", "alamat", "keterangan"]:
            await conn.execute(text(f"ALTER TABLE keu_vendor DROP COLUMN {column} CASCADE"))
        await conn.execute(text("DELETE FROM keu_schema_versions WHERE version=:v"), {"v": VERSION})
        # Simulate the released, slot-constrained guards, then run the actual new artifact.
        for statement in [INVARIANTS[3], INVARIANTS[6]]:
            await conn.execute(text(statement))
        native = await conn.get_raw_connection()
        script = vendor_sql().replace("BEGIN;\n\n", "", 1).rsplit("COMMIT;", 1)[0]
        await native.driver_connection.execute(script)
        await native.driver_connection.execute(script)
        await ensure_keu_schema(conn)
        rows = (await conn.execute(text("SELECT id,kode,nama,jenis,kontak,aktif,alamat,keterangan FROM keu_vendor ORDER BY id"))).mappings().all()
        mapped = {row["id"]: row for row in rows}
        assert mapped["v1"]["kode"] == "tk-1" and mapped["v2"]["kode"] == "tk-2"
        assert mapped["legacy-free"]["kode"] == "VND-legacy-free" and not mapped["legacy-free"]["aktif"]
        assert mapped["legacy-free"]["kontak"] == "08123" and mapped["legacy-free"]["alamat"] == ""
        assert (await conn.execute(text("SELECT vendor_id,qty,biaya_satuan FROM keu_alokasi_vendor WHERE id='historic'"))).one() == ("v1", 1, 5)
        assert (await conn.execute(text("SELECT vendor_id FROM keu_vendor_slot WHERE jenis='tukang_kayu' AND nomor=1"))).scalar_one() == "v1"
        # A dynamic vendor with no slot can allocate immediately after the migration.
        await conn.execute(text("UPDATE keu_vendor SET aktif=true WHERE id='legacy-free'"))
        await conn.execute(text("INSERT INTO keu_vendor(id,kode,nama,jenis) VALUES('free','FREE','Tukang tanpa slot','tukang_kayu')"))
        await conn.execute(text("INSERT INTO keu_alokasi_vendor(id,item_id,vendor_id,qty,biaya_satuan,dibuat_oleh) VALUES('free-allocation','i','free',1,5,'u')"))


@pytest.mark.asyncio
async def test_dynamic_vendor_database_guards_and_no_slot_ceiling(pg):
    async with pg.begin() as conn:
        for index in range(12):
            await conn.execute(text("INSERT INTO keu_vendor(id,kode,nama,jenis) VALUES(:id,:code,'Dinamis','tukang_kayu')"), {"id": f"dyn-{index}", "code": f"DYN-{index}"})
        await conn.execute(text("INSERT INTO keu_vendor_slot(jenis,nomor,vendor_id) VALUES('tukang_kayu',99,'dyn-0')"))
        await conn.execute(text("INSERT INTO keu_alokasi_vendor(id,item_id,vendor_id,qty,biaya_satuan,dibuat_oleh) VALUES('noslot','i','dyn-11',1,5,'u')"))
        await conn.execute(text("UPDATE keu_vendor SET aktif=false WHERE id='dyn-11'"))
        assert (await conn.execute(text("SELECT count(*) FROM keu_alokasi_vendor WHERE vendor_id='dyn-11'"))).scalar_one() == 1
    for statement in [
        "INSERT INTO keu_vendor(id,kode,nama,jenis) VALUES('dup','DYN-11','Duplikat','supplier')",
        "UPDATE keu_vendor SET kode=' ' WHERE id='dyn-11'",
        "UPDATE keu_vendor SET jenis='supplier' WHERE id='dyn-11'",
        "INSERT INTO keu_alokasi_vendor(id,item_id,vendor_id,qty,biaya_satuan,dibuat_oleh) VALUES('inactive','i','dyn-11',1,5,'u')",
        "INSERT INTO keu_alokasi_vendor(id,item_id,vendor_id,qty,biaya_satuan,dibuat_oleh) VALUES('wrong-kind','i','v2',1,5,'u')",
    ]:
        if "wrong-kind" in statement:
            async with pg.begin() as conn:
                await conn.execute(text("UPDATE keu_vendor_slot SET vendor_id=NULL WHERE vendor_id='v2'"))
                await conn.execute(text("UPDATE keu_vendor SET jenis='supplier' WHERE id='v2'"))
        with pytest.raises(IntegrityError):
            async with pg.begin() as conn:
                await conn.execute(text(statement))


@pytest.mark.asyncio
async def test_vendor_deactivation_serializes_with_allocation(pg):
    from fastapi import HTTPException
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_services as svc, schemas_keu as sc
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
    maker = async_sessionmaker(pg, expire_on_commit=False)
    async def attempt(deactivate):
        try:
            async with maker.begin() as session:
                user = await session.get(BlUser, "u")
                if deactivate:
                    await svc.deactivate_vendor(session, user, "v1")
                else:
                    await svc.allocate_vendor(session, user, sc.AlokasiVendorIn(item_id="i", vendor_id="v1", qty=1, biaya_satuan="5"))
            return True
        except HTTPException as error:
            assert error.status_code == 422
            return False
    deactivated, allocated = await asyncio.wait_for(asyncio.gather(attempt(True), attempt(False)), timeout=15)
    assert deactivated
    async with pg.begin() as conn:
        assert not (await conn.execute(text("SELECT aktif FROM keu_vendor WHERE id='v1'"))).scalar_one()
        assert (await conn.execute(text("SELECT count(*) FROM keu_alokasi_vendor WHERE vendor_id='v1'"))).scalar_one() == int(allocated)


@pytest.mark.asyncio
async def test_confirmed_reset_is_atomic_preserves_legacy_and_posting_guards(pg):
    from datetime import date
    from shared.security import hash_password
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_reset, schemas_keu as sc
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAkunKas, BlTransaksi, BlUser
    maker = async_sessionmaker(pg, expire_on_commit=False)
    async with maker.begin() as session:
        user = await session.get(BlUser, "u")
        user.password_hash = hash_password("Owner-password")
        session.add(BlAkunKas(id="legacy", kode="LEGACY", nama="Legacy"))
        await session.flush()
        session.add(BlTransaksi(id="old", tanggal=date(2026, 10, 6), akun_id="legacy", kategori_id="k", jenis="masuk", jumlah=100, dibuat_oleh="u"))
        await session.execute(text("INSERT INTO keu_alokasi_settlement(id,settlement_id,item_id,jumlah) VALUES('as','st','i',18)"))
        await session.execute(text("UPDATE keu_settlement SET status='terkirim' WHERE id='st'"))
        await session.execute(text("INSERT INTO keu_transaksi(id,saluran_id,sumber_ref,tanggal,akun_id,kategori_id,jenis,jumlah,dibuat_oleh,status) VALUES('tx','s','tx','2026-10-06','a','k','masuk',18,'u','terkirim')"))
        await session.flush()
        preview = await keu_reset.preview(session, user)
    payload = sc.ResetKeuIn(challenge_id=preview["challenge_id"], token=preview["token"], konfirmasi="RESET-KEUANGAN", password="Owner-password")
    # A later failure rolls the whole reset back, including TRUNCATE and consumed nonce.
    with pytest.raises(RuntimeError):
        async with maker.begin() as session:
            user = await session.get(BlUser, "u")
            await keu_reset.execute(session, user, payload)
            raise RuntimeError("Rollback test")
    async with pg.begin() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM keu_transaksi"))).scalar_one() == 1
    async with maker.begin() as session:
        user = await session.get(BlUser, "u")
        result = await keu_reset.execute(session, user, payload)
        assert result["counts"]["keu_transaksi"] == 1
    async with pg.begin() as conn:
        for model in keu_reset.RESET_MODELS:
            assert (await conn.execute(text(f"SELECT count(*) FROM {model.__tablename__}"))).scalar_one() == 0
        assert (await conn.execute(text("SELECT jumlah FROM bl_transaksi WHERE id='old'"))).scalar_one() == 100
        assert (await conn.execute(text("SELECT count(*) FROM keu_vendor"))).scalar_one() == 2
        assert (await conn.execute(text("SELECT count(*) FROM keu_produk"))).scalar_one() == 1
        assert (await conn.execute(text("SELECT count(*) FROM keu_akun"))).scalar_one() == 1
        assert (await conn.execute(text("SELECT count(*) FROM bl_audit_log WHERE aksi='reset-keu'"))).scalar_one() == 1
        await conn.execute(text("INSERT INTO keu_transaksi(id,saluran_id,sumber_ref,tanggal,akun_id,kategori_id,jenis,jumlah,dibuat_oleh,status) VALUES('new','s','new','2026-10-06','a','k','masuk',18,'u','terkirim')"))
    with pytest.raises(IntegrityError):
        async with pg.begin() as conn:
            await conn.execute(text("DELETE FROM keu_transaksi WHERE id='new'"))


@pytest.mark.asyncio
async def test_reset_confirmation_cannot_be_replayed_concurrently(pg):
    from fastapi import HTTPException
    from shared.security import hash_password
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_reset, schemas_keu as sc
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
    maker = async_sessionmaker(pg, expire_on_commit=False)
    async with maker.begin() as session:
        user = await session.get(BlUser, "u")
        user.password_hash = hash_password("Owner-password")
        await session.flush()
        preview = await keu_reset.preview(session, user)
    payload = sc.ResetKeuIn(challenge_id=preview["challenge_id"], token=preview["token"], konfirmasi="RESET-KEUANGAN", password="Owner-password")
    async def attempt():
        try:
            async with maker.begin() as session:
                user = await session.get(BlUser, "u")
                await keu_reset.execute(session, user, payload)
            return True
        except HTTPException as error:
            assert error.status_code == 409
            return False
    assert sorted(await asyncio.wait_for(asyncio.gather(attempt(), attempt()), timeout=15)) == [False, True]


@pytest.mark.asyncio
async def test_order_cancellation_serializes_with_settlement_posting(pg):
    from fastapi import HTTPException
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_services as svc, schemas_keu as sc
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
    async with pg.begin() as conn:
        await conn.execute(text("INSERT INTO keu_pesanan(id,saluran_id,sumber_ref,nomor,tanggal,status_sumber,total_sumber) VALUES('other','s','other','other','2026-10-06','manual',20)"))
        await conn.execute(text("INSERT INTO keu_item(id,pesanan_id,sumber_ref,produk_id,nama_snapshot,qty,harga_satuan,subtotal_sumber) VALUES('other-item','other','other','p','Kayu',2,10,20)"))
        await conn.execute(text('UPDATE keu_settlement SET rincian=\'{"order_sn":"o"}\'::jsonb WHERE id=\'st\''))
    maker = async_sessionmaker(pg, expire_on_commit=False)
    async with maker.begin() as session:
        user = await session.get(BlUser, "u")
        await svc.allocate_settlement(session, user, sc.AlokasiSettlementIn(settlement_id="st", item_id="other-item", jumlah="18"))
    async def attempt(cancel):
        try:
            async with maker.begin() as session:
                user = await session.get(BlUser, "u")
                if cancel:
                    await svc.order_status(session, user, "o", sc.PesananStatusIn(status="batal", alasan="Diabaikan"))
                else:
                    await svc.post_settlement(session, user, "st", sc.PostingSettlementIn(akun_id="a", kategori_id="k"))
            return True
        except HTTPException as error:
            assert error.status_code == 409
            return False
    cancelled, posted = await asyncio.wait_for(asyncio.gather(attempt(True), attempt(False)), timeout=15)
    assert cancelled != posted
    async with maker() as session:
        order = (await session.execute(text("SELECT status FROM keu_pesanan WHERE id='o'"))).scalar_one()
        settlement = (await session.execute(text("SELECT status FROM keu_settlement WHERE id='st'"))).scalar_one()
        assert (order, settlement) == (("batal", "draf") if cancelled else ("draf", "terkirim"))
        dashboard = await svc.dashboard(session)
        assert float(dashboard["kas_masuk"]) == (0 if cancelled else 18)


@pytest.mark.asyncio
async def test_unpost_migration_idempotent_preserves_posted_history_and_guards(pg):
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.keu_unpost_migration import sql as unpost_sql
    assert Path("keu_unpost.sql").read_text() == unpost_sql()
    async with pg.begin() as conn:
        await conn.execute(text("INSERT INTO keu_transaksi(id,saluran_id,sumber_ref,akun_id,kategori_id,tanggal,jenis,jumlah,status,dibuat_oleh) VALUES('tx','s','tx','a','k','2026-10-06','masuk',18,'terkirim','u')"))
        native = await conn.get_raw_connection()
        script = unpost_sql().replace("BEGIN;\n\n", "", 1).rsplit("COMMIT;", 1)[0]
        await native.driver_connection.execute(script)
        await native.driver_connection.execute(script)
        await ensure_keu_schema(conn)
        assert (await conn.execute(text("SELECT jumlah FROM keu_transaksi WHERE id='tx'"))).scalar_one() == 18
        for update in ["status='dibatalkan'", "status='dibatalkan',alasan_batal='Koreksi',dibatalkan_oleh='u',dibatalkan_at=now(),jumlah=1", "status='draf'"]:
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE keu_transaksi SET " + update + " WHERE id='tx'"))
        await conn.execute(text("INSERT INTO bl_tutup_buku(id,periode,ditutup_oleh,ditutup_pada,snapshot,status) VALUES('closed','2026-10','u',now(),'{}','ditutup')"))
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(text("UPDATE keu_transaksi SET status='dibatalkan',alasan_batal='Koreksi',dibatalkan_oleh='u',dibatalkan_at=now() WHERE id='tx'"))
        await conn.execute(text("UPDATE bl_tutup_buku SET status='dibuka' WHERE periode='2026-10'"))
        await conn.execute(text("UPDATE keu_transaksi SET status='dibatalkan',alasan_batal='Koreksi',dibatalkan_oleh='u',dibatalkan_at=now() WHERE id='tx'"))
        for update in ["status='terkirim'", "alasan_batal='Rewrite audit'", "jumlah=19"]:
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE keu_transaksi SET " + update + " WHERE id='tx'"))


@pytest.mark.asyncio
async def test_concurrent_unpost_single_audit_and_zero_active_balance(pg):
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_services as svc, schemas_keu as sc
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
    maker = async_sessionmaker(pg, expire_on_commit=False)
    async with pg.begin() as conn:
        await conn.execute(text("INSERT INTO keu_transaksi(id,saluran_id,sumber_ref,akun_id,kategori_id,tanggal,jenis,jumlah,status,dibuat_oleh) VALUES('tx','s','tx','a','k','2026-10-06','masuk',18,'terkirim','u')"))
    async def cancel():
        async with maker.begin() as session:
            user = await session.get(BlUser, "u")
            return await svc.unpost_transaction(session, user, "tx", sc.BatalIn(alasan="Koreksi concurrent"))
    first, second = await asyncio.gather(cancel(), cancel())
    assert first == second
    async with maker.begin() as session:
        assert (await session.execute(text("SELECT count(*) FROM bl_audit_log WHERE aksi='unpost'"))).scalar_one() == 1
        assert (await svc.dashboard(session))["kas_masuk"] == "0"
        assert (await svc.page(session, KeuTransaksi))["total"] == 0


@pytest.mark.asyncio
async def test_settlement_unpost_atomic_rollback_and_linked_trigger_protection(pg):
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_services as svc, schemas_keu as sc
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
    maker = async_sessionmaker(pg, expire_on_commit=False)
    async with pg.begin() as conn:
        await conn.execute(text("INSERT INTO keu_alokasi_settlement(id,settlement_id,item_id,jumlah) VALUES('as','st','i',18)"))
    async with maker.begin() as session:
        user = await session.get(BlUser, "u")
        await svc.post_settlement(session, user, "st", sc.PostingSettlementIn(akun_id="a", kategori_id="k"))
    async with pg.begin() as conn:
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(text("UPDATE keu_settlement SET status='dibatalkan',alasan_batal='Koreksi',dibatalkan_oleh='u',dibatalkan_at=now() WHERE id='st'"))
    with pytest.raises(RuntimeError):
        async with maker.begin() as session:
            user = await session.get(BlUser, "u")
            await svc.unpost_settlement(session, user, "st", sc.BatalIn(alasan="Koreksi rollback"))
            raise RuntimeError("rollback request")
    async with pg.begin() as conn:
        assert (await conn.execute(text("SELECT status FROM keu_settlement WHERE id='st'"))).scalar_one() == "terkirim"
        assert (await conn.execute(text("SELECT status FROM keu_transaksi WHERE settlement_id='st'"))).scalar_one() == "terkirim"
        assert (await conn.execute(text("SELECT count(*) FROM bl_audit_log WHERE aksi='unpost'"))).scalar_one() == 0
    async def cancel():
        async with maker.begin() as session:
            user = await session.get(BlUser, "u")
            return await svc.unpost_settlement(session, user, "st", sc.BatalIn(alasan="Koreksi final"))
    first, second = await asyncio.gather(cancel(), cancel())
    assert first == second
    async with maker.begin() as session:
        assert float((await svc.dashboard(session))["saldo_kas"]) == 0
        assert (await session.execute(text("SELECT count(*) FROM bl_audit_log WHERE aksi='unpost'"))).scalar_one() == 2


async def finance_setup(session):
    from datetime import date
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_accounting as accounting, schemas_keu_finance as sc
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
    user=await session.get(BlUser,'u')
    await accounting.initialize(session,user,sc.BukuIn(tanggal_awal=date(2026,10,6),metode_stok='fifo',tanggal_status='updated_at',histori=True,konfirmasi='AKTIFKAN-BUKU-KEU'))
    return user


def test_financial_sql_is_complete_and_reproducible():
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.keu_finance_migration import sql as finance_sql
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_keu_finance import FINANCE_MODELS
    assert Path('keu_complete_finance.sql').read_text()==finance_sql()
    assert all(model.__tablename__.startswith('keu_') for model in FINANCE_MODELS)
    assert 'TRUNCATE' not in finance_sql() and 'DROP TABLE' not in finance_sql()


@pytest.mark.asyncio
async def test_finance_pg_transfer_concurrent_retry_balance_and_unpost(pg):
    from datetime import date
    from decimal import Decimal
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_ledger as ledger, schemas_keu_finance as sc, schemas_keu as old
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
    maker=async_sessionmaker(pg,expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(text("UPDATE keu_akun SET saldo_awal=500 WHERE id='a'"))
        await session.execute(text("INSERT INTO keu_akun(id,kode,nama,jenis) VALUES('bank','BANK','Bank','bank')"))
        await finance_setup(session)
    payload=sc.MutasiIn(referensi='concurrent',tanggal=date(2026,10,6),akun_asal_id='a',akun_tujuan_id='bank',nominal='100',biaya_admin='2')
    async def transfer():
        async with maker.begin() as session:
            return await ledger.transfer(session,await session.get(BlUser,'u'),payload)
    one,two=await asyncio.gather(transfer(),transfer())
    assert one['id']==two['id']
    async with maker.begin() as session:
        reports=await ledger.reports(session,date(2026,10,6),date(2026,10,6))
        assert Decimal(reports['neraca']['total_aset'])==498
        assert Decimal(reports['neraca']['selisih'])==0
        assert Decimal(reports['arus_kas']['kelompok']['mutasi']['neto'])==0
        assert Decimal(reports['arus_kas']['kelompok']['operasional']['neto'])==-2
        await ledger.unpost(session,await session.get(BlUser,'u'),one['id'],old.BatalIn(alasan='Koreksi transfer'))
    async with maker.begin() as session:
        assert Decimal((await ledger.reports(session,date(2026,10,6),date(2026,10,6)))['neraca']['total_aset'])==500
    for statement in ["UPDATE keu_jurnal_baris SET debet=debet+1 WHERE jurnal_id=:id AND debet>0","UPDATE keu_jurnal SET status='terkirim' WHERE id=:id"]:
        with pytest.raises(IntegrityError):
            async with pg.begin() as conn:
                await conn.execute(text(statement),{'id':one['id']})
    with pytest.raises(IntegrityError):
        async with pg.begin() as conn:
            await conn.execute(text("INSERT INTO keu_jurnal(id,sumber_key,jenis,tanggal,dibuat_oleh) VALUES('bad','bad','manual','2026-10-06','u')"))
            await conn.execute(text("INSERT INTO keu_jurnal_baris(id,jurnal_id,nomor,coa_id,debet,kredit,arus) VALUES('bad-line','bad',1,(SELECT id FROM keu_coa WHERE kas_akun_id='a'),1,0,'operasional')"))
            await conn.execute(text("UPDATE keu_jurnal SET status='terkirim' WHERE id='bad'"))


@pytest.mark.asyncio
async def test_finance_pg_fifo_exact_cost_capacity_and_immutable_audit(pg):
    from datetime import date
    from decimal import Decimal
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_inventory as inventory, keu_ledger as ledger, schemas_keu_finance as sc, schemas_keu as old
    maker=async_sessionmaker(pg,expire_on_commit=False)
    async with maker.begin() as session:
        user=await finance_setup(session)
        await inventory.receive(session,user,sc.StokMasukIn(referensi='batch1',tanggal=date(2026,10,6),produk_id='p',qty=1,biaya_total='5.01',sumber='pembelian',akun_kas_id='a'))
        await inventory.receive(session,user,sc.StokMasukIn(referensi='batch2',tanggal=date(2026,10,6),produk_id='p',qty=2,biaya_total='14',sumber='produksi',vendor_id='v1'))
        out=await inventory.fulfill(session,user,sc.StokKeluarIn(referensi='use',tanggal=date(2026,10,6),item_id='i',qty=2))
    async with pg.begin() as conn:
        assert (await conn.execute(text("SELECT nilai FROM keu_stok_mutasi WHERE jenis='keluar'"))).scalar_one()==Decimal('12.01')
        assert (await conn.execute(text("SELECT sum(nilai) FROM keu_stok_pemakaian"))).scalar_one()==Decimal('12.01')
    for statement in ["UPDATE keu_item SET qty=1 WHERE id='i'", "UPDATE keu_stok_pemakaian SET nilai=1", "UPDATE keu_stok_mutasi SET qty=4", "INSERT INTO keu_alokasi_vendor(id,item_id,vendor_id,qty,biaya_satuan,dibuat_oleh) VALUES('over','i','v1',1,5,'u')"]:
        with pytest.raises(IntegrityError):
            async with pg.begin() as conn:
                await conn.execute(text(statement))
    async with maker.begin() as session:
        from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
        user=await session.get(BlUser,'u')
        report=await ledger.reports(session,date(2026,10,6),date(2026,10,6))
        assert Decimal(report['neraca']['selisih'])==0 and Decimal(report['laba_rugi']['hpp'])==Decimal('12.01')
        await ledger.unpost(session,user,out['id'],old.BatalIn(alasan='Koreksi stok'))
    async with maker.begin() as session:
        assert sum(batch['qty'] for batch in await inventory.batches(session,'p'))==3


@pytest.mark.asyncio
async def test_finance_pg_historical_migration_closed_period_and_atomic_rollback(pg):
    from fastapi import HTTPException
    from sqlalchemy import select
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_keu_finance import KeuJurnal, KeuBuku
    maker=async_sessionmaker(pg,expire_on_commit=False)
    async with pg.begin() as conn:
        await conn.execute(text("INSERT INTO keu_transaksi(id,saluran_id,sumber_ref,tanggal,akun_id,kategori_id,jenis,jumlah,dibuat_oleh,status) VALUES('hist','s','hist','2026-10-06','a','k','masuk',18,'u','terkirim')"))
        await conn.execute(text("INSERT INTO bl_tutup_buku(id,periode,status,ditutup_oleh,ditutup_pada,snapshot) VALUES('closed','2026-10','ditutup','u',now(),'{}')"))
    async with maker.begin() as session:
        await finance_setup(session)
    async with maker.begin() as session:
        assert len((await session.execute(select(KeuJurnal))).scalars().all())==1
    # Activation is all-or-nothing when existing source settlement cannot be reconciled.
    async with pg.begin() as conn:
        await conn.execute(text("TRUNCATE keu_buku,keu_jurnal,keu_jurnal_baris,keu_stok_mutasi,keu_stok_pemakaian"))
        await conn.execute(text("UPDATE bl_tutup_buku SET status='dibuka' WHERE id='closed'"))
        await conn.execute(text("UPDATE keu_pesanan SET total_sumber=1 WHERE id='o'"))
        await conn.execute(text("INSERT INTO keu_alokasi_settlement(id,settlement_id,item_id,jumlah) VALUES('a-st','st','i',18)"))
        await conn.execute(text("UPDATE keu_settlement SET status='terkirim' WHERE id='st'"))
        await conn.execute(text("INSERT INTO keu_transaksi(id,saluran_id,sumber_ref,tanggal,akun_id,kategori_id,jenis,jumlah,dibuat_oleh,status,settlement_id) VALUES('st-tx','s','st-tx','2026-10-06','a','k','masuk',18,'u','terkirim','st')"))
    with pytest.raises(HTTPException):
        async with maker.begin() as session:
            await finance_setup(session)
    async with maker.begin() as session:
        assert (await session.execute(select(KeuBuku))).first() is None
        assert (await session.execute(select(KeuJurnal))).first() is None


@pytest.mark.asyncio
async def test_expenses_pg_classification_concurrent_retry_vendor_hpp_and_unpost(pg):
    from datetime import date
    from decimal import Decimal
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_accounting as accounting, keu_expenses as expenses, keu_ledger as ledger, keu_services as svc, schemas_keu as old, schemas_keu_finance as finance
    from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_keu_expenses import PengeluaranIn
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
    maker=async_sessionmaker(pg,expire_on_commit=False)
    day=date(2026,10,6)
    async with maker.begin() as session:
        user=await finance_setup(session)
        await svc.allocate_vendor(session,user,old.AlokasiVendorIn(item_id='i',vendor_id='v1',qty=2,biaya_satuan='5'))
        await svc.order_status(session,user,'o',old.PesananStatusIn(status='aktif'))
        await svc.order_status(session,user,'o',old.PesananStatusIn(status='selesai'))
    payload=PengeluaranIn(referensi='same',tanggal=day,akun_kas_id='a',tab='bahan',kategori='cat',jumlah='10',keterangan='Cat 50% _ Natural')
    async def create():
        async with maker.begin() as session:
            return await expenses.create(session,await session.get(BlUser,'u'),payload)
    one,two=await asyncio.gather(create(),create())
    assert one['id']==two['id']
    async with maker.begin() as session:
        user=await session.get(BlUser,'u')
        for key,tab,amount in [('listrik','operasional','3'),('gaji','gaji_iklan','4'),('ads_shopee','gaji_iklan','2')]:
            await expenses.create(session,user,PengeluaranIn(referensi=key,tanggal=day,akun_kas_id='a',tab=tab,kategori=key,jumlah=amount,keterangan=key+' Test'))
        payment=await accounting.pay_vendor(session,user,finance.BayarVendorIn(referensi='pay',tanggal=day,vendor_id='v1',akun_kas_id='a',jumlah='10'))
    async with maker.begin() as session:
        report=await ledger.reports(session,day,day)
        assert Decimal(report['laba_rugi']['hpp'])==20
        assert Decimal(report['laba_rugi']['beban_operasional'])==9
        assert Decimal(report['neraca']['selisih'])==0
        rows=await expenses.listing(session,'bahan',day,day,'50% _','pengerjaan',50,0)
        assert rows['total']==1 and rows['rows'][0]['id']==one['id']
        assert (await expenses.listing(session,'gaji_iklan',day,day,'','pengerjaan',50,0))['total']==2
        assert (await expenses.listing(session,'operasional',day,day,'','pengerjaan',50,0))['total']==1
        vendors=await expenses.listing(session,'vendor',day,day,'Test 1','pengerjaan',50,0)
        assert vendors['total']==1 and vendors['rows'][0]['vendor_nama']=='Test 1'
        assert vendors['rows'][0]['akun_nama']=='Kas'
        await ledger.unpost(session,await session.get(BlUser,'u'),payment['id'],old.BatalIn(alasan='Koreksi pelunasan'))
        await ledger.unpost(session,await session.get(BlUser,'u'),one['id'],old.BatalIn(alasan='Koreksi bahan'))
    async with maker.begin() as session:
        report=await ledger.reports(session,day,day)
        assert Decimal(report['laba_rugi']['hpp'])==10
        assert (await expenses.listing(session,'bahan',day,day,'','pengerjaan',50,0))['total']==0
        assert (await expenses.listing(session,'vendor',day,day,'','batal',50,0))['total']==1
