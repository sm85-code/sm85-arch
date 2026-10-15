"""Drive's held balance means the remaining wallets of these three shops."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from . import workflows
from .financial_reports import ZERO, money
from .financial_shopee import shop_key
from ..infrastructure.models import AkunMarketplace

HELD_SHOPS = ("BUMI LESTARI INDONESIA", "CAHAYA LANGIT IND", "RESTU BUMI IND")


async def held_balance(session, cutoff):
    accounts = (await session.execute(select(
        AkunMarketplace.id, AkunMarketplace.nama_toko, AkunMarketplace.id_toko_eksternal,
    ).where(AkunMarketplace.platform == "shopee"))).all()
    rows = []
    for name in HELD_SHOPS:
        matches = [a for a in accounts if shop_key(a.nama_toko) == shop_key(name)]
        row = {"nama": name, "nominal": None, "currency": None, "transaksi_at": None,
               "diperiksa_at": None, "keterangan": None}
        if len(matches) != 1:
            row["keterangan"] = "Akun toko belum ditemukan" if not matches else "Nama cocok dengan lebih dari satu akun"
        elif not matches[0].id_toko_eksternal:
            row["keterangan"] = "Toko belum terhubung"
        elif cutoff >= datetime.now(ZoneInfo("Asia/Jakarta")).date():
            row["keterangan"] = "Tanggal laporan belum berakhir"
        else:
            try:
                snapshot = await workflows.shop_balance_snapshot(session, matches[0].id, cutoff=cutoff)
                row.update(currency=snapshot["currency"], transaksi_at=snapshot["transaksi_at"],
                           diperiksa_at=snapshot["diperiksa_at"])
                if snapshot["saldo_terakhir"] is not None and snapshot["currency"] == "IDR":
                    row["nominal"] = str(money(snapshot["saldo_terakhir"]))
                else:
                    row["keterangan"] = "Riwayat saldo rupiah pada cut-off belum tersedia"
            except HTTPException:
                row["keterangan"] = "Riwayat saldo belum dapat dibaca; periksa koneksi toko"
        rows.append(row)
    complete = all(r["nominal"] is not None for r in rows)
    return {
        "saldo_toko": str(sum((money(r["nominal"]) for r in rows), ZERO)) if complete else None,
        "cutoff": cutoff.isoformat(), "lengkap": complete, "rincian": rows,
    }
