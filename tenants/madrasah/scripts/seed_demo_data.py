"""Fills the madrasah database with realistic dummy data so every page in
the frontend has something to show.

This is NOT part of the app's startup path (unlike seeder.ensure_madrasah_
schema / seed_madrasah, which only ever create the two login accounts --
see that module's docstring on why: this product is sold to multiple real
madrasah, so production seeding must stay generic). `seed_demo_data()` is
meant to be triggered once against a trial/demo database only -- either
via the CLI:

    python -m tenants.madrasah.scripts.seed_demo_data

or, when there is no shell access to the deployed container, via the
GET /api/madrasah/admin/seed-demo-data endpoint (ADMIN_ROLES only,
see madrasah_router.py).

Idempotent by a single guard: if any TingkatMadrasah row already exists,
the whole thing is a no-op, so calling it twice never creates duplicates.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password
from tenants.madrasah.modules.madrasah.application.services import (
    AKUN_BEBAN_ATK,
    AKUN_KAS,
    seed_akun_default,
)
from tenants.madrasah.modules.madrasah.infrastructure.models import (
    AbsensiMadrasah,
    BukuKasMadrasah,
    GuruMapelRombel,
    HonorMengajar,
    JadwalMadrasah,
    JurnalMadrasah,
    MapelMadrasah,
    MateriTarget,
    PengumumanMadrasah,
    ProgresHafalan,
    RombelMadrasah,
    SantriMadrasah,
    TagihanSyahriyah,
    TahunAjaranMadrasah,
    SemesterMadrasah,
    TingkatMadrasah,
    UserMadrasah,
)

PASSWORD = "password123"


def _hp(n: int) -> str:
    return f"0813{n:08d}"


async def seed_demo_data(session: AsyncSession) -> dict:
    """Returns {"status": "created"|"already_seeded", ...}. Safe to call
    repeatedly (see module docstring)."""
    existing = (await session.execute(select(TingkatMadrasah.id).limit(1))).scalar_one_or_none()
    if existing:
        return {"status": "already_seeded"}

    await seed_akun_default(session)
    await session.flush()

    today = date.today()

    # --- Tahun ajaran & semester -------------------------------------
    tahun = TahunAjaranMadrasah(
        kode="2025/2026",
        tanggal_mulai=date(2025, 7, 1),
        tanggal_selesai=date(2026, 6, 30),
    )
    session.add(tahun)
    await session.flush()

    semester_ganjil = SemesterMadrasah(
        tahun_ajaran_id=tahun.id, nama="Ganjil",
        tanggal_mulai=date(2025, 7, 1), tanggal_selesai=date(2025, 12, 20),
        status="ditutup",
    )
    semester_genap = SemesterMadrasah(
        tahun_ajaran_id=tahun.id, nama="Genap",
        tanggal_mulai=date(2026, 1, 5), tanggal_selesai=date(2026, 6, 30),
        status="aktif",
    )
    session.add_all([semester_ganjil, semester_genap])
    await session.flush()

    # --- Tingkat & rombel ----------------------------------------------
    tingkat_names = ["Jilid 1", "Jilid 2", "Jilid 3"]
    tingkats = [TingkatMadrasah(nama=n, urutan=i + 1) for i, n in enumerate(tingkat_names)]
    session.add_all(tingkats)
    await session.flush()

    # --- Guru / wali kelas / staff users --------------------------------
    wali_kelas_users = [
        UserMadrasah(nama=f"Ustadz {n}", no_hp=_hp(100 + i), password_hash=hash_password(PASSWORD), role="wali_kelas")
        for i, n in enumerate(["Ahmad Fauzi", "Siti Aminah", "Budi Santoso"])
    ]
    guru_mapel_users = [
        UserMadrasah(nama=f"Ustadzah {n}", no_hp=_hp(200 + i), password_hash=hash_password(PASSWORD), role="guru")
        for i, n in enumerate(["Nur Hasanah", "Muhammad Idris"])
    ]
    staff_users = [
        UserMadrasah(nama="Kepala Sekolah Demo", no_hp=_hp(300), password_hash=hash_password(PASSWORD), role="kepala_sekolah"),
        UserMadrasah(nama="Bendahara Demo", no_hp=_hp(301), password_hash=hash_password(PASSWORD), role="bendahara"),
        UserMadrasah(nama="Kurikulum Demo", no_hp=_hp(302), password_hash=hash_password(PASSWORD), role="kurikulum"),
        UserMadrasah(nama="Yayasan Demo", no_hp=_hp(303), password_hash=hash_password(PASSWORD), role="yayasan_admin"),
    ]
    session.add_all([*wali_kelas_users, *guru_mapel_users, *staff_users])
    await session.flush()

    rombels = [
        RombelMadrasah(nama=f"Rombel {tingkat_names[i]} A", tingkat_id=tingkats[i].id, wali_kelas_id=wali_kelas_users[i].id)
        for i in range(3)
    ]
    session.add_all(rombels)
    await session.flush()

    # --- Santri + wali santri -------------------------------------------
    santri_first_names = [
        "Abdullah", "Fatimah", "Zainab", "Umar", "Aisyah", "Yusuf", "Khadijah", "Ali",
        "Hafsa", "Bilal", "Maryam", "Hamza", "Ruqayyah", "Ismail", "Safiyyah",
    ]
    santris: list[SantriMadrasah] = []
    wali_santri_users: list[UserMadrasah] = []
    for i, first in enumerate(santri_first_names):
        wali = UserMadrasah(
            nama=f"Bapak/Ibu {first}", no_hp=_hp(400 + i),
            password_hash=hash_password(PASSWORD), role="wali_santri",
        )
        wali_santri_users.append(wali)
    session.add_all(wali_santri_users)
    await session.flush()

    for i, first in enumerate(santri_first_names):
        rombel = rombels[i % len(rombels)]
        santri = SantriMadrasah(
            nama=f"{first} bin/binti Santri",
            rombel_id=rombel.id,
            orang_tua_id=wali_santri_users[i].id,
            status="aktif",
        )
        santris.append(santri)
    session.add_all(santris)
    await session.flush()

    # --- Mapel + materi ---------------------------------------------------
    mapel_defs = [("QUR", "Al-Qur'an"), ("TAJ", "Tajwid"), ("AKH", "Akhlak")]
    mapels = [MapelMadrasah(kode=k, nama=n) for k, n in mapel_defs]
    session.add_all(mapels)
    await session.flush()

    for mapel in mapels:
        session.add_all([
            MateriTarget(mapel_id=mapel.id, judul=f"Materi {i + 1} - {mapel.nama}", urutan=i + 1)
            for i in range(3)
        ])
    await session.flush()

    # --- Jadwal + penugasan guru mapel ------------------------------------
    hari_list = ["Senin", "Selasa", "Rabu"]
    for i, rombel in enumerate(rombels):
        for j, mapel in enumerate(mapels):
            session.add(JadwalMadrasah(
                rombel_id=rombel.id, mapel_id=mapel.id,
                hari=hari_list[j % len(hari_list)],
                jam_mulai="07:30", jam_selesai="08:30",
                semester_id=semester_genap.id,
            ))
            session.add(GuruMapelRombel(
                guru_id=guru_mapel_users[j % len(guru_mapel_users)].id,
                mapel_id=mapel.id, rombel_id=rombel.id,
                tarif_per_sesi=Decimal("25000"),
            ))
    await session.flush()

    # --- Absensi + progres hafalan (7 hari terakhir) ----------------------
    for offset in range(7):
        tanggal = today - timedelta(days=offset)
        for i, santri in enumerate(santris):
            status = "hadir" if (i + offset) % 6 != 0 else "izin"
            session.add(AbsensiMadrasah(
                tanggal=tanggal, status=status, santri_id=santri.id,
                guru_id=wali_kelas_users[i % len(wali_kelas_users)].id,
                semester_id=semester_genap.id,
            ))
        if offset % 2 == 0:
            for santri in santris:
                session.add(ProgresHafalan(
                    tanggal=tanggal, santri_id=santri.id, tipe="hafalan",
                    capaian="Juz 30 - An-Naba s.d. Al-'Abasa",
                    catatan_guru="Lancar, tajwid perlu dilatih lagi.",
                    semester_id=semester_genap.id,
                ))

    # --- Tagihan syahriyah (bulan berjalan + bulan lalu) --------------------
    bulan_ini = today.strftime("%Y-%m")
    bulan_lalu = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    for i, santri in enumerate(santris):
        session.add(TagihanSyahriyah(
            bulan_tahun=bulan_lalu, nominal=Decimal("150000"),
            status_bayar=True, dibayar_pada=datetime.now(timezone.utc),
            santri_id=santri.id, semester_id=semester_genap.id,
        ))
        session.add(TagihanSyahriyah(
            bulan_tahun=bulan_ini, nominal=Decimal("150000"),
            status_bayar=(i % 3 != 0), santri_id=santri.id, semester_id=semester_genap.id,
        ))

    # --- Pengumuman ----------------------------------------------------------
    session.add_all([
        PengumumanMadrasah(
            judul="Libur Semester Genap", isi="Libur semester genap dimulai 1 Juli 2026.",
            tanggal=today, dibuat_by=staff_users[0].id,
        ),
        PengumumanMadrasah(
            judul="Pembayaran Syahriyah", isi="Mohon melunasi syahriyah bulan ini sebelum tanggal 10.",
            tanggal=today - timedelta(days=3), dibuat_by=staff_users[1].id,
        ),
    ])

    # --- Buku kas + jurnal (contoh transaksi keluar) --------------------------
    session.add(BukuKasMadrasah(
        tanggal=today, tipe="keluar", kategori="ATK",
        jumlah=Decimal("350000"), keterangan="Pembelian ATK bulanan",
        dicatat_oleh=staff_users[1].id,
    ))
    session.add(JurnalMadrasah(
        tanggal=today, akun_debit=AKUN_BEBAN_ATK, akun_kredit=AKUN_KAS,
        jumlah=Decimal("350000"), keterangan="Pembelian ATK bulanan",
        sumber_tipe="buku_kas", dibuat_oleh=staff_users[1].id,
    ))

    # --- Honor mengajar --------------------------------------------------------
    for guru in guru_mapel_users:
        session.add(HonorMengajar(
            guru_id=guru.id, mapel_id=mapels[0].id, bulan_tahun=bulan_lalu,
            jumlah_sesi=8, tarif_per_sesi=Decimal("25000"), total=Decimal("200000"),
            status_bayar=True, dibayar_pada=datetime.now(timezone.utc),
        ))

    await session.commit()
    return {
        "status": "created",
        "santri": len(santris),
        "rombel": len(rombels),
        "mapel": len(mapels),
        "users": len(wali_kelas_users) + len(guru_mapel_users) + len(staff_users) + len(wali_santri_users),
        "login_demo": {
            "wali_kelas": _hp(100),
            "guru_mapel": _hp(200),
            "bendahara": _hp(301),
            "wali_santri": _hp(400),
            "password": PASSWORD,
        },
    }


async def main() -> None:
    from tenants.madrasah.modules.madrasah.infrastructure.database import SessionLocal, engine

    if engine is None or SessionLocal is None:
        raise RuntimeError("DATABASE_URL_MADRASAH is not configured")

    async with SessionLocal() as session:
        result = await seed_demo_data(session)
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
