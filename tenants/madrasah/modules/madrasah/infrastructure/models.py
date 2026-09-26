"""Isolated ORM models for the madrasah Neon database (multi-role schema)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase


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
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Dibandingkan dengan klaim "sv" di JWT saat setiap request (lihat
    # get_current_user_madrasah). Dinaikkan setiap kali password diganti atau
    # akun di-nonaktifkan/hapus paksa, supaya token lama langsung tidak valid
    # tanpa perlu tabel blacklist token terpisah. Kolom baru di tabel yang
    # sudah ada -> perlu ALTER TABLE ADD COLUMN IF NOT EXISTS untuk database
    # produksi lama (lihat self-heal di login_by_phone).
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    santri_asuh: Mapped[list["SantriMadrasah"]] = relationship(back_populates="orang_tua")
    absensi_dicatat: Mapped[list["AbsensiMadrasah"]] = relationship(back_populates="guru")
    pengumuman: Mapped[list["PengumumanMadrasah"]] = relationship(back_populates="pembuat")
    rombel_asuh: Mapped[list["RombelMadrasah"]] = relationship(back_populates="wali_kelas")


class TingkatMadrasah(MadrasahBase):
    __tablename__ = "madrasah_tingkat"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    urutan: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    rombel: Mapped[list["RombelMadrasah"]] = relationship(back_populates="tingkat")


class RombelMadrasah(MadrasahBase):
    __tablename__ = "madrasah_rombel"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    tingkat_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_tingkat.id", ondelete="SET NULL"), nullable=True, index=True
    )
    wali_kelas_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    tingkat: Mapped[Optional["TingkatMadrasah"]] = relationship(back_populates="rombel")
    wali_kelas: Mapped[Optional["UserMadrasah"]] = relationship(back_populates="rombel_asuh")
    santri: Mapped[list["SantriMadrasah"]] = relationship(back_populates="rombel")
    jadwal: Mapped[list["JadwalMadrasah"]] = relationship(back_populates="rombel")


class KelasMadrasah(MadrasahBase):
    """Legacy alias table kept so older Neon rows / frontend /kelas keep working."""

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
    rombel_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_rombel.id", ondelete="SET NULL"), nullable=True, index=True
    )
    orang_tua_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    kelas: Mapped[Optional["KelasMadrasah"]] = relationship(back_populates="santri")
    rombel: Mapped[Optional["RombelMadrasah"]] = relationship(back_populates="santri")
    orang_tua: Mapped[Optional["UserMadrasah"]] = relationship(back_populates="santri_asuh")
    absensi: Mapped[list["AbsensiMadrasah"]] = relationship(back_populates="santri")
    hafalan: Mapped[list["ProgresHafalan"]] = relationship(back_populates="santri")
    tagihan: Mapped[list["TagihanSyahriyah"]] = relationship(back_populates="santri")


class MapelMadrasah(MadrasahBase):
    __tablename__ = "madrasah_mapel"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kode: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    nama: Mapped[str] = mapped_column(String(128), nullable=False)

    materi: Mapped[list["MateriTarget"]] = relationship(back_populates="mapel")
    jadwal: Mapped[list["JadwalMadrasah"]] = relationship(back_populates="mapel")


class MateriTarget(MadrasahBase):
    __tablename__ = "madrasah_materi_target"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    mapel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_mapel.id", ondelete="CASCADE"), nullable=False, index=True
    )
    judul: Mapped[str] = mapped_column(String(255), nullable=False)
    urutan: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    mapel: Mapped["MapelMadrasah"] = relationship(back_populates="materi")


class JadwalMadrasah(MadrasahBase):
    __tablename__ = "madrasah_jadwal"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    rombel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_rombel.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mapel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_mapel.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hari: Mapped[str] = mapped_column(String(16), nullable=False)
    jam_mulai: Mapped[str] = mapped_column(String(8), nullable=False, default="07:00")
    jam_selesai: Mapped[str] = mapped_column(String(8), nullable=False, default="08:00")

    rombel: Mapped["RombelMadrasah"] = relationship(back_populates="jadwal")
    mapel: Mapped["MapelMadrasah"] = relationship(back_populates="jadwal")


class AbsensiMadrasah(MadrasahBase):
    __tablename__ = "madrasah_absensi"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    santri_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_santri.id", ondelete="CASCADE"), nullable=False, index=True
    )
    guru_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Nullable: absensi lama (wali kelas, 1x/hari per rombel) tidak punya mapel_id.
    # Absensi baru dari guru mapel (per sesi mapel) mengisi field ini.
    mapel_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_mapel.id", ondelete="SET NULL"), nullable=True, index=True
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
    mapel_id: Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("madrasah_mapel.id", ondelete="SET NULL"), nullable=True)
    materi_id: Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("madrasah_materi_target.id", ondelete="SET NULL"), nullable=True)

    santri: Mapped["SantriMadrasah"] = relationship(back_populates="hafalan")


class TagihanSyahriyah(MadrasahBase):
    __tablename__ = "madrasah_tagihan_syahriyah"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    bulan_tahun: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    nominal: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    status_bayar: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dibayar_pada: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Approval bertingkat: wali kelas "mengajukan" (mencatat sudah terima
    # uang tunai) lewat diajukan_oleh/diajukan_pada; status_bayar baru jadi
    # True setelah bendahara approve lewat alur pay yang sudah ada.
    diajukan_oleh: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    diajukan_pada: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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


class GuruMapelRombel(MadrasahBase):
    """Penugasan: guru X mengajar mapel Y di rombel Z. Many-to-many, baru,
    tidak menyentuh/mengubah tabel manapun yang sudah ada. Dipakai untuk
    men-scope portal Guru Mapel supaya guru hanya melihat rombel & santri
    yang benar-benar dia ajar, bukan wali_kelas_id (itu tetap urusan
    RombelMadrasah.wali_kelas_id, tidak berubah)."""

    __tablename__ = "madrasah_guru_mapel_rombel"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    guru_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mapel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_mapel.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rombel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_rombel.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    guru: Mapped["UserMadrasah"] = relationship()
    mapel: Mapped["MapelMadrasah"] = relationship()
    rombel: Mapped["RombelMadrasah"] = relationship()


class BukuKasMadrasah(MadrasahBase):
    """Kas satu pintu (single entry, bukan pembukuan double-entry): baris
    "masuk" dibuat otomatis saat SPP dibayar lunas (lihat pay_spp_manual di
    services.py), baris "keluar" dicatat manual oleh Kepala Sekolah/Admin
    untuk ATK dan honor guru ngaji."""

    __tablename__ = "madrasah_buku_kas"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    tipe: Mapped[str] = mapped_column(String(16), nullable=False)
    kategori: Mapped[str] = mapped_column(String(64), nullable=False)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    keterangan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dicatat_oleh: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    pencatat: Mapped[Optional["UserMadrasah"]] = relationship()


class PengaturanSekolah(MadrasahBase):
    """Baris tunggal (singleton) berisi identitas madrasah yang dipakai
    frontend (nama, logo, alamat) -- supaya produk ini bisa dijual ke
    madrasah lain tanpa mengubah kode/hardcode nama sekolah tertentu.
    Diambil/di-buat otomatis lewat get_pengaturan() kalau baris belum ada,
    jadi tidak perlu migrasi data manual saat modul ini pertama kali
    dipasang di database yang sudah berjalan."""

    __tablename__ = "madrasah_pengaturan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama_sekolah: Mapped[str] = mapped_column(String(255), nullable=False, default="Madrasah Diniyah")
    tagline: Mapped[str] = mapped_column(String(255), nullable=False, default="Sistem Informasi Madrasah")
    logo_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    alamat: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class PesanMadrasah(MadrasahBase):
    """Komunikasi antara wali kelas dan wali santri, per santri (bukan
    percakapan bebas antar-user). Baru, tidak menyentuh tabel manapun."""

    __tablename__ = "madrasah_pesan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    santri_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_santri.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dari_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    isi: Mapped[str] = mapped_column(Text, nullable=False)
    dibuat_pada: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    santri: Mapped["SantriMadrasah"] = relationship()
    dari_user: Mapped["UserMadrasah"] = relationship()


class AuditLogMadrasah(MadrasahBase):
    """Jejak audit untuk aksi sensitif (login, keuangan, hapus akun/data,
    reset destruktif). Tabel baru, additive -- tidak mengubah tabel manapun
    yang sudah ada. dilihat via GET /admin/audit-log (ADMIN_ROLES saja)."""

    __tablename__ = "madrasah_audit_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    waktu: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    aktor_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    aktor_nama: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    aktor_role: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    aksi: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entitas: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    entitas_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    keterangan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
