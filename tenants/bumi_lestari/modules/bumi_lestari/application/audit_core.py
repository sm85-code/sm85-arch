"""Log audit dasar & status periode -- dipakai semua service (tanpa dependensi ke service lain)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAuditLog


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


async def bulan_tertutup(session: AsyncSession, tanggal: date) -> bool:
    """Apakah bulan `tanggal` sudah tutup buku. Tutup buku dibangun di Fase 2; sampai saat itu belum ada
    bulan yang tertutup. Semua aturan "tidak boleh di bulan tertutup" sudah memanggil fungsi ini."""
    return False
