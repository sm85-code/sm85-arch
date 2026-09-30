"""KK (Kartu Keluarga) OCR feature: parser, adapter gating, and that the new
santri kependudukan fields round-trip through create_santri/patch_santri.
"""
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.external import ocr_space_adapter
from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.kk_parser import _tanggal_lahir_from_nik, parse_kartu_keluarga
from tenants.madrasah.modules.madrasah.application.schemas import SantriIn, SantriPatch
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import SantriMadrasah, UserMadrasah

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
    assert result["rt_rw"] == "001/012"
    assert result["kode_pos"] == "46396"
    assert result["desa_kelurahan"] == "WONOHARJO"
    assert result["kecamatan"] == "PANGANDARAN"
    assert result["kabupaten_kota"] == "PANGANDARAN"
    assert result["provinsi"] == "JAWA BARAT"
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


# Real OCR.space output for the EXACT SAME photo as SAMPLE_KK_TEXT above, but
# from a different OCR run that happened to read the table column-by-column
# (all 3 names together, then all 3 NIK+gender+tempat rows, then all 3
# tanggal+agama rows, ...) instead of person-by-person. Same underlying
# document, structurally different OCR output -- both must parse correctly.
SAMPLE_KK_TEXT_COLUMN_MAJOR = """No
Nama Kepala Keluarga
Alamat
RT/RW
Kode Pos
Nama Lengkap
(1)
HERU HERMAWAN, S.IP
ENDAH TRESNASARI
RAYYAN ATTAR HERMAWAN
KARTU KELUARGA
No . 3218090905170001
: HERU HERMAWAN, S.IP
Desa/Kelurahan
: DUSUN WONOHARJO
Kecamatan
001/012
Kabupaten/Kota
46396
Provinsi
K 32180213463
: WONOHARJO
PANGANDARAN
PANGANDARAN
: JAWA BARAT
NIK
Jenis
Kelamin
Tempat Lahir
(2)
3207222205920002 LAKI-LAKI
CIAMIS
3207226703920002 PEREMPUAN CIAMIS
3218092003180001 LAKI-LAKI
PANGANDARAN
Tanggal
Agama
Lahir
(5)
（6）
22-05-1992 ISLAM
27-03-1992 ISLAM
220-03-2018 ISLAM
Pendidikan
Jenis Pekerjaan
(7)
DIPLOMA IV/STRATA I
SLTA/SEDERAJAT
TIDAK/BLM SEKOLAH
(8)
WIRASWASTA
PERANGKAT DESA
BELUM/TIDAK BEKERJA
No.
Status
Perkawinan
(9)
KAWIN
KAWIN
BELUM KAWIN
Status Hubungan
Dalam Keluarga
(10)
KEPALA KELUARGA
ISTRI
ANAK
Kewarganegaraan
(11)
WNI
WNI
WNI
Dokumen Imigrasi
No. Paspor
No. KITAP
(12)
(13)
Ayah
(14)
RASIDI
ADPAR
HERU HERMAWAN, S.IP
Nama Orang Tua
YULINAR
HATOYAH
ENDAH TRESNASARI
bu
（15）
Dikeluarkan Tanggal
LEMBAR
24-04-2018
Kepala Keluarga
RT
III. Desa/Kelurahan
1V. Kecamatan
SRAGAM : 125.000
.. Instaran : 25.000
KEPALA KELUARGA
HERU HERMAWAN. S.IP
Tanda Tangan/Cap Jempol
"""


def test_parse_kartu_keluarga_column_major_ocr_layout():
    result = parse_kartu_keluarga(SAMPLE_KK_TEXT_COLUMN_MAJOR)
    assert result["nomor_kk"] == "3218090905170001"
    assert result["alamat_lengkap"] == "DUSUN WONOHARJO"
    assert result["rt_rw"] == "001/012"
    assert result["kode_pos"] == "46396"
    assert result["desa_kelurahan"] == "WONOHARJO"
    assert result["kecamatan"] == "PANGANDARAN"
    assert result["kabupaten_kota"] == "PANGANDARAN"
    assert result["provinsi"] == "JAWA BARAT"
    assert len(result["anggota"]) == 3

    ayah, ibu, anak = result["anggota"]
    assert ayah["nama"] == "HERU HERMAWAN, S.IP"
    assert ayah["tanggal_lahir"] == date(1992, 5, 22)
    assert ayah["agama"] == "Islam"
    assert ayah["status_dalam_keluarga"] == "Kepala Keluarga"

    assert ibu["nama"] == "ENDAH TRESNASARI"
    assert ibu["tanggal_lahir"] == date(1992, 3, 27)
    assert ibu["status_dalam_keluarga"] == "Istri"

    assert anak["nama"] == "RAYYAN ATTAR HERMAWAN"
    # OCR misread the printed date as "220-03-2018" (extra leading digit),
    # but tanggal_lahir is derived from the NIK itself (see
    # _tanggal_lahir_from_nik) whenever it parses to a plausible date, and
    # that 16-digit NIK came through clean -- so the correct date still
    # comes out despite the corrupted printed text.
    assert anak["tanggal_lahir"] == date(2018, 3, 20)
    assert anak["status_dalam_keluarga"] == "Anak"
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


def test_parse_kartu_keluarga_alamat_not_confused_with_kepala_keluarga_name():
    """Regression: the header block's "Nama Kepala Keluarga" value line
    inconsistently keeps or drops its leading ": " across OCR runs of the
    exact same photo -- when it's dropped, it must not be mistaken for the
    Alamat value that follows it."""
    text = """Nama Kepala Keluarga
Alamat
RT/RW
Kode Pos
KARTU KELUARGA
No . 3218090905170001
HERU HERMAWAN, S.IP
DUSUN WONOHARJO
: 001/012
: 46396
No
Nama Lengkap
NIK
(1)
(2)
HERU HERMAWAN, S.IP
3207222205920002 LAKI-LAKI
CIAMIS
22-05-1992 ISLAM
"""
    result = parse_kartu_keluarga(text)
    assert result["alamat_lengkap"] == "DUSUN WONOHARJO"


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


def test_tanggal_lahir_from_nik_male():
    # HERU HERMAWAN's real NIK: digits 7-12 = 220592 -> 22-05-1992, male (day <= 40).
    assert _tanggal_lahir_from_nik("3207222205920002") == date(1992, 5, 22)


def test_tanggal_lahir_from_nik_female_subtracts_40_from_day():
    # ENDAH TRESNASARI's real NIK: digits 7-12 = 670392 -> day 67-40=27, 27-03-1992.
    assert _tanggal_lahir_from_nik("3207226703920002") == date(1992, 3, 27)


def test_tanggal_lahir_from_nik_2000s_year():
    # RAYYAN's real NIK: digits 7-12 = 200318 -> 20-03-2018.
    assert _tanggal_lahir_from_nik("3218092003180001") == date(2018, 3, 20)


def test_tanggal_lahir_from_nik_invalid_returns_none():
    assert _tanggal_lahir_from_nik("not-a-nik") is None
    assert _tanggal_lahir_from_nik("1234567899991234") is None  # month 99, day 99-40=59: invalid
    assert _tanggal_lahir_from_nik("") is None
    assert _tanggal_lahir_from_nik(None) is None


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
        rt_rw="001/002",
        kode_pos="40123",
        desa_kelurahan="Sukamaju",
        kecamatan="Coblong",
        kabupaten_kota="Bandung",
        provinsi="Jawa Barat",
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
    assert reloaded.rt_rw == "001/002"
    assert reloaded.kode_pos == "40123"
    assert reloaded.desa_kelurahan == "Sukamaju"
    assert reloaded.kecamatan == "Coblong"
    assert reloaded.kabupaten_kota == "Bandung"
    assert reloaded.provinsi == "Jawa Barat"


@pytest.mark.asyncio
async def test_patch_santri_updates_kependudukan_fields(session):
    santri = SantriMadrasah(nama="Ahmad")
    session.add(santri)
    await session.flush()

    await services.patch_santri(
        session, santri.id, SantriPatch(nik="1234567890123456", jenis_kelamin="L", kecamatan="Coblong")
    )
    await session.flush()

    reloaded = await session.get(SantriMadrasah, santri.id)
    assert reloaded.nik == "1234567890123456"
    assert reloaded.jenis_kelamin == "L"
    assert reloaded.kecamatan == "Coblong"


@pytest.mark.asyncio
async def test_patch_santri_creates_wali_from_nama_wali(session):
    """Registrasi lewat OCR KK boleh menyimpan santri tanpa wali dulu --
    admin mengikat wali belakangan lewat dropdown nama ayah/ibu (lihat
    AdminSantriPage), yang mengirim SantriPatch.nama_wali seperti ini."""
    santri = SantriMadrasah(nama="Rayyan", nama_ayah="Heru Hermawan", nama_ibu="Endah Tresnasari")
    session.add(santri)
    await session.flush()

    row, wali_username, wali_password = await services.patch_santri(session, santri.id, SantriPatch(nama_wali="Heru Hermawan"))
    assert row.orang_tua_id is not None
    assert wali_username
    assert wali_password

    wali = await session.get(UserMadrasah, row.orang_tua_id)
    assert wali.nama == "Heru Hermawan"
    assert wali.role == "wali_santri"


@pytest.mark.asyncio
async def test_patch_santri_reuses_existing_wali_with_same_name(session):
    existing_wali = UserMadrasah(nama="Endah Tresnasari", no_hp="081200000099", password_hash="x", role="wali_santri")
    session.add(existing_wali)
    santri = SantriMadrasah(nama="Rayyan")
    session.add(santri)
    await session.flush()

    row, wali_username, wali_password = await services.patch_santri(session, santri.id, SantriPatch(nama_wali="Endah Tresnasari"))
    assert row.orang_tua_id == existing_wali.id
    assert wali_username == "081200000099"
    # No new password is generated when an existing account is reused.
    assert wali_password is None


@pytest.mark.asyncio
async def test_wali_password_is_santri_birthdate_ddmmyy(session):
    """Password acak sebelumnya tidak pernah ditampilkan lagi setelah
    notifikasi awal hilang, jadi admin tidak bisa menjawab saat wali lupa
    akunnya. Password wali baru sekarang = tanggal lahir santri (ddmmyy),
    sesuatu yang admin selalu bisa lihat lagi di data santri."""
    santri = SantriMadrasah(nama="Rayyan", nama_ayah="Heru Hermawan", tanggal_lahir=date(2018, 3, 20))
    session.add(santri)
    await session.flush()

    _row, _wali_username, wali_password = await services.patch_santri(session, santri.id, SantriPatch(nama_wali="Heru Hermawan"))
    assert wali_password == "200318"


@pytest.mark.asyncio
async def test_wali_password_falls_back_to_random_without_birthdate(session):
    santri = SantriMadrasah(nama="Rayyan", nama_ayah="Heru Hermawan")
    session.add(santri)
    await session.flush()

    _row, _wali_username, wali_password = await services.patch_santri(session, santri.id, SantriPatch(nama_wali="Heru Hermawan"))
    assert wali_password
    assert wali_password != "200318"
