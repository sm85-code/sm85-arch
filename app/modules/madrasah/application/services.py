"""Madrasah use-cases against the isolated Neon session."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.madrasah.application.schemas import AbsenBulkRequest, LoginRequest, ProgresCreateRequest
from app.modules.madrasah.infrastructure.models import (
    AbsensiMadrasah,
    KelasMadrasah,
    PengumumanMadrasah,
    ProgresHafalan,
    SantriMadrasah,
    TagihanSyahriyah,
    UserMadrasah,
)
from shared.security import verify_password

STATUS_BELUM = "Belum Bayar"
STATUS_LUNAS = "Lunas"
DEFAULT_SPP_NOMINAL = Decimal(os.getenv("SPP_NOMINAL", "50000"))


class MadrasahAuthError(Exception):
    pass


class MadrasahNotFoundError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bulan_tahun(value: date | None = None) -> str:
    current = value or date.today()
    return current.strftime("%Y-%m")


def tagihan_status_label(row: TagihanSyahriyah) -> str:
    return STATUS_LUNAS if row.status_bayar else STATUS_BELUM


def tagihan_out(row: TagihanSyahriyah, nama: str | None = None) -> dict:
    paid_at = row.dibayar_pada.isoformat() if row.dibayar_pada else None
    return {
        "id": row.id,
        "santri_id": row.santri_id,
        "nama": nama or (row.santri.nama if getattr(row, "santri", None) else "-"),
        "nama_santri": nama or (row.santri.nama if getattr(row, "santri", None) else "-"),
        "bulan_tahun": row.bulan_tahun,
        "nominal": float(row.nominal),
        "status": tagihan_status_label(row),
        "status_bayar": row.status_bayar,
        "lunas": row.status_bayar,
        "dibayar_pada": paid_at,
    }


async def login_by_phone(session: AsyncSession, payload: LoginRequest) -> UserMadrasah:
    user = (
        await session.execute(select(UserMadrasah).where(UserMadrasah.no_hp == payload.no_hp.strip()))
    ).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise MadrasahAuthError("Nomor HP atau password salah")
    return user


async def list_kelas(session: AsyncSession) -> list[KelasMadrasah]:
    result = await session.execute(select(KelasMadrasah).order_by(KelasMadrasah.nama_kelas))
    return list(result.scalars())


async def list_santri(session: AsyncSession, kelas_id: str | None = None) -> list[SantriMadrasah]:
    stmt = select(SantriMadrasah).order_by(SantriMadrasah.nama)
    if kelas_id:
        stmt = stmt.where(SantriMadrasah.kelas_id == kelas_id)
    result = await session.execute(stmt)
    return list(result.scalars())


async def bulk_insert_absensi(session: AsyncSession, payload: AbsenBulkRequest) -> list[AbsensiMadrasah]:
    guru = await session.get(UserMadrasah, payload.guru_id)
    if not guru or guru.role != "guru":
        raise MadrasahNotFoundError("Guru tidak ditemukan")
    rows: list[AbsensiMadrasah] = []
    for item in payload.items:
        row = AbsensiMadrasah(
            tanggal=payload.tanggal,
            status=item.status,
            santri_id=item.santri_id,
            guru_id=payload.guru_id,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def create_progres(session: AsyncSession, payload: ProgresCreateRequest) -> ProgresHafalan:
    santri = await session.get(SantriMadrasah, payload.santri_id)
    if not santri:
        raise MadrasahNotFoundError("Santri tidak ditemukan")
    row = ProgresHafalan(
        tanggal=payload.tanggal,
        santri_id=payload.santri_id,
        tipe=payload.tipe,
        capaian=payload.capaian,
        catatan_guru=payload.catatan_guru or "",
    )
    session.add(row)
    await session.flush()
    return row


async def list_tagihan(session: AsyncSession, santri_id: str) -> list[TagihanSyahriyah]:
    result = await session.execute(
        select(TagihanSyahriyah)
        .where(TagihanSyahriyah.santri_id == santri_id)
        .order_by(TagihanSyahriyah.bulan_tahun.desc())
    )
    return list(result.scalars())


async def list_pengumuman(session: AsyncSession, limit: int = 50) -> list[PengumumanMadrasah]:
    result = await session.execute(
        select(PengumumanMadrasah).order_by(PengumumanMadrasah.tanggal.desc()).limit(limit)
    )
    return list(result.scalars())


async def generate_spp_massal(session: AsyncSession) -> list[TagihanSyahriyah]:
    """Insert current-month bills for every santri; skip if already billed."""
    await session.execute(
        text(
            "ALTER TABLE IF EXISTS madrasah_tagihan_syahriyah "
            "ADD COLUMN IF NOT EXISTS dibayar_pada TIMESTAMPTZ NULL"
        )
    )
    period = _bulan_tahun()
    santri_rows = list(
        (await session.execute(select(SantriMadrasah).order_by(SantriMadrasah.nama))).scalars()
    )
    existing = {
        row.santri_id
        for row in (
            await session.execute(
                select(TagihanSyahriyah).where(TagihanSyahriyah.bulan_tahun == period)
            )
        ).scalars()
    }
    created: list[TagihanSyahriyah] = []
    for santri in santri_rows:
        if santri.id in existing:
            continue
        row = TagihanSyahriyah(
            bulan_tahun=period,
            nominal=DEFAULT_SPP_NOMINAL,
            status_bayar=False,
            dibayar_pada=None,
            santri_id=santri.id,
        )
        session.add(row)
        created.append(row)
    await session.flush()

    result = await session.execute(
        select(TagihanSyahriyah)
        .options(selectinload(TagihanSyahriyah.santri))
        .where(TagihanSyahriyah.bulan_tahun == period)
        .order_by(TagihanSyahriyah.santri_id)
    )
    return list(result.scalars())


async def pay_spp_manual(session: AsyncSession, target_id: str) -> TagihanSyahriyah:
    """Mark a bill Lunas. `target_id` may be tagihan.id or santri.id."""
    row = await session.get(TagihanSyahriyah, target_id)
    if row is None:
        period = _bulan_tahun()
        row = (
            await session.execute(
                select(TagihanSyahriyah)
                .where(
                    TagihanSyahriyah.santri_id == target_id,
                    TagihanSyahriyah.status_bayar.is_(False),
                    TagihanSyahriyah.bulan_tahun == period,
                )
                .order_by(TagihanSyahriyah.bulan_tahun.desc())
            )
        ).scalar_one_or_none()
        if row is None:
            row = (
                await session.execute(
                    select(TagihanSyahriyah)
                    .where(
                        TagihanSyahriyah.santri_id == target_id,
                        TagihanSyahriyah.status_bayar.is_(False),
                    )
                    .order_by(TagihanSyahriyah.bulan_tahun.desc())
                )
            ).scalar_one_or_none()
    if row is None:
        raise MadrasahNotFoundError("Tagihan SPP tidak ditemukan")
    row.status_bayar = True
    row.dibayar_pada = _utcnow()
    await session.flush()
    await session.refresh(row, attribute_names=["santri"])
    return row
