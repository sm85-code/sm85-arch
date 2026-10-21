"""Explicit admin import of the previously inspected Drive report, never a public seed."""

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import financial_reports as reports
from ..infrastructure.models import LaporanKeuanganAwal, LaporanKeuanganAsal, LaporanKeuanganTutup, LaporanKeuanganAudit


class DriveImport(reports.StrictInput):
    source_url: str = Field(max_length=2000)
    tab: str = Field(min_length=1, max_length=100)
    read_at: datetime
    rows: list[list[str | int | float | None]] = Field(min_length=101, max_length=200)


def parse(payload):
    parsed_url = urlparse(payload.source_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "docs.google.com"
        or not parsed_url.path.startswith("/spreadsheets/d/")
    ):
        raise HTTPException(422, "Gunakan URL spreadsheet Google Drive yang diperiksa")
    if payload.read_at.tzinfo is None:
        raise HTTPException(422, "Waktu pembacaan harus menyertakan zona waktu")
    rows = payload.rows

    def text(row, col):
        try:
            return rows[row - 1][col]
        except IndexError:
            return None

    def value(row, col):
        v = text(row, col)
        if v in (None, ""):
            return Decimal(0)
        try:
            n = reports.money(v)
            if not n.is_finite():
                raise ValueError
            return n
        except (ValueError, ArithmeticError):
            raise HTTPException(422, f"Sel {row}/{col} bukan angka mentah yang valid")

    if text(1, 0) != "LAPORAN KEUANGAN" or text(7, 1) != "PEMASUKAN" or text(24, 1) != "PENGELUARAN":
        raise HTTPException(422, "Struktur tab LAPORAN tidak sesuai; impor dibatalkan")
    cols = [c for c in range(2, 7) if text(6, c) == "OK"]
    if not cols or cols != list(range(2, 2 + len(cols))):
        raise HTTPException(422, "Kolom laporan selesai harus berurutan dari minggu pertama")
    end_first = date(1899, 12, 30) + timedelta(days=int(value(8, cols[0])))
    month_first = end_first.replace(day=1)
    opening_date = month_first - timedelta(days=1)
    b = reports.Balances(
        kas_utama=value(51, 2),
        stok=value(76, 2),
        kas_operasional=value(77, 2),
        iklan_prabayar=value(78, 2),
        piutang_pengiriman=0,
        saldo_toko=value(100, 2),
        kas_gaji=0,
        utang_gaji=0,
        utang_usaha=0,
        modal=value(91, 2),
        saldo_laba=value(92, 2),
    )
    if reports.totals(b.model_dump())["selisih"] != 0:
        raise HTTPException(422, "Saldo awal tidak dapat direkonsiliasi; perlu saldo awal lengkap")
    checksum = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    provenance = {
        "jenis": "google_drive",
        "url": payload.source_url,
        "tab": payload.tab,
        "read_at": payload.read_at.isoformat(),
        "checksum": checksum,
        "kebijakan": "Data utama Drive; ERP hanya pembanding. Riwayat Selasa tidak dibagi rata ke minggu baru.",
    }
    previous = opening_date
    snapshots = []
    for col in cols:
        cutoff = date(1899, 12, 30) + timedelta(days=int(value(8, col)))
        if not previous < cutoff or cutoff.month != month_first.month or cutoff.year != month_first.year:
            raise HTTPException(422, "Tanggal kolom harus berurutan dalam bulan yang sama")
        p = reports.CloseInput(
            tanggal=cutoff,
            penarikan_lain=sum(value(r, col) for r in (9, 10, 11)),
            pendapatan_produk=value(12, col),
            jasa_proses=value(13, col),
            jasa_cat_packing=value(14, col),
            jasa_cat=value(15, col),
            bayar_tukang=value(26, col),
            pembelian=value(27, col),
            operasional=value(28, col),
            langganan=value(29, col),
            gaji=value(30, col),
            biaya_iklan=value(31, col),
            topup_iklan=value(62, col),
            transfer_kas_ops=value(65, col),
            transfer_kas_gaji=-value(66, col),
            piutang_pengiriman=value(79, col),
            saldo_toko=value(80, col),
            stok=value(76, col),
            utang_usaha=value(88, col),
            catatan=f"Impor {payload.tab}, kolom {col + 2}",
            sumber_diperiksa=True,
        )
        result = reports.calculate(b, p, Decimal(0), Decimal(0), legacy=True)
        checks = {
            "pemasukan": (19, result["pemasukan"]),
            "pengeluaran": (36, result["pengeluaran"]),
            "laba": (45, result["laba"]),
            "kas_utama": (68, result["saldo_akhir"]["kas_utama"]),
            "aset": (82, result["neraca"]["aset"]),
            "selisih": (97, result["neraca"]["selisih"]),
        }
        if any(value(row, col) != expected for row, expected in checks.values()):
            raise HTTPException(422, "Angka hasil impor tidak sama dengan laporan Drive; tidak disimpan")
        for key, row in {
            "kas_utama": 75,
            "stok": 76,
            "kas_operasional": 77,
            "iklan_prabayar": 78,
            "piutang_pengiriman": 79,
            "saldo_toko": 80,
            "kas_gaji": 81,
            "utang_gaji": 87,
            "utang_usaha": 88,
            "modal": 91,
        }.items():
            if value(row, col) != result["saldo_akhir"][key]:
                raise HTTPException(422, f"Saldo akhir {key} tidak cocok dengan Drive")
        if result["neraca"]["selisih"] != 0:
            raise HTTPException(422, "Neraca impor tidak seimbang")
        source = {
            "settlements": [],
            "iklan": [],
            "biaya_iklan": "0",
            "pesanan_pengiriman": [],
            "stok": [],
            "drive": provenance,
            "utama": "google_drive",
            "pembanding_erp": None,
            "peringatan": [
                "Riwayat diimpor sesuai tanggal laporan Drive. Rincian harian belum tersedia untuk menyusun ulang minggu Senin–Minggu."
            ],
        }
        snapshot = reports.serialize(
            {
                "calendar_version": 2,
                "imported_historical": True,
                "tanggal": cutoff,
                "mulai": previous + timedelta(days=1),
                "jadwal_laporan": cutoff,
                "tutup_bulanan": cutoff == reports.month_bounds(cutoff.strftime("%Y-%m"))[1],
                "preview_token": "",
                "input": p.model_dump(),
                "sumber": source,
                "hasil": result,
                "created_at": payload.read_at,
                "rincian_toko": [{"nama": text(r, 1), "nominal": value(r, col)} for r in (9, 10, 11)],
            }
        )
        snapshots.append(snapshot)
        b = reports.Balances(**result["saldo_akhir"])
        previous = cutoff
    initial = reports.Balances(
        kas_utama=value(51, 2),
        stok=value(76, 2),
        kas_operasional=value(77, 2),
        iklan_prabayar=value(78, 2),
        piutang_pengiriman=0,
        saldo_toko=value(100, 2),
        kas_gaji=0,
        utang_gaji=0,
        utang_usaha=0,
        modal=value(91, 2),
        saldo_laba=value(92, 2),
    )
    return reports.Opening(tanggal=opening_date, saldo=initial), snapshots, provenance


async def import_drive(session, payload, actor):
    opening, snapshots, provenance = parse(payload)
    existing = await session.scalar(
        select(LaporanKeuanganAwal).where(LaporanKeuanganAwal.id == "global").with_for_update()
    )
    origin = await session.get(LaporanKeuanganAsal, "global")
    if origin and json.loads(origin.asal_json)["checksum"] == provenance["checksum"]:
        return {"diimpor": False, "sudah_ada": True, "sampai": snapshots[-1]["tanggal"]}
    if existing or await session.scalar(select(LaporanKeuanganTutup.tanggal).limit(1)):
        raise HTTPException(409, "Laporan sudah memiliki saldo/riwayat. Impor tidak menimpa data yang ada.")
    session.add(
        LaporanKeuanganAwal(
            id="global", tanggal=opening.tanggal, saldo_json=opening.saldo.model_dump_json(), created_by=actor.id
        )
    )
    session.add(LaporanKeuanganAsal(id="global", asal_json=json.dumps(provenance), created_by=actor.id))
    for s in snapshots:
        s["created_by"] = actor.id
        cutoff = date.fromisoformat(s["tanggal"])
        raw = json.dumps(s)
        session.add(LaporanKeuanganTutup(tanggal=cutoff, snapshot_json=raw, created_by=actor.id))
        session.add(
            LaporanKeuanganAudit(
                tanggal=cutoff,
                action="import_drive",
                reason=s["input"]["catatan"],
                snapshot_json=raw,
                created_by=actor.id,
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Impor bersamaan terdeteksi; muat ulang laporan")
    return {"diimpor": True, "jumlah": len(snapshots), "sampai": snapshots[-1]["tanggal"], "sumber": provenance}
