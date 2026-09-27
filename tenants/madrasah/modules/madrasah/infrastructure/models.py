"""Isolated ORM models for the madrasah Neon database (multi-role schema)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Yayasan(MadrasahBase):
    """Identitas yayasan yang membawahi satu atau lebih MadrasahUnit. Baris
    tunggal per deployment (produk ini tetap satu Neon DB per pelanggan --
    lihat docstring seeder.py -- jadi satu database hanya pernah punya satu
    yayasan), diambil/dibuat otomatis lewat get_or_create_yayasan(),
    sama seperti pola PengaturanSekolah."""

    __tablename__ = "madrasah_yayasan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False, default="Yayasan")
    alamat: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    unit: Mapped[list["MadrasahUnit"]] = relationship(back_populates="yayasan")


class MadrasahUnit(MadrasahBase):
    """Satu madrasah/unit di bawah Yayasan (mis. "Madrasah Diniyah 1",
    "TPQ Cabang 2"). Pelanggan yang hanya punya satu madrasah otomatis
    punya SATU baris di sini ("Unit Utama", dibuat lewat
    services.ensure_default_unit_and_backfill() saat startup) -- semua
    data existing (Tingkat/Rombel/Santri/Mapel/User) ditandai milik unit
    ini secara otomatis, tanpa migrasi data manual apa pun."""

    __tablename__ = "madrasah_unit"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    yayasan_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_yayasan.id", ondelete="SET NULL"), nullable=True, index=True
    )
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    alamat: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    kepala_unit: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    yayasan: Mapped[Optional["Yayasan"]] = relationship(back_populates="unit")


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
    # Nullable & di-backfill otomatis ke "Unit Utama" (lihat MadrasahUnit).
    # role="yayasan_admin" adalah satu-satunya role yang boleh punya
    # madrasah_unit_id=NULL secara permanen (dia melihat lintas-unit).
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )

    santri_asuh: Mapped[list["SantriMadrasah"]] = relationship(back_populates="orang_tua")
    absensi_dicatat: Mapped[list["AbsensiMadrasah"]] = relationship(back_populates="guru")
    pengumuman: Mapped[list["PengumumanMadrasah"]] = relationship(back_populates="pembuat")
    rombel_asuh: Mapped[list["RombelMadrasah"]] = relationship(back_populates="wali_kelas")


class TahunAjaranMadrasah(MadrasahBase):
    """Tahun ajaran (mis. "2025/2026"). Induk dari SemesterMadrasah -- semua
    entity transaksional (absensi, progres, tagihan, jadwal) di-tag lewat
    semester, bukan langsung ke tahun ajaran, karena madrasah tutup buku per
    semester (2x setahun), bukan per tahun ajaran penuh."""

    __tablename__ = "madrasah_tahun_ajaran"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    tanggal_mulai: Mapped[date] = mapped_column(Date, nullable=False)
    tanggal_selesai: Mapped[date] = mapped_column(Date, nullable=False)
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )

    semester: Mapped[list["SemesterMadrasah"]] = relationship(back_populates="tahun_ajaran")


class SemesterMadrasah(MadrasahBase):
    """Satu periode akademik aktif-atau-ditutup. Hanya SATU semester boleh
    status="aktif" di seluruh database pada satu waktu -- lihat
    services.aktifkan_semester(), yang menonaktifkan semester lain sebelum
    mengaktifkan yang dipilih. Semester "ditutup" mengunci input baru untuk
    ditandai ke periode itu (bukan menghapus data lama)."""

    __tablename__ = "madrasah_semester"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tahun_ajaran_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_tahun_ajaran.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nama: Mapped[str] = mapped_column(String(16), nullable=False)  # "Ganjil" | "Genap"
    tanggal_mulai: Mapped[date] = mapped_column(Date, nullable=False)
    tanggal_selesai: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)  # draft|aktif|ditutup

    tahun_ajaran: Mapped["TahunAjaranMadrasah"] = relationship(back_populates="semester")


class TingkatMadrasah(MadrasahBase):
    __tablename__ = "madrasah_tingkat"
    __table_args__ = (UniqueConstraint("madrasah_unit_id", "nama", name="uq_tingkat_unit_nama"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False)
    urutan: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )

    rombel: Mapped[list["RombelMadrasah"]] = relationship(back_populates="tingkat")


class RombelMadrasah(MadrasahBase):
    __tablename__ = "madrasah_rombel"
    __table_args__ = (UniqueConstraint("madrasah_unit_id", "nama", name="uq_rombel_unit_nama"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False)
    tingkat_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_tingkat.id", ondelete="SET NULL"), nullable=True, index=True
    )
    wali_kelas_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
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
    # aktif | lulus | keluar | pindah. Santri non-aktif tidak lagi muncul di
    # listing default (list_santri) supaya tidak tercampur dengan santri
    # yang masih belajar, tapi barisnya TIDAK dihapus -- riwayat penempatan/
    # absensi/progres/tagihan lama tetap tersimpan untuk histori/alumni.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="aktif", index=True)
    tanggal_status: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )

    kelas: Mapped[Optional["KelasMadrasah"]] = relationship(back_populates="santri")
    rombel: Mapped[Optional["RombelMadrasah"]] = relationship(back_populates="santri")
    orang_tua: Mapped[Optional["UserMadrasah"]] = relationship(back_populates="santri_asuh")
    absensi: Mapped[list["AbsensiMadrasah"]] = relationship(back_populates="santri")
    hafalan: Mapped[list["ProgresHafalan"]] = relationship(back_populates="santri")
    tagihan: Mapped[list["TagihanSyahriyah"]] = relationship(back_populates="santri")
    riwayat_kelas: Mapped[list["RiwayatPenempatanSantri"]] = relationship(back_populates="santri")


class RiwayatPenempatanSantri(MadrasahBase):
    """Histori penempatan santri per rombel. place_santri() menutup baris
    yang masih terbuka (tanggal_keluar IS NULL) untuk santri ini sebelum
    membuka baris baru -- jadi selalu ada paling banyak SATU baris terbuka
    per santri, dan `rombel_id` di SantriMadrasah tetap sekadar cache dari
    baris ini yang terbuka (dibaca lebih cepat, sudah dipakai di banyak
    tempat lain di modul ini)."""

    __tablename__ = "madrasah_riwayat_penempatan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    santri_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_santri.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rombel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_rombel.id", ondelete="CASCADE"), nullable=False, index=True
    )
    semester_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_semester.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tanggal_masuk: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    tanggal_keluar: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)

    santri: Mapped["SantriMadrasah"] = relationship(back_populates="riwayat_kelas")
    rombel: Mapped["RombelMadrasah"] = relationship()


class MapelMadrasah(MadrasahBase):
    __tablename__ = "madrasah_mapel"
    __table_args__ = (UniqueConstraint("madrasah_unit_id", "kode", name="uq_mapel_unit_kode"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kode: Mapped[str] = mapped_column(String(32), nullable=False)
    nama: Mapped[str] = mapped_column(String(128), nullable=False)
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )

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
    semester_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_semester.id", ondelete="SET NULL"), nullable=True, index=True
    )

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
    # Nullable dan diisi otomatis dari semester aktif saat baris dibuat
    # (lihat services._semester_aktif_id). Baris lama (sebelum fitur ini
    # ada) tetap NULL -- tidak perlu backfill manual. Kolom baru di tabel
    # yang sudah ada -> lihat ALTER TABLE ADD COLUMN IF NOT EXISTS di
    # seeder.ensure_madrasah_schema().
    semester_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_semester.id", ondelete="SET NULL"), nullable=True, index=True
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
    semester_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_semester.id", ondelete="SET NULL"), nullable=True, index=True
    )

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
    semester_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_semester.id", ondelete="SET NULL"), nullable=True, index=True
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
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )

    pembuat: Mapped[Optional["UserMadrasah"]] = relationship(back_populates="pengumuman")


class KegiatanMadrasah(MadrasahBase):
    """Kegiatan/program yang ditampilkan di landing page publik (mis.
    Tahfidz, Kajian Kitab Kuning, Ekstrakurikuler) -- diisi admin lewat
    /admin/kegiatan, dipakai marketing/PSB, tidak terkait entitas lain."""

    __tablename__ = "madrasah_kegiatan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    judul: Mapped[str] = mapped_column(String(255), nullable=False)
    deskripsi: Mapped[str] = mapped_column(Text, nullable=False, default="")
    urutan: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    dibuat_pada: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )


class PendaftaranSantri(MadrasahBase):
    """Pengajuan dari calon santri/wali lewat form PSB publik (tanpa login).
    Ditinjau admin lewat /admin/pendaftaran -- tidak otomatis membuat akun
    UserMadrasah/SantriMadrasah; itu tetap keputusan manual admin setelah
    verifikasi (wawancara, berkas fisik, dll), bukan hal yang aman untuk
    diotomasi dari input publik yang belum diverifikasi."""

    __tablename__ = "madrasah_pendaftaran"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama_calon: Mapped[str] = mapped_column(String(255), nullable=False)
    tempat_lahir: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tanggal_lahir: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    nama_orang_tua: Mapped[str] = mapped_column(String(255), nullable=False)
    no_hp: Mapped[str] = mapped_column(String(32), nullable=False)
    alamat: Mapped[str] = mapped_column(Text, nullable=False, default="")
    asal_sekolah: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    catatan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # "baru" | "dihubungi" | "diterima" | "ditolak" -- plain string (bukan DB
    # enum) supaya menambah status baru nanti tidak perlu migrasi ALTER TYPE,
    # sama seperti pola SantriMadrasah.status.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="baru", index=True)
    dibuat_pada: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )


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
    # Nullable: kalau kosong, generate_honor_massal() jatuh ke tarif default
    # (env HONOR_PER_SESI) -- supaya penugasan lama tetap bisa dihitung
    # honornya tanpa perlu admin mengisi tarif satu-satu dulu.
    tarif_per_sesi: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)

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
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    pencatat: Mapped[Optional["UserMadrasah"]] = relationship()


class AkunMadrasah(MadrasahBase):
    """Chart of Accounts minimal, khusus modul madrasah -- daftar tetap dan
    kecil (Kas, Pendapatan SPP, Beban ATK, Beban Honor, dst), bukan COA
    fleksibel seperti modul SIABUMDES. Diseed otomatis oleh
    seeder.seed_akun_default() (dipanggil dari ensure_madrasah_schema),
    idempotent -- tidak menyentuh baris yang sudah ada kalau admin
    mengubah `nama`-nya."""

    __tablename__ = "madrasah_akun"

    kode: Mapped[str] = mapped_column(String(16), primary_key=True)
    nama: Mapped[str] = mapped_column(String(128), nullable=False)
    tipe: Mapped[str] = mapped_column(String(16), nullable=False)  # aset|kewajiban|ekuitas|pendapatan|beban


class JurnalMadrasah(MadrasahBase):
    """Baris jurnal double-entry: setiap baris SELALU punya akun_debit dan
    akun_kredit dengan `jumlah` yang sama (bukan tabel debit/kredit
    terpisah) -- representasi paling sederhana untuk volume transaksi kecil
    madrasah, sambil tetap bisa diringkas jadi laba-rugi/neraca per akun.
    Ditulis otomatis dari pay_spp_manual dan create_buku_kas_entry lewat
    services.catat_jurnal(); tidak ada endpoint untuk menulis jurnal
    manual langsung -- semua jurnal berasal dari transaksi kas/SPP yang
    sudah tercatat di BukuKasMadrasah/TagihanSyahriyah, supaya jurnal dan
    buku kas tidak pernah bisa berbeda."""

    __tablename__ = "madrasah_jurnal"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    akun_debit: Mapped[str] = mapped_column(String(16), ForeignKey("madrasah_akun.kode"), nullable=False, index=True)
    akun_kredit: Mapped[str] = mapped_column(String(16), ForeignKey("madrasah_akun.kode"), nullable=False, index=True)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    keterangan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sumber_tipe: Mapped[str] = mapped_column(String(32), nullable=False, default="")  # "spp" | "buku_kas" | "honor"
    sumber_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dibuat_oleh: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="SET NULL"), nullable=True
    )
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class HonorMengajar(MadrasahBase):
    """Honor bulanan seorang guru UNTUK SATU mapel yang dia ajar (bukan
    digabung semua mapel per guru), dihitung dari jumlah sesi (hari unik dia
    mencatat absensi mapel itu, lihat AbsensiMadrasah.mapel_id) di bulan itu
    dikali tarif per sesi (GuruMapelRombel.tarif_per_sesi, atau tarif
    default kalau kosong) -- setiap mapel bisa punya tarif berbeda, jadi
    tidak digabung jadi satu baris per guru supaya breakdown-nya tetap
    jelas. generate_honor_massal() idempoten per (guru, mapel, bulan)."""

    __tablename__ = "madrasah_honor_mengajar"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    guru_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("madrasah_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mapel_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_mapel.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bulan_tahun: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    jumlah_sesi: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tarif_per_sesi: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    status_bayar: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dibayar_pada: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    madrasah_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("madrasah_unit.id", ondelete="SET NULL"), nullable=True, index=True
    )

    guru: Mapped["UserMadrasah"] = relationship()
    mapel: Mapped[Optional["MapelMadrasah"]] = relationship()


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
    # Ditampilkan di section "Penerimaan Santri Baru" landing page publik.
    info_psb: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kontak_psb: Mapped[str] = mapped_column(String(64), nullable=False, default="")
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
