"""KK (Kartu Keluarga) OCR feature: parser, adapter gating, and that the new
santri kependudukan fields round-trip through create_santri/patch_santri.
"""
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.external import ocr_space_adapter
from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.kk_parser import parse_kartu_keluarga
from tenants.madrasah.modules.madrasah.application.schemas import SantriIn, SantriPatch
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import SantriMadrasah

# Real OCR.space (OCREngine=2) output for an actual Kartu Keluarga photo
# (captured while debugging a production report that the old, hand-written
# sample text didn't resemble at all -- the real form is a bordered table,
# and OCR reads it as label-block-then-value-block up top, then a run of
# lines per person with NO leading row number, not "1 Nama\nNIK\n...").
SAMPLE_KK_TEXT = """Nama Kepala Keluarga
Alamat
RT/RW
Kode Pos
KARTU KELUARGA
No . 3218090905170001
: HERU HERMAWAN, S.IP
DUSUN WONOHARJO
: 001/012
: 46396
Desa/Kelurahan
Kecamatan
Kabupaten/Kota
Provinsi
K 32180213463
: WONOHARJO
PANGANDARAN
: PANGANDARAN
: JAWA BARAT
No
Nama Lengkap
NIK
Jenis
Kelamin
Tempat Lahir
Tanggal
Lahir
Agama
Pendidikan
Jenis Pekerjaan
(1)
(2)
(3)
(4)
(5)
(6)
(7)
HERU HERMAWAN, S.IP
3207222205920002 LAKI-LAKI
CIAMIS
22-05-1992 ISLAM
DIPLOMA IV/STRATA I
(8)
WIRASWASTA
ENDAH TRESNASARI
3207226703920002 PEREMPUAN CIAMIS
27-03-1992ISLAM
SLTA/SEDERAJAT
PERANGKAT DESA
RAYYAN ATTAR HERMAWAN
3218092003180001 LAKI-LAKI
PANGANDARAN
20-03-2018|ISLAM
TIDAK/BLM SEKOLAH
BELUM/TIDAK BEKERJA
Status
Status Hubungan
Dokumen Imigrasi
Nama Orang Tua
No.
Perkawinan
Dalam Keluarga
Kewarganegaraan
No. Paspor
No. KITAP
Ayah
Ibu
(9)
(10)
(11)
(12)
（13）
(14)
(15)
KAWIN
KEPALA KELUARGA
WNI
RASIDI
YULINAR
KAWIN
ISTRI
WNI
ADPAR
HATOYAH
BELUM KAWIN
ANAK
WNI
HERU HERMAWAN, S.IP
ENDAH TRESNASARI
Dikeluarkan Tanggal
LEMBAR
24-04-2018
Kepala Keluarga
RT
III. Desa/Kelurahan
IV. Kecamatan
KEPALA KELUARGA
HERU HERMAWAN. S.IP
Tanda Tangan/Cap Jempol
"""


def test_parse_kartu_keluarga_extracts_all_rows():
    result = parse_kartu_keluarga(SAMPLE_KK_TEXT)
    assert result["nomor_kk"] == "3218090905170001"
    assert result["alamat_lengkap"] == "DUSUN WONOHARJO"
    assert len(result["anggota"]) == 3

    ayah, ibu, anak = result["anggota"]
    assert ayah["nama"] == "HERU HERMAWAN, S.IP"
    assert ayah["nik"] == "3207222205920002"
    assert ayah["jenis_kelamin"] == "L"
    assert ayah["tempat_lahir"] == "Ciamis"
    assert ayah["tanggal_lahir"] == date(1992, 5, 22)
    assert ayah["agama"] == "Islam"
    assert ayah["status_dalam_keluarga"] == "Kepala Keluarga"

    assert ibu["nama"] == "ENDAH TRESNASARI"
    assert ibu["nik"] == "3207226703920002"
    assert ibu["jenis_kelamin"] == "P"
    assert ibu["tempat_lahir"] == "Ciamis"
    assert ibu["tanggal_lahir"] == date(1992, 3, 27)
    assert ibu["status_dalam_keluarga"] == "Istri"

    assert anak["nama"] == "RAYYAN ATTAR HERMAWAN"
    assert anak["nik"] == "3218092003180001"
    assert anak["tempat_lahir"] == "Pangandaran"
    assert anak["tanggal_lahir"] == date(2018, 3, 20)
    assert anak["status_dalam_keluarga"] == "Anak"
    # Ayah/ibu inferred from the head-of-family / wife rows for every member,
    # so the santri form can be prefilled with parent names.
    assert anak["nama_ayah"] == "HERU HERMAWAN, S.IP"
    assert anak["nama_ibu"] == "ENDAH TRESNASARI"


def test_parse_kartu_keluarga_empty_text_returns_no_members():
    result = parse_kartu_keluarga("")
    assert result["anggota"] == []
    assert result["nomor_kk"] is None


def test_parse_kartu_keluarga_does_not_pick_own_tempat_lahir_as_nama():
    """Regression: a lower-resolution re-compress of the same KK photo (the
    frontend downsizes uploads before OCR) can shift the OCR output so a
    person's tempat_lahir line ends up as the nearest preceding line to
    their NIK -- it must not be mistaken for their name."""
    text = """HERU HERMAWAN, S.IP
3207222205920002 LAKI-LAKI
CIAMIS
22-05-1992 ISLAM
ENDAH TRESNASARI
3207226703920002 PEREMPUAN CIAMIS
27-03-1992 ISLAM
"""
    result = parse_kartu_keluarga(text)
    assert len(result["anggota"]) == 2
    second = result["anggota"][1]
    assert second["nama"] == "ENDAH TRESNASARI"
    assert second["tempat_lahir"] == "Ciamis"


def test_parse_kartu_keluarga_nama_never_a_gender_or_agama_value():
    """A line that itself matches jenis kelamin/agama/tanggal lahir/NIK can
    never be returned as someone's nama, even when it's the nearest
    preceding line to a NIK (OCR sometimes drops the real name line)."""
    text = """3207222205920002 LAKI-LAKI
CIAMIS
22-05-1992 ISLAM
3207226703920002 PEREMPUAN
27-03-1992 ISLAM
"""
    result = parse_kartu_keluarga(text)
    for anggota in result["anggota"]:
        assert anggota["nama"] not in {"LAKI-LAKI", "PEREMPUAN", "ISLAM"}


def test_ocr_space_adapter_not_configured_without_api_key(monkeypatch):
    monkeypatch.delenv("OCR_SPACE_API_KEY", raising=False)
    assert ocr_space_adapter.is_configured() is False


def test_ocr_space_adapter_configured_with_api_key(monkeypatch):
    monkeypatch.setenv("OCR_SPACE_API_KEY", "fake-key-for-test")
    assert ocr_space_adapter.is_configured() is True


@pytest.mark.asyncio
async def test_detect_document_text_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("OCR_SPACE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OCR_SPACE_API_KEY"):
        await ocr_space_adapter.detect_document_text(b"fake-image-bytes")


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
