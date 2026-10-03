"""Log audit dasar & status periode -- dipakai semua service (tanpa dependensi ke service lain)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import event, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    STATUS_DITUTUP,
    BlAuditLog,
    BlTransaksi,
    BlTransfer,
    BlTutupBuku,
)

_BULAN = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober",
          "November", "Desember")


def periode_dari(tanggal: date) -> str:
    return f"{tanggal.year}-{tanggal.month:02d}"


def nama_bulan(periode: str) -> str:
    return f"{_BULAN[int(periode[5:7]) - 1]} {periode[:4]}"


def pesan_bulan_tertutup(periode: str) -> str:
    return f"Bulan {nama_bulan(periode)} sudah tutup buku; catat sebagai koreksi di bulan berjalan"


def _json(nilai: Any) -> Any:
    if isinstance(nilai, dict):
        return {k: _json(v) for k, v in nilai.items()}
    if isinstance(nilai, (list, tuple)):
        return [_json(v) for v in nilai]
    if isinstance(nilai, Decimal):
        return str(nilai)
    if isinstance(nilai, date):
        return nilai.isoformat()
    return nilai


async def catat_audit(
    session: AsyncSession, user_id: str | None, aksi: str, entitas: str, entitas_id: str | None = None, *,
    sebelum: dict | None = None, sesudah: dict | None = None, alasan: str | None = None,
) -> None:
    session.add(
        BlAuditLog(
            user_id=user_id, aksi=aksi, entitas=entitas, entitas_id=entitas_id,
            sebelum=_json(sebelum), sesudah=_json(sesudah), alasan=alasan,
        )
    )


async def periode_tertutup(session: AsyncSession, periode: str) -> bool:
    stmt = select(BlTutupBuku.id).where(BlTutupBuku.periode == periode, BlTutupBuku.status == STATUS_DITUTUP)
    return (await session.execute(stmt)).first() is not None


async def bulan_tertutup(session: AsyncSession, tanggal: date) -> bool:
    """Apakah bulan `tanggal` sudah tutup buku (spesifikasi AB-TB-4)."""
    return await periode_tertutup(session, periode_dari(tanggal))


# --- Pengaman terakhir: tidak ada transaksi/transfer bertanggal di bulan tertutup --------------------------
# Semua uang (manual, pembayaran, penerimaan, gaji, tagihan, sisihan, isi ulang, kiriman, pembatalan) akhirnya
# menulis bl_transaksi atau bl_transfer, jadi penjagaan di tingkat mapper menutup semua endpoint tulis sekaligus.
# Service tetap memanggil `pastikan_bulan_terbuka` lebih awal agar pesannya jelas sebelum ada perubahan.


def _tanggal_tersentuh(target) -> set[date]:
    hasil = {target.tanggal} if target.tanggal else set()
    hist = inspect(target).attrs.tanggal.history
    hasil.update(d for d in (hist.deleted or ()) if d)
    return hasil


def _jaga_periode(connection, target, *, ubah: bool) -> None:
    if ubah and not inspect(target).modified:
        return
    periode = {periode_dari(d) for d in _tanggal_tersentuh(target)}
    if not periode:
        return
    row = connection.execute(
        select(BlTutupBuku.periode).where(BlTutupBuku.periode.in_(periode), BlTutupBuku.status == STATUS_DITUTUP)
    ).first()
    if row is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=pesan_bulan_tertutup(row[0]))


for _model in (BlTransaksi, BlTransfer):
    event.listen(_model, "before_insert", lambda m, c, t: _jaga_periode(c, t, ubah=False))
    event.listen(_model, "before_update", lambda m, c, t: _jaga_periode(c, t, ubah=True))
