"""Parses raw OCR text of an Indonesian Kartu Keluarga (KK) into structured
family-member rows.

The OCR adapter (adapters/external/ocr_space_adapter.py) only returns plain
text -- it has no notion of "this is a KK" or which line is whose NIK. All
of the field-to-column mapping below is our own logic.

The official KK form is a bordered table, and OCR (without table-structure
detection) reads it as a flat sequence of lines that do NOT start with a
row number -- the "(1)" "(2)" ... column markers are their own lines, and
the actual row number cells are often skipped by the OCR engine entirely.
Verified against real output for this form (OCR.space, OCREngine=2), a
person's data comes out as a run of lines in a roughly fixed order:

    <nama lengkap>
    <NIK> <jenis kelamin>[ <tempat lahir>]
    [<tempat lahir>]
    <tanggal lahir>[<agama>]
    <pendidikan>
    <pekerjaan>

with the 16-digit NIK as the one reliably distinctive anchor per person
(the nomor KK is also 16 digits but appears once, earlier, next to "No").
This parser is therefore NIK-anchored: it finds every 16-digit number that
isn't the nomor KK, and for each one looks at the lines immediately before
(for the name) and after (for jenis kelamin/tempat/tanggal lahir/agama),
bounded by the next NIK found. "Status Hubungan Dalam Keluarga" (which
determines nama_ayah/nama_ibu) lives in a second table further down with
its own person-per-line pattern, matched by position (assumes it lists
family members in the same order as the first table, which the government
form always does).

This is inherently best-effort OCR text, so every result MUST go through
the mandatory admin verification/edit step (see madrasah_router.py's KK
endpoints) before anything is saved -- this module never writes to the
database.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

_NIK_RE = re.compile(r"\b(\d{16})\b")
_NOMOR_KK_RE = re.compile(r"\bNo\s*\.?\s*(\d{16})\b", re.IGNORECASE)
# No trailing \b: OCR sometimes runs the year straight into the next word
# with no space (e.g. "27-03-1992ISLAM"), and \b doesn't fire between two
# word characters (digit -> letter). (?!\d) only rules out a longer number.
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[-/](\d{1,2})[-/](\d{4})(?!\d)")
_PLACE_RE = re.compile(r"[A-Za-z][A-Za-z .'-]{1,39}")
_PAREN_MARKER_RE = re.compile(r"^[(（]\s*\d+\s*[)）]$")

_JENIS_KELAMIN_MAP = {
    "LAKI-LAKI": "L",
    "LAKI LAKI": "L",
    "PEREMPUAN": "P",
}

_STATUS_HUBUNGAN_KEYWORDS = (
    "KEPALA KELUARGA",
    "SUAMI",
    "ISTRI",
    "ANAK",
    "MENANTU",
    "CUCU",
    "ORANGTUA",
    "ORANG TUA",
    "MERTUA",
    "FAMILI LAIN",
    "PEMBANTU",
    "LAINNYA",
)

_AGAMA_KEYWORDS = (
    "ISLAM",
    "KRISTEN",
    "KATOLIK",
    "HINDU",
    "BUDHA",
    "BUDDHA",
    "KHONGHUCU",
    "KEPERCAYAAN",
)

# Header/label words that must never be mistaken for a person's name when
# scanning backward from a NIK for the nearest preceding line.
_HEADER_STOPWORDS = {
    "KARTU KELUARGA",
    "NAMA KEPALA KELUARGA",
    "ALAMAT",
    "RT/RW",
    "KODE POS",
    "DESA/KELURAHAN",
    "KECAMATAN",
    "KABUPATEN/KOTA",
    "PROVINSI",
    "NO",
    "NAMA LENGKAP",
    "NIK",
    "JENIS",
    "KELAMIN",
    "JENIS KELAMIN",
    "TEMPAT LAHIR",
    "TANGGAL",
    "LAHIR",
    "TANGGAL LAHIR",
    "AGAMA",
    "PENDIDIKAN",
    "JENIS PEKERJAAN",
}


def _parse_tanggal(text: str) -> Optional[date]:
    match = _DATE_RE.search(text)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _find_jenis_kelamin(text: str) -> Optional[str]:
    upper = text.upper()
    for keyword, code in _JENIS_KELAMIN_MAP.items():
        if keyword in upper:
            return code
    return None


def _find_agama(text: str) -> Optional[str]:
    upper = text.upper()
    for keyword in _AGAMA_KEYWORDS:
        if keyword in upper:
            return keyword.title()
    return None


def _find_status_hubungan(line: str) -> Optional[str]:
    upper = line.upper()
    for keyword in _STATUS_HUBUNGAN_KEYWORDS:
        if keyword in upper:
            return keyword.title()
    return None


def _find_nomor_kk(full_text: str) -> Optional[str]:
    match = _NOMOR_KK_RE.search(full_text)
    return match.group(1) if match else None


def _find_alamat(lines: list[str], nomor_kk: Optional[str]) -> Optional[str]:
    """Best-effort: the KK header block lists field LABELS (Nama Kepala
    Keluarga, Alamat, RT/RW, Kode Pos) before it lists their VALUES, so
    "Alamat"'s value doesn't sit next to its own label -- it's simply the
    first plausible value-looking line after the nomor KK line that isn't
    itself a ": <value>" for a different label (the head-of-family name,
    which always comes first)."""
    if nomor_kk is None:
        return None
    start = next((i for i, line in enumerate(lines) if nomor_kk in line), None)
    if start is None:
        return None
    for line in lines[start + 1 : start + 6]:
        candidate = line.strip()
        if not candidate or candidate.startswith(":"):
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9 ./,'-]{3,79}", candidate):
            return candidate
    return None


def _find_nama_before(lines: list[str], nik_idx: int, search_start: int, tempat_lahir: Optional[str]) -> Optional[str]:
    """Scans backward from the NIK line for the nearest line that's plausibly
    a person's name -- skipping header/column labels, paren column markers,
    and (importantly) lines that are actually OTHER fields: a jenis
    kelamin/agama keyword, a date, another NIK, or the same value already
    identified as this person's tempat_lahir (OCR sometimes drops the name
    line entirely, which would otherwise make "CIAMIS" look like the
    nearest candidate)."""
    tempat_upper = tempat_lahir.upper() if tempat_lahir else None
    for i in range(nik_idx - 1, search_start - 1, -1):
        candidate = lines[i].strip()
        if not candidate or _PAREN_MARKER_RE.match(candidate):
            continue
        upper = candidate.upper()
        if upper in _HEADER_STOPWORDS or upper == tempat_upper:
            continue
        if _find_jenis_kelamin(candidate) or _find_agama(candidate) or _DATE_RE.search(candidate) or _NIK_RE.search(candidate):
            continue
        candidate = candidate.lstrip(": ").strip()
        if candidate and re.search(r"[A-Za-z]{2,}", candidate) and len(candidate) <= 60:
            return candidate
    return None


def _find_tempat_lahir(lines_from_nik: list[str]) -> Optional[str]:
    if not lines_from_nik:
        return None
    first_line = lines_from_nik[0]
    gender_match = re.search(r"LAKI-LAKI|PEREMPUAN", first_line.upper())
    if gender_match:
        remainder = first_line[gender_match.end() :].strip(" :.-")
        if remainder and _PLACE_RE.fullmatch(remainder):
            return remainder.title()
    for line in lines_from_nik[1:3]:
        candidate = line.strip()
        if not candidate or _DATE_RE.search(candidate) or _find_jenis_kelamin(candidate) or _find_agama(candidate):
            continue
        if _PLACE_RE.fullmatch(candidate):
            return candidate.title()
    return None


def parse_kartu_keluarga(raw_text: str) -> dict:
    """Turns the OCR adapter's raw text into a KkOcrResult-shaped dict:
    {nomor_kk, alamat_lengkap, anggota: [...], raw_text}.

    Best-effort only -- every field can come back None if the photo was
    blurry/cropped/at an angle. The caller (madrasah_router.py) returns this
    straight to the admin for review; nothing here is ever saved directly.
    """
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    full_text = "\n".join(lines)
    nomor_kk = _find_nomor_kk(full_text)
    alamat_lengkap = _find_alamat(lines, nomor_kk)

    nik_positions: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        for match in _NIK_RE.finditer(line):
            nik = match.group(1)
            if nik != nomor_kk:
                nik_positions.append((idx, nik))

    anggota = []
    for person_index, (line_idx, nik) in enumerate(nik_positions):
        prev_end = nik_positions[person_index - 1][0] + 1 if person_index > 0 else 0
        # Look back a bit further than one line -- OCR occasionally drops
        # the name line entirely, and the fallback in _find_nama_before
        # (skipping known-other-field lines) needs room to reach the real
        # name instead of stopping at the first thing it sees.
        window_start = max(prev_end, line_idx - 4)
        window_end = nik_positions[person_index + 1][0] if person_index + 1 < len(nik_positions) else min(len(lines), line_idx + 6)
        lines_from_nik = lines[line_idx:window_end]
        window_text = "\n".join(lines_from_nik)

        tempat_lahir = _find_tempat_lahir(lines_from_nik)
        anggota.append(
            {
                "baris": person_index + 1,
                "nama": _find_nama_before(lines, line_idx, window_start, tempat_lahir),
                "nik": nik,
                "tempat_lahir": tempat_lahir,
                "tanggal_lahir": _parse_tanggal(window_text),
                "jenis_kelamin": _find_jenis_kelamin(window_text),
                "agama": _find_agama(window_text),
                "status_dalam_keluarga": None,
            }
        )

    # Second table ("Status Hubungan Dalam Keluarga") lists the same people
    # in the same order, further down -- matched by position, not by name.
    # Bounded to the region after the LAST "(9)".."(15)" column marker (the
    # second table's own header row) and before "Dikeluarkan Tanggal" (the
    # footer) -- otherwise header lines like "Nama Kepala Keluarga" (top of
    # the form) or "Nama Orang Tua" (second table's own column header) get
    # matched as if they were a person's actual status.
    paren_indices = [i for i, line in enumerate(lines) if _PAREN_MARKER_RE.match(line)]
    status_start = paren_indices[-1] + 1 if paren_indices else 0
    status_end = next(
        (i for i in range(status_start, len(lines)) if "DIKELUARKAN" in lines[i].upper()),
        len(lines),
    )
    status_matches = [s for s in (_find_status_hubungan(line) for line in lines[status_start:status_end]) if s]
    for person, status in zip(anggota, status_matches):
        person["status_dalam_keluarga"] = status

    # Ayah/ibu inferred from status_dalam_keluarga so the santri form can be
    # prefilled without the admin retyping parent names by hand.
    ayah = next((a["nama"] for a in anggota if a["status_dalam_keluarga"] == "Kepala Keluarga"), None)
    ibu = next((a["nama"] for a in anggota if a["status_dalam_keluarga"] == "Istri"), None)
    for a in anggota:
        a["nama_ayah"] = ayah
        a["nama_ibu"] = ibu

    return {
        "nomor_kk": nomor_kk,
        "alamat_lengkap": alamat_lengkap,
        "anggota": anggota,
        "raw_text": raw_text,
    }
