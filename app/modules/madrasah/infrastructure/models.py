"""Isolated ORM models for the madrasah Neon database."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.modules.madrasah.infrastructure.database import MadrasahBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserMadrasah(MadrasahBase):
    __tablename__ = "madrasah_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    no_hp: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # guru | wali_santri
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    santri_asuh: Mapped[list["SantriMadrasah"]] = relationship(back_populates="orang_tua")
    absensi_dicatat: Mapped[list["AbsensiMadrasah"]] = relationship(back_populates="guru")
    pengumuman: Mapped[list["PengumumanMadrasah"]] = relationship(back_populates="pembuat")


class KelasMadrasah(MadrasahBase):
    __tablename__ = "madrasah_kelas"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama_kelas: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    santri: Mapped[list["SantriMadrasah"]] = relationship(back_populates="kelas")


class SantriMadrasah(MadrasahBase):
    __tablename__ = "madrasah_santri"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    kelas_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_kelas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    orang_tua_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    kelas: Mapped[Optional["KelasMadrasah"]] = relationship(back_populates="santri")
    orang_tua: Mapped[Optional["UserMadrasah"]] = relationship(back_populates="santri_asuh")
    absensi: Mapped[list["AbsensiMadrasah"]] = relationship(back_populates="santri")
    hafalan: Mapped[list["ProgresHafalan"]] = relationship(back_populates="santri")
    tagihan: Mapped[list["TagihanSyahriyah"]] = relationship(back_populates="santri")


class AbsensiMadrasah(MadrasahBase):
    __tablename__ = "madrasah_absensi"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # hadir | sakit | izin | alpa
    santri_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_santri.id", ondelete="CASCADE"), nullable=False, index=True
    )
    guru_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    santri: Mapped["SantriMadrasah"] = relationship(back_populates="absensi")
    guru: Mapped[Optional["UserMadrasah"]] = relationship(back_populates="absensi_dicatat")


class ProgresHafalan(MadrasahBase):
    __tablename__ = "madrasah_progres_hafalan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    santri_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_santri.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tipe: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    capaian: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    catatan_guru: Mapped[str] = mapped_column(Text, nullable=False, default="")

    santri: Mapped["SantriMadrasah"] = relationship(back_populates="hafalan")


class TagihanSyahriyah(MadrasahBase):
    __tablename__ = "madrasah_tagihan_syahriyah"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    bulan_tahun: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # YYYY-MM
    nominal: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    status_bayar: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    santri_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_santri.id", ondelete="CASCADE"), nullable=False, index=True
    )

    santri: Mapped["SantriMadrasah"] = relationship(back_populates="tagihan")


class PengumumanMadrasah(MadrasahBase):
    __tablename__ = "madrasah_pengumuman"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    judul: Mapped[str] = mapped_column(String(255), nullable=False)
    isi: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    dibuat_by: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    pembuat: Mapped[Optional["UserMadrasah"]] = relationship(back_populates="pengumuman")
