"""Per-tab keys: the split pages migrate without anyone gaining or losing access, and each tab is checked on its own."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import akses, peta_akses
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PengaturanUi, Role, RoleHalaman

P = "/api/marketplace-erp"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        await akses.pastikan_bawaan(s)
        await s.commit()
        yield s
    await engine.dispose()


async def _role_lama(s, rid, halaman):
    """A role as it was stored before the split (the marker removed so the migration runs again)."""
    s.add(Role(id=rid, nama=rid, keterangan="", akses_toko="semua", sistem=False))
    for k, t in halaman.items():
        s.add(RoleHalaman(role_id=rid, halaman=k, tingkat=t))
    penanda = await s.get(PengaturanUi, akses.PENANDA_PER_TAB)
    await s.delete(penanda)
    await s.flush()


async def _izin(s, rid):
    return {h.halaman: h.tingkat for h in (await s.scalars(select(RoleHalaman).where(RoleHalaman.role_id == rid))).all()}


@pytest.mark.asyncio
async def test_migration_copies_levels_keeps_old_rows_and_is_idempotent(session):
    await _role_lama(session, "lama", {"info_toko": "baca", "settlement": "tulis", "kelola_toko": "tulis", "kelola_pengguna": "baca"})
    await akses.pastikan_bawaan(session)
    izin = await _izin(session, "lama")
    for k in akses.USANG["info_toko"]:
        assert izin[k] == "baca"
    for k in akses.USANG["settlement"]:
        assert izin[k] == "tulis"
    assert izin["toko_penjualan"] == "tulis" and izin["kelola_role"] == "baca"
    assert izin["info_toko"] == "baca" and izin["settlement"] == "tulis"  # nothing deleted
    # a tab taken away later is not given back
    await session.delete(await session.get(RoleHalaman, ("lama", "performa_toko")))
    await session.flush()
    await akses.pastikan_bawaan(session)
    assert "performa_toko" not in await _izin(session, "lama")


@pytest.mark.asyncio
async def test_each_tab_route_has_its_own_key_and_old_key_still_opens():
    assert peta_akses.cari(f"{P}/akun/1/pengaturan-shopee/libur", "PUT") == ("pengaturan_toko_libur|info_toko", "tulis")
    assert peta_akses.cari(f"{P}/akun/1/performa-toko", "GET") == ("performa_toko|info_toko", "baca")
    assert peta_akses.cari(f"{P}/akun/1/saldo-toko", "GET")[0] == "settlement_saldo|settlement"
    assert peta_akses.cari(f"{P}/akun/1/transaksi-dana", "GET")[0] == "settlement_mutasi|settlement"
    assert peta_akses.cari(f"{P}/settlement-pesanan", "GET")[0] == "settlement_dana|settlement"
    assert peta_akses.cari(f"{P}/roles", "GET")[0] == "kelola_role|kelola_pengguna"
    assert peta_akses.cari(f"{P}/roles/x", "PUT") == ("kelola_role", "tulis")
    assert peta_akses.cari(f"{P}/users", "GET") == ("kelola_pengguna", "baca")


@pytest.mark.asyncio
async def test_read_only_tab_reads_but_cannot_write_and_other_tabs_closed(session):
    await akses.buat(session, "Baca Libur", "", "semua", {"pengaturan_toko_libur": "baca"})
    rid = "baca_libur"
    assert await akses.tingkat_halaman(session, rid, "pengaturan_toko_libur|info_toko") == "baca"
    assert await akses.tingkat_halaman(session, rid, "pengaturan_toko_profil|info_toko") is None
    assert await akses.tingkat_halaman(session, rid, "performa_toko|info_toko") is None


@pytest.mark.asyncio
async def test_old_app_sending_the_page_key_gets_every_tab(session):
    await akses.buat(session, "Dari App Lama", "", "semua", {"settlement": "baca"})
    izin = await _izin(session, "dari_app_lama")
    assert izin == {k: "baca" for k in akses.USANG["settlement"]}


@pytest.mark.asyncio
async def test_saving_from_new_editor_drops_the_old_page_row(session):
    await _role_lama(session, "lama2", {"info_toko": "tulis"})
    await akses.pastikan_bawaan(session)
    baru = {k: "tulis" for k in akses.USANG["info_toko"] if k != "performa_toko"}
    await akses.ubah(session, "lama2", "lama2", "", "semua", baru)
    assert await _izin(session, "lama2") == baru
    assert await akses.tingkat_halaman(session, "lama2", "performa_toko|info_toko") is None
