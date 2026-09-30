"""KK (Kartu Keluarga) OCR feature: parser, adapter gating, and that the new
santri kependudukan fields round-trip through create_santri/patch_santri.
"""
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.external import google_vision_adapter
from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.kk_parser import parse_kartu_keluarga
from tenants.madrasah.modules.madrasah.application.schemas import SantriIn, SantriPatch
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import SantriMadrasah

SAMPLE_KK_TEXT = """KARTU KELUARGA
No 3201010101010001
Nama Kepala Keluarga: Ahmad Sudrajat
Alamat: Jl. Melati No. 5 RT 01 RW 02
1 Ahmad Sudrajat
3201011501800001
LAKI-LAKI
Bandung, 15-01-1980
ISLAM
KEPALA KELUARGA
2 Siti Aminah
3201015803850002
PEREMPUAN
Bandung, 18-03-1985
ISLAM
ISTRI
3 Budi Sudrajat
3201010506100003
LAKI-LAKI
Bandung, 05-06-2010
ISLAM
ANAK
"""


def test_parse_kartu_keluarga_extracts_all_rows():
    result = parse_kartu_keluarga(SAMPLE_KK_TEXT)
    assert result["nomor_kk"] == "3201010101010001"
    assert result["alamat_lengkap"] == "Jl. Melati No. 5 RT 01 RW 02"
    assert len(result["anggota"]) == 3

    ayah, ibu, anak = result["anggota"]
    assert ayah["nama"] == "Ahmad Sudrajat"
    assert ayah["nik"] == "3201011501800001"
    assert ayah["jenis_kelamin"] == "L"
    assert ayah["tempat_lahir"] == "Bandung"
    assert ayah["tanggal_lahir"] == date(1980, 1, 15)
    assert ayah["agama"] == "Islam"
    assert ayah["status_dalam_keluarga"] == "Kepala Keluarga"

    assert anak["nama"] == "Budi Sudrajat"
    assert anak["status_dalam_keluarga"] == "Anak"
    # Ayah/ibu inferred from the head-of-family / wife rows for every member,
    # so the santri form can be prefilled with parent names.
    assert anak["nama_ayah"] == "Ahmad Sudrajat"
    assert anak["nama_ibu"] == "Siti Aminah"
    assert ibu["nama"] == "Siti Aminah"


def test_parse_kartu_keluarga_empty_text_returns_no_members():
    result = parse_kartu_keluarga("")
    assert result["anggota"] == []
    assert result["nomor_kk"] is None


def test_google_vision_adapter_not_configured_without_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
    assert google_vision_adapter.is_configured() is False


def test_google_vision_adapter_configured_with_api_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_VISION_API_KEY", "fake-key-for-test")
    assert google_vision_adapter.is_configured() is True


@pytest.mark.asyncio
async def test_detect_document_text_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_VISION_API_KEY"):
        await google_vision_adapter.detect_document_text(b"fake-image-bytes")


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
async def test_create_santri_persists_kependudukan_fields(session):
    payload = SantriIn(
        nama="Budi Sudrajat",
        nik="3201010506100003",
        tempat_lahir="Bandung",
        tanggal_lahir=date(2010, 6, 5),
        jenis_kelamin="L",
        agama="Islam",
        status_dalam_keluarga="Anak",
        alamat_lengkap="Jl. Melati No. 5 RT 01 RW 02",
        nomor_kk="3201010101010001",
        nama_ayah="Ahmad Sudrajat",
        nama_ibu="Siti Aminah",
    )
    row, _wali_username, _wali_password = await services.create_santri(session, payload)
    await session.flush()

    reloaded = await session.get(SantriMadrasah, row.id)
    assert reloaded.nik == "3201010506100003"
    assert reloaded.tempat_lahir == "Bandung"
    assert reloaded.tanggal_lahir == date(2010, 6, 5)
    assert reloaded.jenis_kelamin == "L"
    assert reloaded.agama == "Islam"
    assert reloaded.status_dalam_keluarga == "Anak"
    assert reloaded.alamat_lengkap == "Jl. Melati No. 5 RT 01 RW 02"
    assert reloaded.nomor_kk == "3201010101010001"
    assert reloaded.nama_ayah == "Ahmad Sudrajat"
    assert reloaded.nama_ibu == "Siti Aminah"


@pytest.mark.asyncio
async def test_patch_santri_updates_kependudukan_fields(session):
    santri = SantriMadrasah(nama="Ahmad")
    session.add(santri)
    await session.flush()

    await services.patch_santri(session, santri.id, SantriPatch(nik="1234567890123456", jenis_kelamin="L"))
    await session.flush()

    reloaded = await session.get(SantriMadrasah, santri.id)
    assert reloaded.nik == "1234567890123456"
    assert reloaded.jenis_kelamin == "L"
