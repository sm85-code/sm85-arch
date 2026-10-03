"""Mesin format file penghasilan (spesifikasi 10.3): file ekspor marketplace (xlsx/csv) → baris standar pencairan.

Murni pemetaan kolom & format angka/tanggal -- tanpa logika bisnis (AB-MP-5) dan tanpa akses database, jadi bisa
dipakai unggahan dari layar, uji format di Data master, maupun job sinkronisasi.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any

PESAN_KOLOM_HILANG = "Kolom '{}' tidak ditemukan — format file mungkin berubah. Buat versi baru format."
_NOL = Decimal("0")


class FormatError(ValueError):
    """File tidak bisa dibaca dengan format ini (sheet/kolom tidak ada, file rusak)."""


@dataclass
class KolomPeta:
    kolom_tujuan: str
    kolom_sumber: str
    operasi: str = "ambil"
    nama_rincian: str | None = None


@dataclass
class KonfigFormat:
    kolom: list[KolomPeta]
    jenis_file: str = "xlsx"
    nama_sheet: str | None = None
    baris_header: int = 1
    baris_data_mulai: int | None = None
    format_tanggal: str = "dd/mm/yyyy"
    pemisah_desimal: str = ","
    pemisah_ribuan: str = "."
    aturan_tanda: str = "mutlak"  # mutlak: potongan diambil nilai mutlak; positif: apa adanya; kurung: (123) = -123
    aturan_jenis_baris: dict = field(default_factory=dict)  # {kolom, retur: [..], penyesuaian: [..], negatif_penyesuaian}
    satuan_baris: str = "per_pesanan"
    aturan_abaikan: dict = field(default_factory=dict)  # {kode_kosong: true, berisi: ["Total"]}


@dataclass
class Masalah:
    baris: int
    kolom: str
    nilai: str
    alasan: str

    def teks(self) -> str:
        return f"baris {self.baris}, {self.kolom}: '{self.nilai}' {self.alasan}"


@dataclass
class BarisStandar:
    baris_file: int
    kode_pesanan: str
    tanggal_cair: date
    jumlah_cair: Decimal
    harga_jual: Decimal | None = None
    potongan_biaya: Decimal | None = None
    rincian_biaya: dict[str, Decimal] = field(default_factory=dict)
    jenis_baris: str = "pesanan"
    mode_catat: str = "bruto"
    catatan: list[str] = field(default_factory=list)  # mis. selisih harga jual − potongan ≠ jumlah cair
    data_asli: dict[str, Any] = field(default_factory=dict)


@dataclass
class HasilFormat:
    baris: list[BarisStandar]
    masalah: list[Masalah]
    kolom_file: list[str]


# ---- membaca file ----------------------------------------------------------------------------------------------


def _teks(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def baca_sheet(isi: bytes, jenis_file: str, nama_sheet: str | None = None) -> tuple[list[str], list[list[Any]]]:
    """→ (daftar sheet, baris-baris sel). CSV: pemisah koma atau titik koma dideteksi otomatis."""
    if jenis_file == "csv":
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                teks = isi.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        contoh = teks[:4096]
        pemisah = ";" if contoh.count(";") > contoh.count(",") else ","
        return ["csv"], [list(r) for r in csv.reader(io.StringIO(teks), delimiter=pemisah)]
    if jenis_file != "xlsx":
        raise FormatError("Jenis file harus xlsx atau csv")
    try:
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(isi), read_only=True, data_only=True)
    except Exception as exc:  # file rusak / bukan xlsx
        raise FormatError("File tidak bisa dibaca sebagai Excel (.xlsx)") from exc
    try:
        sheets = list(wb.sheetnames)
        if nama_sheet and nama_sheet not in sheets:
            raise FormatError(f"Sheet '{nama_sheet}' tidak ditemukan — format file mungkin berubah. Buat versi baru format.")
        ws = wb[nama_sheet] if nama_sheet else wb[sheets[0]]
        return sheets, [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def saran_baris_header(rows: list[list[Any]], batas: int = 30) -> int:
    """Baris judul = baris (dari 30 pertama) dengan sel teks terbanyak."""
    terbaik, skor = 1, -1
    for i, r in enumerate(rows[:batas], start=1):
        n = sum(1 for v in r if isinstance(v, str) and v.strip() and not _mirip_angka(v))
        if n > skor:
            terbaik, skor = i, n
    return terbaik


def _mirip_angka(v: str) -> bool:
    return bool(re.fullmatch(r"[\s\-+()Rp.,\d]+", v)) and any(ch.isdigit() for ch in v)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def saran_pemetaan(kolom_file: list[str], kamus: dict[str, list[str]]) -> dict[str, str | None]:
    """Saran kolom file per kolom tujuan berdasarkan kemiripan nama (wizard 10.3.3 langkah 3)."""
    hasil: dict[str, str | None] = {}
    for tujuan, kandidat in kamus.items():
        terbaik, skor = None, 0.0
        for k in kolom_file:
            for c in kandidat:
                s = SequenceMatcher(None, _norm(k), _norm(c)).ratio()
                if _norm(c) and _norm(c) in _norm(k):
                    s = max(s, 0.9)
                if s > skor:
                    terbaik, skor = k, s
        hasil[tujuan] = terbaik if skor >= 0.6 else None
    return hasil


KAMUS_SARAN = {
    "kode_pesanan": ["No. Pesanan", "Order ID", "Nomor Pesanan", "Kode pesanan", "Order Number"],
    "tanggal_cair": ["Tanggal Dana Dilepaskan", "Tanggal cair", "Settlement Date", "Tanggal penyelesaian", "Release Date"],
    "jumlah_cair": ["Total Penghasilan", "Jumlah Dana Dilepaskan", "Settlement Amount", "Jumlah cair", "Total Settlement"],
    "harga_jual": ["Harga Asli Produk", "Total Pesanan", "Harga jual", "Subtotal", "Order Amount"],
}


# ---- angka & tanggal -------------------------------------------------------------------------------------------

_POLA_TANGGAL = {"dd": "%d", "mm": "%m", "yyyy": "%Y", "yy": "%y", "hh": "%H", "ii": "%M", "ss": "%S"}


def _strftime(pola: str) -> str:
    hasil = pola.lower().replace("hh:mm", "hh:ii").replace("hh.mm", "hh.ii")
    for k in ("yyyy", "yy", "dd", "mm", "hh", "ii", "ss"):
        hasil = hasil.replace(k, _POLA_TANGGAL[k])
    return hasil


def parse_tanggal(v: Any, pola: str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _teks(v)
    if not s:
        raise ValueError("kosong")
    fmt = _strftime(pola)
    for kandidat in (s, s.split(" ")[0]):
        try:
            return datetime.strptime(kandidat, fmt).date()
        except ValueError:
            pass
        try:  # pola tanpa jam untuk nilai yang membawa jam
            return datetime.strptime(kandidat, fmt.split(" ")[0]).date()
        except ValueError:
            pass
    raise ValueError("bukan tanggal sah")


def parse_angka(v: Any, desimal: str = ",", ribuan: str = ".", *, kurung: bool = True) -> Decimal:
    if v is None or v == "":
        return _NOL
    if isinstance(v, bool):
        raise ValueError("bukan angka")
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    s = str(v).strip().replace("Rp", "").replace("rp", "").replace("IDR", "").replace("\u00a0", "").replace(" ", "")
    if s in ("", "-"):
        return _NOL
    negatif = False
    if kurung and s.startswith("(") and s.endswith(")"):
        negatif, s = True, s[1:-1]
    if s.startswith("-"):
        negatif, s = not negatif, s[1:]
    s = s.replace(ribuan, "") if ribuan else s
    s = s.replace(desimal, ".") if desimal != "." else s
    try:
        d = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError("bukan angka") from exc
    return -d if negatif else d


# ---- menerapkan format -----------------------------------------------------------------------------------------


def _indeks_kolom(header: list[str], sumber: str) -> int:
    norm = [h.strip().lower() for h in header]
    if sumber.strip().lower() in norm:
        return norm.index(sumber.strip().lower())
    if re.fullmatch(r"[A-Za-z]{1,3}", sumber.strip()):  # huruf kolom Excel
        idx = 0
        for ch in sumber.strip().upper():
            idx = idx * 26 + (ord(ch) - 64)
        return idx - 1
    raise FormatError(PESAN_KOLOM_HILANG.format(sumber))


def cek_konfig(konfig: KonfigFormat) -> list[str]:
    """Aturan 10.3.2: kode pesanan & tanggal cair wajib; minimal dua dari harga jual, potongan, jumlah cair."""
    ada = {k.kolom_tujuan for k in konfig.kolom}
    salah = [f"Kolom tujuan '{t}' wajib dipetakan" for t in ("kode_pesanan", "tanggal_cair") if t not in ada]
    if "jumlah_cair" not in ada and not {"harga_jual", "potongan_biaya"} <= ada:
        salah.append("Petakan jumlah cair, atau harga jual dan potongan biaya (yang ketiga dihitung)")
    if konfig.satuan_baris not in ("per_pesanan", "per_produk"):
        salah.append("Satuan baris harus per_pesanan atau per_produk")
    if konfig.aturan_tanda not in ("mutlak", "positif", "kurung"):
        salah.append("Aturan tanda harus mutlak, positif, atau kurung")
    return salah


def terapkan(rows: list[list[Any]], konfig: KonfigFormat) -> HasilFormat:
    """Jalankan format pada baris-baris file. Baris bermasalah dicatat (nomor baris, kolom, nilai, alasan)."""
    if len(rows) < konfig.baris_header:
        raise FormatError(f"File hanya punya {len(rows)} baris; baris judul {konfig.baris_header} tidak ada")
    header = [_teks(h) for h in rows[konfig.baris_header - 1]]
    peta = [(k, _indeks_kolom(header, k.kolom_sumber)) for k in konfig.kolom]
    mulai = konfig.baris_data_mulai or konfig.baris_header + 1
    jb = konfig.aturan_jenis_baris or {}
    idx_jenis = _indeks_kolom(header, jb["kolom"]) if jb.get("kolom") else None
    abaikan = konfig.aturan_abaikan or {"kode_kosong": True, "berisi": ["Total"]}
    berisi = [t.lower() for t in abaikan.get("berisi", [])]
    kurung = konfig.aturan_tanda == "kurung" or konfig.aturan_tanda == "mutlak"
    punya = {k.kolom_tujuan for k in konfig.kolom}

    baris: list[BarisStandar] = []
    masalah: list[Masalah] = []
    for no, r in enumerate(rows[mulai - 1:], start=mulai):
        sel = lambda i: r[i] if i < len(r) else None  # noqa: E731
        if all(_teks(v) == "" for v in r):
            continue
        if berisi and any(_teks(v).lower() in berisi for v in r[:3]):
            continue
        nilai: dict[str, Any] = {"harga_jual": None, "potongan_biaya": None, "jumlah_cair": None}
        rincian: dict[str, Decimal] = {}
        kode, tgl, salah = "", None, False
        for k, i in peta:
            v = sel(i)
            try:
                if k.kolom_tujuan == "kode_pesanan":
                    kode = _teks(v)
                elif k.kolom_tujuan == "tanggal_cair":
                    tgl = parse_tanggal(v, konfig.format_tanggal)
                elif k.kolom_tujuan in ("harga_jual", "jumlah_cair", "potongan_biaya"):
                    d = parse_angka(v, konfig.pemisah_desimal, konfig.pemisah_ribuan, kurung=kurung)
                    if k.operasi == "mutlak" or (k.kolom_tujuan == "potongan_biaya" and konfig.aturan_tanda == "mutlak"):
                        d = abs(d)
                    elif k.operasi == "balik_tanda":
                        d = -d
                    nilai[k.kolom_tujuan] = (nilai[k.kolom_tujuan] or _NOL) + d
                    if k.kolom_tujuan == "potongan_biaya":
                        nama = k.nama_rincian or k.kolom_sumber
                        rincian[nama] = rincian.get(nama, _NOL) + d
            except ValueError as exc:
                if k.kolom_tujuan == "tanggal_cair" and abaikan.get("kode_kosong", True) and not kode and _teks(v) == "":
                    salah = True  # baris tanpa kode & tanggal: diabaikan diam-diam (mis. baris catatan)
                    break
                masalah.append(Masalah(no, k.kolom_sumber, _teks(v), str(exc)))
                salah = True
        if not kode and abaikan.get("kode_kosong", True):
            continue
        if salah:
            continue
        if not kode:
            masalah.append(Masalah(no, "kode pesanan", "", "kosong"))
            continue
        hj, pot, cair = nilai["harga_jual"], nilai["potongan_biaya"], nilai["jumlah_cair"]
        catatan: list[str] = []
        if cair is None:
            cair = (hj or _NOL) - (pot or _NOL)
        elif hj is not None and pot is None:
            pot = hj - cair
            rincian = {"Potongan marketplace": pot}
        elif pot is not None and hj is None:
            hj = cair + pot
        elif hj is not None and pot is not None and hj - pot != cair:
            catatan.append(f"harga jual − potongan ({hj - pot}) ≠ jumlah cair ({cair})")
        mode = "bruto" if {"harga_jual", "potongan_biaya"} & punya else "neto"
        if mode == "neto":
            hj, pot, rincian = None, None, {}
        jenis = "pesanan"
        if idx_jenis is not None:
            teks_jenis = _teks(sel(idx_jenis)).lower()
            if any(t.lower() in teks_jenis for t in jb.get("retur", []) if t):
                jenis = "retur"
            elif any(t.lower() in teks_jenis for t in jb.get("penyesuaian", []) if t):
                jenis = "penyesuaian"
        if jenis == "pesanan" and cair < 0 and jb.get("negatif_penyesuaian", True):
            jenis = "penyesuaian"
        baris.append(BarisStandar(
            baris_file=no, kode_pesanan=kode, tanggal_cair=tgl, jumlah_cair=cair, harga_jual=hj, potongan_biaya=pot,
            rincian_biaya=rincian, jenis_baris=jenis, mode_catat=mode, catatan=catatan,
            data_asli={h: _teks(sel(i)) for i, h in enumerate(header) if h},
        ))
    if konfig.satuan_baris == "per_produk":
        baris = _gabung_per_pesanan(baris)
    return HasilFormat(baris=baris, masalah=masalah, kolom_file=[h for h in header if h])


def _gabung_per_pesanan(baris: list[BarisStandar]) -> list[BarisStandar]:
    """Satuan per produk: jumlahkan per (kode pesanan, jenis baris); tanggal cair = yang terakhir."""
    grup: dict[tuple[str, str], BarisStandar] = {}
    for b in baris:
        g = grup.get((b.kode_pesanan, b.jenis_baris))
        if g is None:
            grup[(b.kode_pesanan, b.jenis_baris)] = b
            continue
        g.jumlah_cair += b.jumlah_cair
        if b.harga_jual is not None:
            g.harga_jual = (g.harga_jual or _NOL) + b.harga_jual
        if b.potongan_biaya is not None:
            g.potongan_biaya = (g.potongan_biaya or _NOL) + b.potongan_biaya
        for k, v in b.rincian_biaya.items():
            g.rincian_biaya[k] = g.rincian_biaya.get(k, _NOL) + v
        g.tanggal_cair = max(g.tanggal_cair, b.tanggal_cair)
        g.catatan += b.catatan
    return list(grup.values())
