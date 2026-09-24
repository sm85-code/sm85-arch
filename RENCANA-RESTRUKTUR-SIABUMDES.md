# Rencana: Rapikan Struktur Backend — Satukan Semua Milik Siabumdes

> Disusun dari sesi analisis: siabumdes TETAP jadi inti aplikasi (bukan dipindah ke `tenants/`).
> Tujuan: `modules/identity`, `modules/uu05_inventory`, dan file-file di `shared/`/`adapters/`
> yang ternyata cuma dipakai siabumdes, disatukan ke dalam `modules/siabumdes/`.

## Latar belakang (kesimpulan dari analisis)

- `modules/` berisi 3 paket: `identity`, `siabumdes`, `uu05_inventory` — bukan cuma siabumdes.
- `modules/identity` dan `modules/uu05_inventory` **konseptual bagian dari siabumdes**
  (identity = auth/user/tutup-buku siabumdes; uu05_inventory = menu Inventory siabumdes
  untuk unit usaha UU05), tapi diletakkan terpisah.
- `tenants/madrasah` dan `tenants/toko` **fully independen** — auth sendiri, database sendiri,
  zero import ke `modules.identity`/`modules.siabumdes`.
- Ditemukan bug: `modules/identity/application/services.py` sempat mengimpor balik
  `from modules.siabumdes.infrastructure.models import Transaction, UnitUsaha` — arah
  ketergantungan terbalik (core→tenant-like). **Sudah diperbaiki** di PR #72 (sekaligus
  menghapus 2 fungsi mati `close_period`/`reopen_period` versi lama yang sudah digantikan
  oleh `run_monthly_close`/`undo_monthly_close` di `modules/siabumdes/application/closing.py`).
- Rekomendasi: **JANGAN** pindahkan siabumdes ke `tenants/` — dia bukan tenant tambahan,
  dia inti/produk utama (routing tanpa prefix, dipakai di hampir semua adapters/shared).

## Hasil audit: file mana yang genuinely "shared" vs cuma dipakai siabumdes

Dicek dengan grep terhadap `tenants/madrasah` dan `tenants/toko`:

### `shared/` — HANYA 3 file yang genuinely dipakai semua tenant
- `shared/config.py` ✅ dipakai madrasah + toko
- `shared/database.py` ✅ dipakai madrasah + toko
- `shared/security.py` ✅ dipakai madrasah + toko

### `shared/` — 5 file yang TERNYATA cuma dipakai siabumdes (harus pindah)
- `shared/coa_taxonomy.py`
- `shared/period.py`
- `shared/report_branding.py`
- `shared/schema.py`
- `shared/seed.py`

### `adapters/api/` — SEMUA cuma dipakai siabumdes (harus pindah)
- `adapters/api/deps.py`, `adapters/api/scope.py`
- 11 router di `adapters/api/v1/*.py` (admin_control, auth, io, master_data, org_profile,
  period_close, public, reports, siabumdes, transaction, uu05_inventory)
- Catatan: madrasah & toko sudah punya `adapters/api/` sendiri masing-masing di foldernya —
  pola ini tinggal diikuti siabumdes juga.

### `adapters/external/` — CAMPURAN, cek satu per satu!
- ❌ TETAP di root (genuinely dipakai bersama): **`gdrive_adapter.py`** — dipakai toko juga
  (ada komentar eksplisit di `tenants/toko/modules/toko/infrastructure/image_upload.py`
  yang bilang "already used by the BUMDes reporting side")
- ✅ Pindah ke siabumdes (cuma dipakai siabumdes): `excel_adapter.py`, `html_pdf.py`,
  `org_logo_upload.py`, `pdf_generator.py`, `report_formatting.py`, `word_generator.py`

## Struktur akhir yang dituju

```
sm85-arch/
├── shared/
│   ├── config.py
│   ├── database.py
│   └── security.py
├── adapters/
│   └── external/
│       └── gdrive_adapter.py
├── modules/
│   └── siabumdes/
│       ├── identity/              (dari modules/identity/)
│       ├── inventory/             (dari modules/uu05_inventory/)
│       ├── application/           (sudah ada)
│       ├── infrastructure/        (sudah ada)
│       ├── adapters/
│       │   ├── api/               (dari adapters/api/deps.py, scope.py, v1/*.py)
│       │   └── external/          (dari adapters/external/*, kecuali gdrive_adapter.py)
│       ├── coa_taxonomy.py
│       ├── period.py
│       ├── report_branding.py
│       ├── schema.py
│       └── seed.py
├── tenants/
│   ├── madrasah/{modules, adapters/api}
│   └── toko/{modules, adapters/api}
├── alembic/
└── main.py
```

## Tahapan eksekusi (1 PR per tahap)

- **Tahap 0** — Cek Procfile/pyproject.toml/CI YAML/Dockerfile untuk path hardcode.
- **Tahap 1** — Pindahkan `modules/identity` → `modules/siabumdes/identity/`.
  28 baris referensi, 8 file pemakai. Verifikasi: login, Tutup Buku, profil organisasi.
- **Tahap 2** — Pindahkan `modules/uu05_inventory` → `modules/siabumdes/inventory/`.
  14 baris referensi, 3 file pemakai. Verifikasi: fitur Inventory.
- **Tahap 3** — Pindahkan 5 file `shared/` yang bukan shared ke `modules/siabumdes/`.
  Verifikasi: laporan keuangan, tutup buku, export PDF/Excel/Word.
- **Tahap 4** — Pindahkan `adapters/api/` (deps, scope, 11 router) ke
  `modules/siabumdes/adapters/api/`. Update `main.py`. **Paling luas dampaknya** —
  jalankan full test suite + cek manual semua grup fitur.
- **Tahap 5** — Pindahkan 6 file `adapters/external/` (KECUALI `gdrive_adapter.py`) ke
  `modules/siabumdes/adapters/external/`. **Hati-hati jangan sampai gdrive_adapter.py
  ikut kepindah** — akan merusak fitur upload gambar toko.
- **Tahap 6** — Hapus folder kosong, jalankan `alembic revision --autogenerate` untuk
  pastikan tidak ada perubahan skema tak sengaja, full test suite + smoke test tiap tenant.

## Yang TIDAK berubah
- URL API (routing independen dari lokasi file, sudah diverifikasi).
- Nama tabel database / skema (cuma pindah lokasi kode Python).
- Frontend tidak perlu disentuh sama sekali.
- Status siabumdes sebagai inti aplikasi (bukan dipindah ke `tenants/`).

## Estimasi
- ~24 file dipindah, ~75+ baris import diupdate.
- Risiko rendah-menengah, murni pemindahan lokasi kode (tidak ada perubahan logika bisnis).
- Titik paling perlu hati-hati: Tahap 4 (blast radius luas) dan Tahap 5 (jangan salah pindah gdrive_adapter.py).
