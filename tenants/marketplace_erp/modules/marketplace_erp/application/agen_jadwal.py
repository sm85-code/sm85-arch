"""Pause and resume campaigns outside their allowed WIB hours.

The background loop stays off unless AGEN_IKLAN_JADWAL_MENIT is a positive number.
Each schedule row is also off until ``aktif`` is set. STOP still blocks the pause/resume.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_aksi
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models_agen import AgenIklanJadwal

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))
ACTOR = SimpleNamespace(id="jadwal", username="jadwal", nama="Jadwal iklan")


def interval_seconds() -> float:
    """AGEN_IKLAN_JADWAL_MENIT. 0 (the default) or anything invalid keeps the loop off."""
    raw = os.getenv("AGEN_IKLAN_JADWAL_MENIT", "0").strip()
    try:
        menit = float(raw)
    except ValueError:
        return 0.0
    return max(menit, 0.0) * 60


class JadwalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: int = Field(gt=0)
    jam: list[int] = Field(min_length=1, max_length=24)
    aktif: bool = False


def _jam(nilai: list[int]) -> list[int]:
    if any(type(j) is not int or not 0 <= j <= 23 for j in nilai):
        raise HTTPException(status_code=422, detail="Jam harus 0 sampai 23")
    return sorted(set(nilai))


def _keluar(row: AgenIklanJadwal) -> dict:
    return {
        "id": row.id,
        "akun_id": row.akun_id,
        "campaign_id": row.campaign_id,
        "jam": [int(j) for j in json.loads(row.jam_json or "[]")],
        "aktif": bool(row.aktif),
        "dijeda_jadwal": bool(row.dijeda_jadwal),
    }


async def daftar(session: AsyncSession, akun_id: str) -> dict:
    rows = (await session.execute(
        select(AgenIklanJadwal).where(AgenIklanJadwal.akun_id == akun_id).order_by(AgenIklanJadwal.campaign_id)
    )).scalars().all()
    return {"items": [_keluar(r) for r in rows], "loop_detik": interval_seconds()}


async def simpan(session: AsyncSession, akun_id: str, body: JadwalIn) -> dict:
    jam = _jam(body.jam)
    row = (await session.execute(
        select(AgenIklanJadwal).where(AgenIklanJadwal.akun_id == akun_id, AgenIklanJadwal.campaign_id == str(body.campaign_id))
    )).scalar_one_or_none()
    if row is None:
        row = AgenIklanJadwal(akun_id=akun_id, campaign_id=str(body.campaign_id))
        session.add(row)
    row.jam_json = json.dumps(jam)
    row.aktif = body.aktif
    if not body.aktif:
        row.dijeda_jadwal = False
    await session.flush()
    return _keluar(row)


async def sekali(session: AsyncSession, sekarang: datetime | None = None) -> list[dict]:
    """One pass. Outside the allowed hours, pause; inside them, resume only what this job paused."""
    jam = (sekarang or datetime.now(WIB)).astimezone(WIB)
    rows = (await session.execute(select(AgenIklanJadwal).where(AgenIklanJadwal.aktif.is_(True)))).scalars().all()
    hasil = []
    for row in rows:
        boleh = jam.hour in set(json.loads(row.jam_json or "[]"))
        if boleh and not row.dijeda_jadwal:
            continue
        if not boleh and row.dijeda_jadwal:
            continue
        akun = await session.get(AkunMarketplace, row.akun_id)
        if akun is None:
            continue
        jenis = "lanjut" if boleh else "jeda"
        try:
            await agen_aksi.terapkan(
                session, akun,
                agen_aksi.AksiIn(
                    jenis=jenis, campaign_id=int(row.campaign_id), kering=False,
                    alasan="jadwal WIB", request_id=f"jadwal-{row.campaign_id}-{jam.strftime('%Y%m%d%H%M')}-{jenis}",
                ),
                ACTOR, sumber="jadwal",
            )
        except HTTPException as exc:
            hasil.append({"campaign_id": row.campaign_id, "jenis": jenis, "gagal": str(exc.detail)})
            continue
        row.dijeda_jadwal = jenis == "jeda"
        await session.commit()
        hasil.append({"campaign_id": row.campaign_id, "jenis": jenis, "gagal": None})
    return hasil


async def jalankan_berkala(session_factory, interval: float) -> None:
    while True:
        try:
            async with session_factory() as session:
                await sekali(session)
        except Exception:
            logger.exception("Jadwal iklan gagal; diulang pada putaran berikut")
        await asyncio.sleep(interval)
