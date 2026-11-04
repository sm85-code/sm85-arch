"""Robustness of the Kartu Keluarga OCR parser against phone-photo OCR output.

Fixtures (tests/fixtures/kk/) are ANONYMIZED: fictional names/NIKs/addresses
with valid formats (NIK digits 7-12 = DDMMYY, +40 on DD for women), but the
exact layout quirks of real OCR.space output for real phone photos:

* kk_foto_hp_anon.txt -- engine 2, column-scrambled: labels interleaved with
  values, glued ``<NIK> PEREMPUAN GARUT`` token, ``K <number>`` junk line,
  ``: ``-prefixed wilayah values far from their labels, parents' name blocks
  before their "Ayah"/"Ibu" labels, footer noise (``: 125.000``, NIP line,
  ``AGUS SETIAWAN. S.PO``).
* kk_engine3_markdown_anon.txt -- engine 3 markdown table with shifted cells.

Every variant below must yield the SAME parse, and a value that differs from
the ground truth must never be silent (it has to be in uncertain_fields).
"""
import random
import re
from datetime import date
from pathlib import Path

import pytest

from adapters.external import ocr_space_adapter
from tenants.madrasah.adapters.api.v1.madrasah_router import build_kk_ocr_result
from tenants.madrasah.modules.madrasah.application.kk_parser import merge_parse_results, parse_kartu_keluarga
from tenants.madrasah.modules.madrasah.application.schemas import KkOcrResult

FIXTURES = Path(__file__).parent / "fixtures" / "kk"
PHONE = (FIXTURES / "kk_foto_hp_anon.txt").read_text()
MARKDOWN = (FIXTURES / "kk_engine3_markdown_anon.txt").read_text()

EXPECTED_HEADER = {
    "nomor_kk": "3278061203180004",
    "nama_kepala_keluarga": "AGUS SETIAWAN, S.PD",
    "alamat_lengkap": "DUSUN KARANGSARI",
    "rt_rw": "003/007",
    "kode_pos": "46195",
    "desa_kelurahan": "KARANGSARI",
    "kecamatan": "BOJONGSARI",
    "kabupaten_kota": "BOJONGSARI",
    "provinsi": "JAWA BARAT",
}
EXPECTED_MEMBERS = [
    {
        "nama": "AGUS SETIAWAN, S.PD",
        "nik": "3205101408900003",
        "jenis_kelamin": "L",
        "tempat_lahir": "Garut",
        "tanggal_lahir": date(1990, 8, 14),
        "agama": "Islam",
        "pendidikan": "DIPLOMA IV/STRATA I",
        "pekerjaan": "WIRASWASTA",
        "status_perkawinan": "KAWIN",
        "status_dalam_keluarga": "Kepala Keluarga",
        "kewarganegaraan": "WNI",
        "nama_ayah": "SUPARMAN",
        "nama_ibu": "KARTINI",
    },
    {
        "nama": "RINA MARLINA",
        "nik": "3205104911910001",
        "jenis_kelamin": "P",
        "tempat_lahir": "Garut",
        "tanggal_lahir": date(1991, 11, 9),
        "agama": "Islam",
        "pendidikan": "SLTA/SEDERAJAT",
        "pekerjaan": "PERANGKAT DESA",
        "status_perkawinan": "KAWIN",
        "status_dalam_keluarga": "Istri",
        "kewarganegaraan": "WNI",
        "nama_ayah": "DARSO",
        "nama_ibu": "WARSIH",
    },
    {
        "nama": "FAHRI ADITYA SETIAWAN",
        "nik": "3278060502170002",
        "jenis_kelamin": "L",
        "tempat_lahir": "Bojongsari",
        "tanggal_lahir": date(2017, 2, 5),
        "agama": "Islam",
        "pendidikan": "TIDAK/BLM SEKOLAH",
        "pekerjaan": "BELUM/TIDAK BEKERJA",
        "status_perkawinan": "BELUM KAWIN",
        "status_dalam_keluarga": "Anak",
        "kewarganegaraan": "WNI",
        "nama_ayah": "AGUS SETIAWAN, S.PD",
        "nama_ibu": "RINA MARLINA",
    },
]


def mismatches(result: dict) -> list[str]:
    """Every field that differs from the ground truth, marked SILENT when it
    is not listed in uncertain_fields."""
    out = []
    for key, want in EXPECTED_HEADER.items():
        if result.get(key) != want:
            flag = "flagged" if key in result["uncertain_fields"] else "SILENT"
            out.append(f"{key}: {result.get(key)!r} != {want!r} ({flag})")
    if len(result["anggota"]) != len(EXPECTED_MEMBERS):
        out.append(f"anggota count {len(result['anggota'])} != {len(EXPECTED_MEMBERS)} (SILENT)")
    for i, (got, want) in enumerate(zip(result["anggota"], EXPECTED_MEMBERS)):
        for key, value in want.items():
            if got.get(key) != value:
                flag = "flagged" if key in got["uncertain_fields"] else "SILENT"
                out.append(f"anggota {i + 1} {key}: {got.get(key)!r} != {value!r} ({flag})")
    return out


def assert_exact(result: dict) -> None:
    assert mismatches(result) == []


# ---------------------------------------------------------------------------
# Base fixtures
# ---------------------------------------------------------------------------


def test_phone_photo_fixture_parses_exactly_with_high_confidence():
    result = parse_kartu_keluarga(PHONE)
    assert_exact(result)
    assert result["uncertain_fields"] == []
    assert all(a["uncertain_fields"] == [] for a in result["anggota"])
    assert result["warnings"] == []


def test_engine3_markdown_fixture_with_shifted_cells_parses_exactly():
    # Row 1 has an extra BOJONGSARI cell after "LAKI-LAKI GARUT" and the
    # parents are shifted one column right -- content-based parsing must
    # still assign GARUT and SUPARMAN/KARTINI to row 1.
    result = parse_kartu_keluarga(MARKDOWN)
    assert_exact(result)
    assert result["uncertain_fields"] == []


def test_result_validates_against_api_schema():
    result = parse_kartu_keluarga(PHONE)
    model = KkOcrResult.model_validate(result)
    assert model.anggota[0].confidence["nik"] >= 0.9
    assert model.anggota[2].pendidikan == "TIDAK/BLM SEKOLAH"
    assert model.nama_kepala_keluarga == "AGUS SETIAWAN, S.PD"


# ---------------------------------------------------------------------------
# Mutated variants of the phone-photo fixture
# ---------------------------------------------------------------------------

_LABEL_LINE_RE = re.compile(r"^(\(\d+\)|No\.?|[A-Z][a-z].*|Ibu|Ayah|NIK)$")


def _is_label(line: str) -> bool:
    text = line.strip()
    if re.search(r"\d", text) and not re.fullmatch(r"\(\d+\)", text):
        return False  # e.g. "No. 3278..." is the nomor KK, not a label
    return bool(_LABEL_LINE_RE.match(text)) and "Dikeluarkan" not in text and "Tanda" not in text


def drop_label_lines(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not _is_label(ln) or "KARTU" in ln)


def shuffle_label_lines(text: str, seed: int) -> str:
    lines = text.splitlines()
    footer = next(i for i, ln in enumerate(lines) if ln.startswith("Dikeluarkan"))
    positions = [i for i, ln in enumerate(lines[:footer]) if _is_label(ln)]
    labels = [lines[i] for i in positions]
    random.Random(seed).shuffle(labels)
    for i, label in zip(positions, labels):
        lines[i] = label
    return "\n".join(lines)


def digit_confusions(text: str) -> str:
    return (
        text.replace("No. 3278061203180004", "No. 32780612O318OOO4")
        .replace("3205101408900003", "3205lO1408900003")
        .replace("3205104911910001 PEREMPUAN", "32051049119l0001 PEREMPUAN")
        .replace("3278060502170002 LAKI-LAKI", "3278 0605 0217 0002 LAKI-LAKI")
        .replace(": 46195", ": 4619S")
    )


def glued_tokens(text: str) -> str:
    text = text.replace("14-08-1990 ISLAM", "14-08-1990ISLAM")
    text = text.replace("09-11-1991 ISLAM", "09/11/1991 ISLAM")
    text = text.replace("05-02-2017 ISLAM", "05.02.2017|ISLAM")
    # Member 1's gender and birthplace glued onto its NIK line; the
    # standalone GARUT line disappears.
    text = text.replace("3205101408900003\nLAKI-LAKI\n", "3205101408900003 LAKI-LAKI GARUT\n")
    text = text.replace("(4)\nGARUT\nBOJONGSARI\n", "(4)\nBOJONGSARI\n")
    return text


def parents_labels_first(text: str) -> str:
    text = text.replace(
        "SUPARMAN\nDARSO\nAGUS SETIAWAN, S.PD\nAyah\n(14)\nNama Orang Tua\nKARTINI\nWARSIH\nRINA MARLINA\nIbu\n",
        "Ayah\n(14)\nSUPARMAN\nDARSO\nAGUS SETIAWAN, S.PD\nNama Orang Tua\nIbu\nKARTINI\nWARSIH\nRINA MARLINA\n",
    )
    assert "Ayah\n(14)\nSUPARMAN" in text
    return text


def fuzzy_vocab(text: str) -> str:
    return (
        text.replace("PEREMPUAN", "PEREMPUAM")
        .replace("WIRASWASTA", "WIRASWSTA")
        .replace("SLTA/SEDERAJAT", "SLTA/SEDERAJAI")
        .replace("\nKEPALA KELUARGA\nISTRI", "\nKEPALA KELUAGA\nISTRI")
        .replace("BELUM/TIDAK BEKERJA", "BELUMITIDAK BEKERJA")
        .replace("DIPLOMA IV/STRATA I", "DIPLOMA IV/STRATAI")
        .replace("\nLAKI-LAKI\n", "\nLAKl-LAKI\n")
    )


def extra_noise(text: str) -> str:
    text = text.replace("(15)\n", "(15)\nSRAGAM : 125.000\nPendaftaran : 25.000\n")
    return text.replace("\n", "\r\n").replace("KAWIN\r\n", "  KAWIN  \r\n", 1)


def header_colon_variants(text: str) -> str:
    return (
        text.replace(": 46195", "46195")
        .replace("\nDUSUN KARANGSARI\n", "\n: DUSUN KARANGSARI\n")
        .replace("\n003/007\n", "\n: 003/007\n")
        .replace(": AGUS SETIAWAN, S.PD\n", "AGUS SETIAWAN, S.PD\n", 1)
    )


VARIANTS = {
    "drop_label_lines": drop_label_lines,
    "digit_confusions": digit_confusions,
    "glued_tokens": glued_tokens,
    "parents_labels_first": parents_labels_first,
    "fuzzy_vocab": fuzzy_vocab,
    "extra_noise": extra_noise,
    "header_colon_variants": header_colon_variants,
    "all_combined": lambda t: header_colon_variants(fuzzy_vocab(digit_confusions(glued_tokens(t)))),
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_mutated_variant_gives_same_parse(name):
    result = parse_kartu_keluarga(VARIANTS[name](PHONE))
    assert_exact(result)


@pytest.mark.parametrize("seed", range(12))
def test_shuffled_label_lines_give_same_parse(seed):
    result = parse_kartu_keluarga(shuffle_label_lines(PHONE, seed))
    assert_exact(result)


def test_digit_confusions_are_corrected_but_flagged_when_unverified():
    result = parse_kartu_keluarga(digit_confusions(PHONE))
    assert_exact(result)
    # Corrected NIKs whose birth date matches the printed date are trusted.
    assert result["anggota"][0]["confidence"]["nik"] >= 0.9


def test_missing_gender_line_is_filled_from_nik():
    result = parse_kartu_keluarga(PHONE.replace("3205101408900003\nLAKI-LAKI\n", "3205101408900003\n"))
    assert_exact(result)
    assert result["anggota"][0]["confidence"]["jenis_kelamin"] >= 0.8


def test_missing_date_line_is_filled_from_nik_and_agama_flagged():
    result = parse_kartu_keluarga(PHONE.replace("14-08-1990 ISLAM\n", ""))
    # Values are still right (NIK encodes the date; the family shares agama)...
    assert_exact(result)
    # ...but which row lost its agama is a guess, so it is flagged.
    assert any("agama" in a["uncertain_fields"] for a in result["anggota"])


def test_missing_name_line_is_flagged_not_silently_shifted():
    result = parse_kartu_keluarga(PHONE.replace("RINA MARLINA\nFAHRI ADITYA SETIAWAN\n", "FAHRI ADITYA SETIAWAN\n", 1))
    problems = mismatches(result)
    assert problems, "a dropped name cannot be recovered"
    assert not [p for p in problems if "SILENT" in p], problems


def test_missing_parent_name_is_flagged_not_silent():
    result = parse_kartu_keluarga(PHONE.replace("SUPARMAN\n", ""))
    problems = mismatches(result)
    assert not [p for p in problems if "SILENT" in p], problems


def test_gender_nik_conflict_is_flagged():
    # OCR misread member 2's printed gender; NIK (day 49 -> female) disagrees.
    result = parse_kartu_keluarga(PHONE.replace("3205104911910001 PEREMPUAN GARUT", "3205104911910001 LAKI-LAKI GARUT"))
    member = result["anggota"][1]
    assert "jenis_kelamin" in member["uncertain_fields"]
    assert any("jenis kelamin" in w for w in result["warnings"])


def test_kk_number_is_not_counted_as_a_member():
    result = parse_kartu_keluarga(PHONE)
    assert [a["nik"] for a in result["anggota"]] == [m["nik"] for m in EXPECTED_MEMBERS]
    assert result["nomor_kk"] == "3278061203180004"


def test_footer_values_are_ignored():
    result = parse_kartu_keluarga(PHONE)
    everything = repr(result["anggota"]) + repr({k: result[k] for k in EXPECTED_HEADER})
    for noise in ("125.000", "BAMBANG", "19650412", "S.PO", "12-03-2018", "LDALAM"):
        assert noise not in everything


def test_row_major_tesseract_like_layout():
    text = """KARTU KELUARGA K 32780613579
No. 3278061203180004
Nama Kepala Keluarga : AGUS SETIAWAN, S.PD      Desa/Kelurahan : KARANGSARI
Alamat : DUSUN KARANGSARI      Kecamatan : BOJONGSARI
RT/RW : 003/007      Kabupaten/Kota : BOJONGSARI
Kode Pos : 46195      Provinsi : JAWA BARAT
No Nama Lengkap NIK Jenis Kelamin Tempat Lahir Tanggal Lahir Agama Pendidikan Jenis Pekerjaan
1 AGUS SETIAWAN, S.PD 3205101408900003 LAKI-LAKI GARUT 14-08-1990 ISLAM DIPLOMA IV/STRATA I WIRASWASTA
2 RINA MARLINA 3205104911910001 PEREMPUAN GARUT 09-11-1991 ISLAM SLTA/SEDERAJAT PERANGKAT DESA
3 FAHRI ADITYA SETIAWAN 3278060502170002 LAKI-LAKI BOJONGSARI 05-02-2017 ISLAM TIDAK/BLM SEKOLAH BELUM/TIDAK BEKERJA
No Status Perkawinan Status Hubungan Dalam Keluarga Kewarganegaraan No. Paspor No. KITAP Ayah Ibu
1 KAWIN KEPALA KELUARGA WNI - - SUPARMAN    KARTINI
2 KAWIN ISTRI WNI - - DARSO    WARSIH
3 BELUM KAWIN ANAK WNI - - AGUS SETIAWAN, S.PD    RINA MARLINA
Dikeluarkan Tanggal : 12-03-2018
"""
    assert_exact(parse_kartu_keluarga(text))


# ---------------------------------------------------------------------------
# Merging several OCR reads (engine 3 + engine 2)
# ---------------------------------------------------------------------------


def test_merge_engine3_and_engine2_reads():
    degraded_e2 = PHONE.replace("SUPARMAN\n", "").replace("\nKAWIN\nKAWIN\n", "\nKAWIN\n")
    merged = merge_parse_results([parse_kartu_keluarga(degraded_e2), parse_kartu_keluarga(MARKDOWN)])
    assert_exact(merged)
    assert merged["uncertain_fields"] == []
    assert all(a["uncertain_fields"] == [] for a in merged["anggota"])


def test_merge_flags_confident_disagreement():
    other = MARKDOWN.replace("Alamat : DUSUN KARANGSARI", "Alamat : DUSUN SUKAMAJU")
    merged = merge_parse_results([parse_kartu_keluarga(PHONE), parse_kartu_keluarga(other)])
    assert "alamat_lengkap" in merged["uncertain_fields"]
    assert any("alamat_lengkap" in w for w in merged["warnings"])


def test_build_kk_ocr_result_keeps_all_raw_reads_and_validates():
    result = build_kk_ocr_result([(3, MARKDOWN), (2, PHONE)])
    assert result["ocr_engines"] == [3, 2]
    assert "OCR engine 3" in result["raw_text"] and "OCR engine 2" in result["raw_text"]
    assert_exact(result)
    KkOcrResult.model_validate(result)


def test_build_kk_ocr_result_single_read_keeps_raw_text_verbatim():
    result = build_kk_ocr_result([(2, PHONE)])
    assert result["raw_text"] == PHONE
    assert_exact(result)


@pytest.mark.asyncio
async def test_multi_engine_survives_one_engine_failing(monkeypatch):
    monkeypatch.setenv("OCR_SPACE_API_KEY", "fake-key-for-test")

    def fake_parse(image_bytes, file_name, engine=2):
        if engine == 3:
            raise RuntimeError("E3 quota exceeded")
        return PHONE

    monkeypatch.setattr(ocr_space_adapter, "_parse_sync", fake_parse)
    reads = await ocr_space_adapter.detect_document_text_multi(b"img", "kk.jpg")
    assert reads == [(2, PHONE)]


@pytest.mark.asyncio
async def test_multi_engine_raises_when_all_engines_fail(monkeypatch):
    def fake_parse(image_bytes, file_name, engine=2):
        raise RuntimeError(f"down {engine}")

    monkeypatch.setattr(ocr_space_adapter, "_parse_sync", fake_parse)
    with pytest.raises(RuntimeError, match="engine 3: down 3; engine 2: down 2"):
        await ocr_space_adapter.detect_document_text_multi(b"img", "kk.jpg")
