"""Madrasah use-cases against the isolated Neon session."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


class MadrasahAuthError(Exception):
    pass


class MadrasahNotFoundError(Exception):
    pass


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
