# Madrasah module (isolated)

Lives on Neon via `DATABASE_URL_MADRASAH`. Mounted only at `/api/madrasah`.
Does not import or mutate SIABUMDES / BUMDes tables.

## Roles
- `/admin` Kepala Sekolah: tingkat, rombel, guru, penempatan santri
- `/kurikulum` mapel, materi target (PATCH inline), jadwal
- `/bendahara` SPP generate + pay cash
- `/wali-kelas` absensi massal + progres (dropdown mapel/materi)
- `/wali-santri` read-only time series + riwayat bayar

## Seed
`GET /api/madrasah/seed-now` — SITI MUKAROMAH MASKUR / 082315394967 / password123 as wali kelas of Jilid 1 B; santri MUHAMMAD FAQIH AL MURTADLO.
