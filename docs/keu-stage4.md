# BUMI Lestari: keu tahap 4

## Berkas dan isolasi

Model ada di `tenants/bumi_lestari/modules/bumi_lestari/infrastructure/models_keu.py`, input tervalidasi di `application/schemas_keu.py`, dan API di `tenants/bumi_lestari/adapters/api/v1/keu_router.py`.

Kelima belas tabel memakai `BumiLestariBase` dan session dari `DATABASE_URL_BUMI_LESTARI`. Identitas, kategori, audit dan kunci periode menggunakan `bl_users`, `bl_kategori`, `bl_audit_log`, dan `bl_tutup_buku` yang sudah ada pada database tenant yang sama. Tabel/data `bl_*` dan `bl2_*` tidak dikonversi, dihapus, atau dimasukkan ke saldo `keu` secara otomatis.

Slot awal DB adalah `tk-1` sampai `tk-5` dan `sup-1` sampai `sup-3`, semuanya tanpa vendor. Nama/kontak diisi melalui Pengaturan; penyimpanan ulang slot mempertahankan identitas vendor. Segmentasi pelanggan opsional: `umkm`, `reseller`, atau belum diklasifikasi.

## Migrasi

Generator deterministik:

```sh
python scripts/generate_keu_migration.py > keu_initial.sql
```

`seeder._create_schema()` menjalankan migrasi additive pada startup backend. Versi dicatat di `keu_schema_versions`; startup berulang aman. Tabel dasar BUMI dibuat/disesuaikan lebih dulu oleh seeder. Semua perubahan startup berada dalam satu transaksi dengan advisory transaction lock.

Alternatif sebelum deploy: jalankan `keu_initial.sql` melalui client PostgreSQL pada database BUMI yang sudah terinisialisasi. Skrip SQL secara sengaja menolak database tanpa `bl_users`/`bl_kategori`, versi yang sudah diterapkan, atau tabel keu yang sudah dibuat sebagian. Jangan jalankan pada database Store/ERP. Gunakan endpoint Neon direct untuk eksekusi DDL administratif; request aplikasi tetap memakai URL tenant yang dikonfigurasi, termasuk URL pooled bila tersedia. Skrip tidak membutuhkan session-level advisory lock.

Tidak ada DDL di request keu. Engine/pool existing tidak ditambah; penarikan sumber meminjam satu session BUMI dan satu session Store atau ERP, maksimal 100 sumber per penarikan. Tidak ada koneksi ke provider payment/Shopee pada penarikan ini. Kunci versi schema harus dinaikkan melalui migrasi baru untuk perubahan skema berikutnya.

## Kontrak API dan pencatatan

Prefix: `/api/bumi-lestari/keu`. Semua route membutuhkan owner/admin dan `must_change_password=false`. Role staff tetap memakai kas kecil lama. Actor diperoleh dari session autentikasi, tidak diterima dari body. Nominal API dikirim sebagai string desimal, database `NUMERIC(20,2)`.

- Master: GET/POST `/saluran`, `/akun`, `/pelanggan`, `/vendor`, `/produk`; GET `/kategori` dan `/sumber`.
- Vendor slot: GET `/vendor-slot`, PUT `/vendor-slot/{kode}`.
- Pesanan: GET/POST `/pesanan`, GET `/pesanan/{id}`, PATCH `/item/{id}/produk`, POST `/pesanan/{id}/status`.
- Produksi: GET/POST `/alokasi-vendor`, POST `/alokasi-vendor/{id}/batal`.
- Settlement: GET/POST `/settlement`, GET/POST `/alokasi-settlement`, POST `/settlement/{id}/posting`.
- Kas: GET/POST `/transaksi`, POST `/transaksi/{id}/posting`.
- Import: GET `/impor/template?jenis=order|biaya|settlement`, POST multipart `/impor/pratinjau`, GET `/impor/{id}`, POST `/impor/{id}/terapkan`.
- Sumber: POST `/saluran/{id}/tarik?entitas=order|settlement`; GET `/masukan`, POST `/masukan/{id}/ulang`.

List menggunakan limit (1–200), offset, dan search; hasil `{rows,total,limit,offset}`. Biaya operasional/import dibuat draf, belum menambah/mengurangi saldo sampai posting. Nilai pesanan terpisah dari kas; biaya alokasi vendor adalah estimasi komitmen, bukan transaksi kas otomatis. Settlement hanya menghasilkan satu transaksi sebesar neto setelah alokasi lengkap; posting ulang tidak menggandakan transaksi. Periode tutup buku ditolak saat posting. Catatan posted dikunci oleh trigger PostgreSQL; koreksi memerlukan catatan terpisah, bukan overwrite.

## Import dan sumber

CSV UTF-8 (koma/titik koma, quoted fields didukung) dan XLSX dibatasi 5 MB, 10.000 baris; formula dan Excel dengan isi terkompresi berlebihan ditolak. Gunakan template keu, bukan format mentah provider. Pratinjau melakukan validasi schema; referensi DB diperiksa saat terapkan. Jika satu baris gagal, seluruh batch rollback. File yang sama pada saluran/jenis/versi sama dideduplikasi. Data import order multi-item dikelompokkan berdasarkan sumber_ref.

Saluran `manual` untuk input pesanan/kas manual. Saluran `store` memakai akun_ref literal `store`. Saluran `marketplace_erp` memakai ID `AkunMarketplace` asli dari `/sumber`, bukan nama toko. Saluran aktif diatur eksplisit saat dibuat.

Penarikan berupa pull inkremental yang dipicu pengguna, belum scheduler otomatis. Store/ERP dibaca melalui model/session existing dan PostgreSQL READ ONLY; token provider tidak disalin. Item sumber tidak ditebak produk/vendornya: petakan di Order. Cursor diperbarui setelah event tersimpan pada inbox; event gagal bisa dicoba ulang. Revisi pesanan yang lebih lama tidak menimpa snapshot terbaru. Perubahan kuantitas/harga pada order yang telah dialokasikan memerlukan rekonsiliasi manual.

Settlement ERP memakai `SettlementPesanan.diambil_at` sebagai cursor dan `dirilis_at` sebagai tanggal cair. Bruto=`penjualan`, neto=`jumlah_cair`; potongan merupakan selisih keduanya, dengan `rincian` sumber dipertahankan untuk tinjauan. Ini bukan klaim bahwa selisih adalah rincian fee provider. Data tanpa tanggal cair masuk antrian gagal. Settlement Store tidak ditarik karena model Store yang digunakan tidak menyediakan bukti pencairan; jangan menganggap status pembayaran sebagai kas yang sudah cair.

## Deployment FE

Frontend React/Vite: `keu.ampelkuning.com`. Build static `dist`, `VITE_API_URL` diatur saat build ke origin backend. Tambahkan `https://keu.ampelkuning.com` ke nilai environment `CORS_ORIGINS` BE yang sudah ada, sambil mempertahankan origin aplikasi lain. Session tetap menggunakan cookie BUMI existing dan `credentials: include`; source code middleware/cookie/payment iPaymu tidak berubah.

Deploy BE terlebih dahulu dan pastikan migration selesai sebelum FE. CI memvalidasi SQL pada PostgreSQL 16 disposable, bukan database Neon staging. Keberhasilan deployment dan isi database live harus diperiksa terpisah dari status PR/CI.

## Verifikasi

```sh
ruff check .
python -m pytest tests/ -n 0
TEST_DATABASE_URL=postgresql://user:password@localhost/test_db python -m pytest tests/test_keu_pg.py -n 0
```

Tes mencakup auth/password guard, batas nominal, alokasi dan jenis vendor, idempotensi, rollback batch, posting neto, periode tertutup, event gagal/revisi stale, SQL hasil generator, trigger dan concurrency PostgreSQL.
