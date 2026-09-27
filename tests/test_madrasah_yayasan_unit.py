"""Regression tests for Fase 3 (Multi-Madrasah/Yayasan):

- ensure_default_unit_and_backfill() membuat "Unit Utama" kalau belum ada
  unit sama sekali, dan menandai baris lama (madrasah_unit_id=NULL) ke unit
  itu -- idempoten, tidak menyentuh baris yang sudah punya unit.
- create_tingkat/create_rombel/create_santri/create_mapel/create_guru jatuh
  ke unit default kalau madrasah_unit_id tidak disebutkan, tapi menghormati
  unit yang dipilih eksplisit untuk setup multi-unit.
- rekap_yayasan() meringkas per unit tanpa mencampur data unit lain.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import (
    GuruIn,
    MadrasahUnitIn,
    RombelIn,
    SantriIn,
    TingkatIn,
)
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import RombelMadrasah, SantriMadrasah, TingkatMadrasah, UserMadrasah


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_creates_default_unit_when_none_exists(session):
    tingkat = TingkatMadrasah(nama="Jilid 1", urutan=1)
    session.add(tingkat)
    await session.flush()
    assert tingkat.madrasah_unit_id is None

    await services.ensure_default_unit_and_backfill(session)

    units = await services.list_unit(session)
    assert len(units) == 1
    assert units[0].nama == "Unit Utama"

    await session.refresh(tingkat)
    assert tingkat.madrasah_unit_id == units[0].id


@pytest.mark.asyncio
async def test_backfill_is_idempotent_and_does_not_touch_rows_with_a_unit(session):
    await services.ensure_default_unit_and_backfill(session)
    unit_a = (await services.list_unit(session))[0]

    unit_b = await services.create_unit(session, MadrasahUnitIn(nama="Unit Cabang"))
    santri = SantriMadrasah(nama="Santri Cabang", madrasah_unit_id=unit_b.id)
    session.add(santri)
    await session.flush()

    await services.ensure_default_unit_and_backfill(session)

    await session.refresh(santri)
    assert santri.madrasah_unit_id == unit_b.id  # tidak ditimpa balik ke unit_a

    units = await services.list_unit(session)
    assert len(units) == 2  # tidak dibuat "Unit Utama" kedua


@pytest.mark.asyncio
async def test_create_functions_fall_back_to_default_unit(session):
    await services.ensure_default_unit_and_backfill(session)
    default_unit = (await services.list_unit(session))[0]

    tingkat = await services.create_tingkat(session, TingkatIn(nama="Jilid 2"))
    rombel = await services.create_rombel(session, RombelIn(nama="Jilid 2 A"))
    santri, _, _ = await services.create_santri(session, SantriIn(nama="Santri Baru"))
    guru, _ = await services.create_guru(session, GuruIn(nama="Guru Baru", no_hp="081377770001"))

    assert tingkat.madrasah_unit_id == default_unit.id
    assert rombel.madrasah_unit_id == default_unit.id
    assert santri.madrasah_unit_id == default_unit.id
    assert guru.madrasah_unit_id == default_unit.id


@pytest.mark.asyncio
async def test_create_functions_respect_explicit_unit(session):
    await services.ensure_default_unit_and_backfill(session)
    unit_b = await services.create_unit(session, MadrasahUnitIn(nama="Unit Cabang"))

    rombel = await services.create_rombel(session, RombelIn(nama="Jilid 3 Cabang", madrasah_unit_id=unit_b.id))
    assert rombel.madrasah_unit_id == unit_b.id


@pytest.mark.asyncio
async def test_rekap_yayasan_keeps_units_separate(session):
    await services.ensure_default_unit_and_backfill(session)
    unit_a = (await services.list_unit(session))[0]
    unit_b = await services.create_unit(session, MadrasahUnitIn(nama="Unit Cabang"))

    santri_a = SantriMadrasah(nama="Santri A", madrasah_unit_id=unit_a.id)
    santri_b1 = SantriMadrasah(nama="Santri B1", madrasah_unit_id=unit_b.id)
    santri_b2 = SantriMadrasah(nama="Santri B2", madrasah_unit_id=unit_b.id)
    rombel_b = RombelMadrasah(nama="Rombel B", madrasah_unit_id=unit_b.id)
    guru_b = UserMadrasah(nama="Guru B", no_hp="081377770002", password_hash="x", role="guru", madrasah_unit_id=unit_b.id)
    session.add_all([santri_a, santri_b1, santri_b2, rombel_b, guru_b])
    await session.flush()

    hasil = await services.rekap_yayasan(session)
    by_id = {r["unit_id"]: r for r in hasil}

    assert by_id[unit_a.id]["total_santri"] == 1
    assert by_id[unit_b.id]["total_santri"] == 2
    assert by_id[unit_b.id]["total_rombel"] == 1
    assert by_id[unit_b.id]["total_guru"] == 1
    assert by_id[unit_a.id]["total_rombel"] == 0


@pytest.mark.asyncio
async def test_update_yayasan_and_patch_unit(session):
    from tenants.madrasah.modules.madrasah.application.schemas import MadrasahUnitPatch, YayasanPatch

    await services.update_yayasan(session, YayasanPatch(nama="Yayasan Al-Barokah"))
    yayasan = await services.get_or_create_yayasan(session)
    assert yayasan.nama == "Yayasan Al-Barokah"

    unit = await services.create_unit(session, MadrasahUnitIn(nama="Unit Awal"))
    patched = await services.patch_unit(session, unit.id, MadrasahUnitPatch(nama="Unit Baru", aktif=False))
    assert patched.nama == "Unit Baru"
    assert patched.aktif is False


@pytest.mark.asyncio
async def test_patch_unit_rejects_unknown_id(session):
    from tenants.madrasah.modules.madrasah.application.schemas import MadrasahUnitPatch

    with pytest.raises(services.MadrasahNotFoundError):
        await services.patch_unit(session, "tidak-ada", MadrasahUnitPatch(nama="X"))


@pytest.mark.asyncio
async def test_list_santri_filters_by_unit(session):
    await services.ensure_default_unit_and_backfill(session)
    unit_a = (await services.list_unit(session))[0]
    unit_b = await services.create_unit(session, MadrasahUnitIn(nama="Unit Cabang"))
    session.add_all(
        [
            SantriMadrasah(nama="A1", madrasah_unit_id=unit_a.id),
            SantriMadrasah(nama="B1", madrasah_unit_id=unit_b.id),
        ]
    )
    await session.flush()

    only_b = await services.list_santri(session, status="semua", unit_id=unit_b.id)
    assert [s.nama for s in only_b] == ["B1"]

    semua = await services.list_santri(session, status="semua")
    assert {s.nama for s in semua} == {"A1", "B1"}


@pytest.mark.asyncio
async def test_admin_utama_sees_all_kepala_sees_own_unit(session):
    await services.ensure_default_unit_and_backfill(session)
    unit_a = (await services.list_unit(session))[0]
    unit_b = await services.create_unit(session, MadrasahUnitIn(nama="Unit Cabang"))
    admin = UserMadrasah(nama="Admin Utama", no_hp="081300000001", password_hash="x", role="admin", madrasah_unit_id=unit_a.id)
    kepala = UserMadrasah(nama="Kepala B", no_hp="081300000002", password_hash="x", role="kepala_sekolah", madrasah_unit_id=unit_b.id)
    session.add_all(
        [
            admin,
            kepala,
            SantriMadrasah(nama="A1", madrasah_unit_id=unit_a.id),
            SantriMadrasah(nama="B1", madrasah_unit_id=unit_b.id),
        ]
    )
    await session.flush()

    assert services.resolve_unit_scope(admin) is None
    assert services.resolve_unit_scope(admin, unit_b.id) == unit_b.id
    assert services.resolve_unit_scope(kepala) == unit_b.id
    with pytest.raises(services.MadrasahForbiddenError):
        services.resolve_unit_scope(kepala, unit_a.id)

    owned = await services.list_santri(session, status="semua", unit_id=services.resolve_unit_scope(kepala))
    assert [s.nama for s in owned] == ["B1"]
    all_rows = await services.list_santri(session, status="semua", unit_id=services.resolve_unit_scope(admin))
    assert {s.nama for s in all_rows} == {"A1", "B1"}


@pytest.mark.asyncio
async def test_staf_cannot_create_into_other_unit(session):
    await services.ensure_default_unit_and_backfill(session)
    unit_a = (await services.list_unit(session))[0]
    unit_b = await services.create_unit(session, MadrasahUnitIn(nama="Unit Cabang"))
    kepala = UserMadrasah(
        nama="Kepala A", no_hp="081300000003", password_hash="x", role="kepala_sekolah", madrasah_unit_id=unit_a.id
    )
    session.add(kepala)
    await session.flush()

    with pytest.raises(services.MadrasahForbiddenError):
        await services.create_santri(session, SantriIn(nama="Nyasar", madrasah_unit_id=unit_b.id), caller=kepala)

    admin = UserMadrasah(nama="Admin", no_hp="081300000004", password_hash="x", role="admin")
    session.add(admin)
    await session.flush()
    ok, _, _ = await services.create_santri(session, SantriIn(nama="Cabang", madrasah_unit_id=unit_b.id), caller=admin)
    assert ok.madrasah_unit_id == unit_b.id


@pytest.mark.asyncio
async def test_two_units_may_share_tingkat_name(session):
    await services.ensure_default_unit_and_backfill(session)
    unit_a = (await services.list_unit(session))[0]
    unit_b = await services.create_unit(session, MadrasahUnitIn(nama="Unit Cabang"))
    t1 = await services.create_tingkat(session, TingkatIn(nama="Jilid 1", madrasah_unit_id=unit_a.id))
    t2 = await services.create_tingkat(session, TingkatIn(nama="Jilid 1", madrasah_unit_id=unit_b.id))
    assert t1.nama == t2.nama == "Jilid 1"
    assert t1.madrasah_unit_id != t2.madrasah_unit_id


@pytest.mark.asyncio
async def test_unit_with_santri_cannot_be_deleted(session):
    await services.ensure_default_unit_and_backfill(session)
    unit = (await services.list_unit(session))[0]
    session.add(SantriMadrasah(nama="Anak", madrasah_unit_id=unit.id))
    await session.flush()
    with pytest.raises(services.MadrasahForbiddenError):
        await services.assert_unit_boleh_dihapus(session, unit.id)


@pytest.mark.asyncio
async def test_kepala_user_id_mengikat_akun_ke_unit(session):
    await services.ensure_default_unit_and_backfill(session)
    unit_a = (await services.list_unit(session))[0]
    kepala = UserMadrasah(
        nama="Kepala Baru", no_hp="081300000099", password_hash="x", role="kepala_sekolah", madrasah_unit_id=unit_a.id
    )
    session.add(kepala)
    await session.flush()

    unit_b = await services.create_unit(session, MadrasahUnitIn(nama="Unit Cabang", kepala_user_id=kepala.id))
    await session.refresh(kepala)
    assert kepala.madrasah_unit_id == unit_b.id
    assert unit_b.kepala_unit == "Kepala Baru"

    daftar = await services.list_guru(session, unit_id=unit_b.id)
    assert [u.nama for u in daftar] == ["Kepala Baru"]


@pytest.mark.asyncio
async def test_akun_admin_tidak_diikat_ke_unit(session):
    await services.ensure_default_unit_and_backfill(session)
    admin_caller = UserMadrasah(nama="Admin", no_hp="081300000098", password_hash="x", role="admin")
    session.add(admin_caller)
    await session.flush()
    baru, _ = await services.create_guru(
        session, GuruIn(nama="Admin Yayasan", no_hp="081300000097", role="yayasan_admin"), caller=admin_caller
    )
    assert baru.madrasah_unit_id is None


@pytest.mark.asyncio
async def test_kepala_bertindak_sebagai_admin_unit(session):
    await services.ensure_default_unit_and_backfill(session)
    unit_a = (await services.list_unit(session))[0]
    unit_b = await services.create_unit(session, MadrasahUnitIn(nama="Unit Cabang"))
    kepala = UserMadrasah(
        nama="Kepala B", no_hp="081300000201", password_hash="x", role="kepala_sekolah", madrasah_unit_id=unit_b.id
    )
    session.add(kepala)
    await session.flush()

    guru, _ = await services.create_guru(
        session, GuruIn(nama="Guru B", no_hp="081300000202", role="guru", madrasah_unit_id=unit_a.id), caller=kepala
    )
    assert guru.madrasah_unit_id == unit_b.id

    with pytest.raises(services.MadrasahForbiddenError):
        await services.create_guru(
            session, GuruIn(nama="Admin Palsu", no_hp="081300000203", role="admin"), caller=kepala
        )
