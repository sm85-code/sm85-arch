"""Regression tests for Fase 2 (COA/double-entry & payroll honor ustadz):

- seed_akun_default() idempoten dan tidak menimpa akun yang sudah ada.
- pay_spp_manual dan create_buku_kas_entry masing-masing menulis satu baris
  jurnal berpasangan (debit/kredit) yang konsisten dengan kategori/tipenya.
- laba_rugi() meringkas jurnal per akun pendapatan/beban dengan benar.
- generate_honor_massal() menghitung sesi dari tanggal absensi UNIK per
  (guru, mapel) dalam satu bulan, idempoten per periode, dan pay_honor()
  menulis buku kas + jurnal yang berpasangan.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import (
    AbsenMapelBulkRequest,
    AbsenMapelItem,
    BukuKasIn,
    PenugasanIn,
)
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import (
    AkunMadrasah,
    MapelMadrasah,
    RombelMadrasah,
    SantriMadrasah,
    UserMadrasah,
)
from shared.security import hash_password


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
async def test_seed_akun_default_is_idempotent_and_preserves_edits(session):
    await services.seed_akun_default(session)
    first_count = len(await services.list_akun(session))
    assert first_count == len(services.AKUN_DEFAULT)

    kas = await session.get(AkunMadrasah, services.AKUN_KAS)
    kas.nama = "Kas Tunai (custom admin)"
    await session.flush()

    await services.seed_akun_default(session)
    rows = await services.list_akun(session)
    assert len(rows) == first_count
    kas_after = await session.get(AkunMadrasah, services.AKUN_KAS)
    assert kas_after.nama == "Kas Tunai (custom admin)"


@pytest.mark.asyncio
async def test_pay_spp_manual_writes_matching_journal_entry(session):
    await services.seed_akun_default(session)
    santri = SantriMadrasah(nama="Santri A")
    session.add(santri)
    await session.flush()
    tagihan = (await services.generate_spp_massal(session))
    assert tagihan, "seed harus menghasilkan minimal satu tagihan"
    target = tagihan[0]

    await services.pay_spp_manual(session, target.id)

    jurnal = await services.list_jurnal(session)
    assert len(jurnal) == 1
    assert jurnal[0].akun_debit == services.AKUN_KAS
    assert jurnal[0].akun_kredit == services.AKUN_PENDAPATAN_SPP
    assert jurnal[0].jumlah == target.nominal


@pytest.mark.asyncio
async def test_buku_kas_masuk_and_keluar_map_to_expected_accounts(session):
    await services.seed_akun_default(session)
    admin = UserMadrasah(nama="Admin", no_hp="081399999901", password_hash=hash_password("x"), role="admin")
    session.add(admin)
    await session.flush()

    await services.create_buku_kas_entry(
        session, BukuKasIn(tanggal=dt.date.today(), tipe="masuk", kategori="Donasi", jumlah=Decimal("100000")), dicatat_oleh=admin.id
    )
    await services.create_buku_kas_entry(
        session, BukuKasIn(tanggal=dt.date.today(), tipe="keluar", kategori="ATK", jumlah=Decimal("25000")), dicatat_oleh=admin.id
    )

    jurnal = await services.list_jurnal(session)
    assert len(jurnal) == 2
    masuk = next(j for j in jurnal if j.sumber_tipe == "buku_kas" and j.akun_debit == services.AKUN_KAS)
    keluar = next(j for j in jurnal if j.akun_kredit == services.AKUN_KAS)
    assert masuk.akun_kredit == services.AKUN_PENDAPATAN_LAIN  # "Donasi" tidak dikenali -> lain-lain
    assert keluar.akun_debit == services.AKUN_BEBAN_ATK  # "ATK" dikenali


@pytest.mark.asyncio
async def test_laba_rugi_summarizes_pendapatan_and_beban(session):
    await services.seed_akun_default(session)
    admin = UserMadrasah(nama="Admin", no_hp="081399999902", password_hash=hash_password("x"), role="admin")
    session.add(admin)
    await session.flush()

    await services.catat_jurnal(
        session, tanggal=dt.date.today(), akun_debit=services.AKUN_KAS, akun_kredit=services.AKUN_PENDAPATAN_SPP, jumlah=Decimal("50000")
    )
    await services.catat_jurnal(
        session, tanggal=dt.date.today(), akun_debit=services.AKUN_BEBAN_ATK, akun_kredit=services.AKUN_KAS, jumlah=Decimal("20000")
    )

    hasil = await services.laba_rugi(session)
    assert Decimal(hasil["total_pendapatan"]) == Decimal("50000")
    assert Decimal(hasil["total_beban"]) == Decimal("20000")
    assert Decimal(hasil["laba_bersih"]) == Decimal("30000")


@pytest.mark.asyncio
async def test_generate_honor_massal_counts_unique_session_dates(session):
    guru = UserMadrasah(nama="Ustadz A", no_hp="081399999903", password_hash=hash_password("x"), role="guru")
    rombel = RombelMadrasah(nama="Jilid 1")
    mapel = MapelMadrasah(kode="TAJWID", nama="Tajwid")
    session.add_all([guru, rombel, mapel])
    await session.flush()
    santri = SantriMadrasah(nama="Santri A", rombel_id=rombel.id)
    session.add(santri)
    await session.flush()

    await services.assign_guru_mapel(
        session, PenugasanIn(guru_id=guru.id, mapel_id=mapel.id, rombel_id=rombel.id, tarif_per_sesi=Decimal("15000"))
    )

    for day in (1, 2, 3):
        await services.bulk_insert_absensi_mapel(
            session,
            guru.id,
            AbsenMapelBulkRequest(
                tanggal=dt.date(2025, 8, day), rombel_id=rombel.id, mapel_id=mapel.id, items=[AbsenMapelItem(santri_id=santri.id, status="hadir")]
            ),
        )

    honor = await services.generate_honor_massal(session, "2025-08")
    assert len(honor) == 1
    assert honor[0].jumlah_sesi == 3
    assert honor[0].tarif_per_sesi == Decimal("15000")
    assert honor[0].total == Decimal("45000")


@pytest.mark.asyncio
async def test_generate_honor_massal_falls_back_to_default_tarif(session):
    guru = UserMadrasah(nama="Ustadz B", no_hp="081399999904", password_hash=hash_password("x"), role="guru")
    rombel = RombelMadrasah(nama="Jilid 2")
    mapel = MapelMadrasah(kode="HAFALAN", nama="Hafalan")
    session.add_all([guru, rombel, mapel])
    await session.flush()
    santri = SantriMadrasah(nama="Santri B", rombel_id=rombel.id)
    session.add(santri)
    await session.flush()

    await services.assign_guru_mapel(session, PenugasanIn(guru_id=guru.id, mapel_id=mapel.id, rombel_id=rombel.id))
    await services.bulk_insert_absensi_mapel(
        session,
        guru.id,
        AbsenMapelBulkRequest(
            tanggal=dt.date(2025, 9, 1), rombel_id=rombel.id, mapel_id=mapel.id, items=[AbsenMapelItem(santri_id=santri.id, status="hadir")]
        ),
    )

    honor = await services.generate_honor_massal(session, "2025-09")
    assert len(honor) == 1
    assert honor[0].tarif_per_sesi == services.DEFAULT_HONOR_PER_SESI


@pytest.mark.asyncio
async def test_generate_honor_massal_is_idempotent_per_period(session):
    guru = UserMadrasah(nama="Ustadz C", no_hp="081399999905", password_hash=hash_password("x"), role="guru")
    rombel = RombelMadrasah(nama="Jilid 3")
    mapel = MapelMadrasah(kode="IQRO", nama="Iqro")
    session.add_all([guru, rombel, mapel])
    await session.flush()
    santri = SantriMadrasah(nama="Santri C", rombel_id=rombel.id)
    session.add(santri)
    await session.flush()

    await services.assign_guru_mapel(session, PenugasanIn(guru_id=guru.id, mapel_id=mapel.id, rombel_id=rombel.id))
    await services.bulk_insert_absensi_mapel(
        session,
        guru.id,
        AbsenMapelBulkRequest(
            tanggal=dt.date(2025, 10, 1), rombel_id=rombel.id, mapel_id=mapel.id, items=[AbsenMapelItem(santri_id=santri.id, status="hadir")]
        ),
    )

    first = await services.generate_honor_massal(session, "2025-10")
    second = await services.generate_honor_massal(session, "2025-10")
    assert len(first) == 1
    assert second == []  # tidak ada baris baru dibuat, sudah ada untuk periode ini

    all_honor = await services.list_honor(session, "2025-10")
    assert len(all_honor) == 1


@pytest.mark.asyncio
async def test_pay_honor_writes_buku_kas_and_journal_then_rejects_double_pay(session):
    await services.seed_akun_default(session)
    guru = UserMadrasah(nama="Ustadz D", no_hp="081399999906", password_hash=hash_password("x"), role="guru")
    rombel = RombelMadrasah(nama="Jilid 4")
    mapel = MapelMadrasah(kode="AKHLAK", nama="Akhlak")
    session.add_all([guru, rombel, mapel])
    await session.flush()
    santri = SantriMadrasah(nama="Santri D", rombel_id=rombel.id)
    session.add(santri)
    await session.flush()

    await services.assign_guru_mapel(
        session, PenugasanIn(guru_id=guru.id, mapel_id=mapel.id, rombel_id=rombel.id, tarif_per_sesi=Decimal("20000"))
    )
    await services.bulk_insert_absensi_mapel(
        session,
        guru.id,
        AbsenMapelBulkRequest(
            tanggal=dt.date(2025, 11, 1), rombel_id=rombel.id, mapel_id=mapel.id, items=[AbsenMapelItem(santri_id=santri.id, status="hadir")]
        ),
    )
    honor = (await services.generate_honor_massal(session, "2025-11"))[0]

    paid = await services.pay_honor(session, honor.id)
    assert paid.status_bayar is True

    buku_kas = await services.list_buku_kas(session)
    assert any(r.kategori == "Honor Mengajar" and r.jumlah == Decimal("20000") for r in buku_kas)

    jurnal = await services.list_jurnal(session)
    honor_jurnal = next(j for j in jurnal if j.sumber_tipe == "honor")
    assert honor_jurnal.akun_debit == services.AKUN_BEBAN_HONOR
    assert honor_jurnal.akun_kredit == services.AKUN_KAS

    with pytest.raises(services.MadrasahForbiddenError):
        await services.pay_honor(session, honor.id)
