"""Parses raw OCR text of an Indonesian Kartu Keluarga (KK) into structured
family-member rows.

Google Cloud Vision (adapters/external/google_vision_adapter.py) only
returns plain text -- it has no notion of "this is a KK" or which line is
whose NIK. All of the field-to-column mapping below is our own logic, built
against the standard KK layout:

    KARTU KELUARGA
    No. .................... : <nomor kk>
    Nama Kepala Keluarga     : ...
    Alamat                   : ...
    ...
    No | Nama Lengkap | NIK | Jenis Kelamin | Tempat Lahir | Tanggal Lahir |
        Agama | ... | Status Hubungan Dalam Keluarga | ...

In practice DOCUMENT_TEXT_DETECTION returns the table as a flat sequence of
lines (not aligned columns), so this parser groups lines by the numbered-row
markers ("1", "2", ...) that start each family member's block and pulls
fields out of the lines between them by keyword/pattern -- it does not
assume a fixed column order. This is inherently best-effort OCR text, so
every result MUST go through the mandatory admin verification/edit step
(see madrasah_router.py's KK endpoints) before anything is saved -- this
module never writes to the database.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

_NIK_RE = re.compile(r"\b(\d{16})\b")
_KK_NUMBER_RE = re.compile(r"\b(\d{16})\b")
_ROW_START_RE = re.compile(r"^(\d{1,2})[.)]?\s+(.*)$")
_DATE_RE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")

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


def _parse_tanggal(text: str) -> Optional[date]:
    match = _DATE_RE.search(text)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _find_tempat_tanggal_lahir(line: str) -> tuple[Optional[str], Optional[date]]:
    """"<tempat>, dd-mm-yyyy" is the standard KK rendering for this field."""
    tanggal = _parse_tanggal(line)
    if tanggal is None:
        return None, None
    tempat = line.split(",")[0].strip() if "," in line else None
    # Strip a leading numeric row marker accidentally captured as "tempat".
    if tempat and re.match(r"^\d", tempat):
        tempat = None
    return (tempat or None), tanggal


def _find_jenis_kelamin(line: str) -> Optional[str]:
    upper = line.upper()
    for keyword, code in _JENIS_KELAMIN_MAP.items():
        if keyword in upper:
            return code
    return None


def _find_agama(line: str) -> Optional[str]:
    upper = line.upper()
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


def _split_rows(lines: list[str]) -> list[list[str]]:
    """Groups lines into per-family-member blocks, split at each numbered
    row marker ("1 Budi Santoso", "2. Siti Aminah", ...). Lines before the
    first row marker (header, nomor KK, alamat) are dropped here and parsed
    separately by parse_kartu_keluarga()."""
    rows: list[list[str]] = []
    current: Optional[list[str]] = None
    next_expected = 1
    for line in lines:
        match = _ROW_START_RE.match(line)
        if match and int(match.group(1)) == next_expected:
            if current is not None:
                rows.append(current)
            current = [match.group(2)] if match.group(2) else []
            next_expected += 1
            continue
        if current is not None and line:
            current.append(line)
    if current is not None:
        rows.append(current)
    return rows


def _parse_row(row_number: int, block: list[str]) -> dict:
    nama = block[0].strip() if block else None
    nik = None
    tempat_lahir = None
    tanggal_lahir = None
    jenis_kelamin = None
    agama = None
    status_dalam_keluarga = None

    for line in block:
        if nik is None:
            match = _NIK_RE.search(line)
            if match:
                nik = match.group(1)
        if tempat_lahir is None and tanggal_lahir is None:
            tempat_lahir, tanggal_lahir = _find_tempat_tanggal_lahir(line)
        if jenis_kelamin is None:
            jenis_kelamin = _find_jenis_kelamin(line)
        if agama is None:
            agama = _find_agama(line)
        if status_dalam_keluarga is None:
            status_dalam_keluarga = _find_status_hubungan(line)

    return {
        "baris": row_number,
        "nama": nama or None,
        "nik": nik,
        "tempat_lahir": tempat_lahir,
        "tanggal_lahir": tanggal_lahir,
        "jenis_kelamin": jenis_kelamin,
        "agama": agama,
        "status_dalam_keluarga": status_dalam_keluarga,
    }


def _find_nomor_kk(lines: list[str]) -> Optional[str]:
    for line in lines:
        if "NOMOR" in line.upper() or "NO." in line.upper()[:6]:
            match = _KK_NUMBER_RE.search(line)
            if match:
                return match.group(1)
    for line in lines[:5]:
        match = _KK_NUMBER_RE.search(line)
        if match:
            return match.group(1)
    return None


def _find_alamat(lines: list[str]) -> Optional[str]:
    for line in lines:
        if line.upper().strip().startswith("ALAMAT"):
            value = line.split(":", 1)[1].strip() if ":" in line else line[len("ALAMAT"):].strip(" :")
            return value or None
    return None


def parse_kartu_keluarga(raw_text: str) -> dict:
    """Turns Vision's raw OCR text into a KkOcrResult-shaped dict:
    {nomor_kk, alamat_lengkap, anggota: [...], raw_text}.

    Best-effort only -- every field can come back None if the photo was
    blurry/cropped/at an angle. The caller (madrasah_router.py) returns this
    straight to the admin for review; nothing here is ever saved directly.
    """
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    nomor_kk = _find_nomor_kk(lines)
    alamat_lengkap = _find_alamat(lines)

    anggota = []
    for index, block in enumerate(_split_rows(lines), start=1):
        parsed = _parse_row(index, block)
        if parsed["nama"] or parsed["nik"]:
            anggota.append(parsed)

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
