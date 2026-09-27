"""Bendahara harus bisa menghapus tagihan syahriyah yang salah generate
(misal duplikat, nominal keliru) selama belum dibayar -- tapi tidak boleh
menghapus tagihan yang sudah lunas, karena angkanya sudah tercermin di
buku kas/jurnal dan laporan laba rugi.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import SantriMadrasah, TagihanSyahriyah


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
async def test_delete_tagihan_belum_lunas_berhasil(session):
    santri = SantriMadrasah(nama="Ahmad")
    session.add(santri)
    await session.flush()
    tagihan = TagihanSyahriyah(bulan_tahun="2026-01", nominal="150000", status_bayar=False, santri_id=santri.id)
    session.add(tagihan)
    await session.flush()

    await services.delete_tagihan(session, tagihan.id)
    await session.flush()

    assert await session.get(TagihanSyahriyah, tagihan.id) is None


@pytest.mark.asyncio
async def test_delete_tagihan_sudah_lunas_ditolak(session):
    santri = SantriMadrasah(nama="Ahmad")
    session.add(santri)
    await session.flush()
    tagihan = TagihanSyahriyah(bulan_tahun="2026-01", nominal="150000", status_bayar=True, santri_id=santri.id)
    session.add(tagihan)
    await session.flush()

    with pytest.raises(services.MadrasahForbiddenError):
        await services.delete_tagihan(session, tagihan.id)

    assert await session.get(TagihanSyahriyah, tagihan.id) is not None


@pytest.mark.asyncio
async def test_delete_tagihan_not_found_raises(session):
    with pytest.raises(services.MadrasahNotFoundError):
        await services.delete_tagihan(session, "tidak-ada")


@pytest.mark.asyncio
async def test_list_tagihan_semua_filter_bulan(session):
    santri = SantriMadrasah(nama="Ahmad")
    session.add(santri)
    await session.flush()
    session.add_all(
        [
            TagihanSyahriyah(bulan_tahun="2026-01", nominal="150000", status_bayar=False, santri_id=santri.id),
            TagihanSyahriyah(bulan_tahun="2026-02", nominal="150000", status_bayar=True, santri_id=santri.id),
        ]
    )
    await session.flush()

    semua = await services.list_tagihan_semua(session)
    assert len(semua) == 2

    januari = await services.list_tagihan_semua(session, bulan_tahun="2026-01")
    assert len(januari) == 1
    assert januari[0].bulan_tahun == "2026-01"
