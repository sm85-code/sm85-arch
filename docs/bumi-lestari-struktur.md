# Tenant `bumi_lestari` — Keuangan UMKM & Order Produk

Status: **usulan struktur** (belum ada kode). Mengikuti pola tenant `marketplace_erp`:
database sendiri, JWT sendiri, router sendiri, tanpa tabel bersama.

Cakupan: (1) **keuangan UMKM secara umum** dan (2) **pengelolaan order produk**
(awalnya partisi ruangan kayu dari Shopee, bisa produk lain nanti).

## 1. Dua modul dalam satu tenant

```
tenants/bumi_lestari/
├── __init__.py
├── adapters/api/v1/bumi_lestari_router.py   # prefix /api/bumi-lestari
└── modules/
    ├── keuangan/        # inti: kas, transaksi, utang-piutang, laporan
    │   ├── application/{schemas,services}.py
    │   └── infrastructure/models.py
    ├── order/           # order produk, tukang, status produksi
    │   ├── application/{schemas,services}.py
    │   └── infrastructure/models.py
    └── infrastructure/  # dipakai bersama kedua modul
        ├── auth.py      # JWT khusus (aud="bumi_lestari")
        ├── database.py  # DATABASE_URL_BUMI_LESTARI, BumiLestariBase
        └── seeder.py    # ensure_bumi_lestari_schema + owner awal
tests/test_bumi_lestari_*.py
```

Perubahan di file yang sudah ada (kecil): `main.py` (include router, lifespan schema,
`_TENANT_DB_MODULES`), `shared/config.py` (`JWT_SECRET_BUMI_LESTARI`,
`JWT_TENANT_BUMI_LESTARI`), `.env.example` (`DATABASE_URL_BUMI_LESTARI`).

**Prinsip:** modul `order` tidak mengubah saldo sendiri. Setiap kejadian uang dari order
(pencairan Shopee, utang tukang, pembayaran tukang) menghasilkan **transaksi** di modul
`keuangan`. Dengan begitu satu laporan keuangan mencakup semua, termasuk pemasukan dan
pengeluaran yang tidak berhubungan dengan order.

## 2. Modul keuangan (umum)

| Tabel | Isi utama |
|---|---|
| `bl_users` | nama, email, password_hash, role (owner/staff), must_change_password |
| `bl_akun_kas` | nama (Kas tunai, BCA, Saldo Shopee…), jenis (kas/bank/ewallet), saldo_awal |
| `bl_kategori` | nama, jenis (pemasukan/pengeluaran), mis. Penjualan, Biaya produksi (tukang), Transport, Packing, Operasional, Prive |
| `bl_transaksi` | tanggal, akun_id, kategori_id, jenis (masuk/keluar), jumlah, keterangan, order_id (opsional), pembayaran_id (opsional) |
| `bl_transfer` | tanggal, dari_akun_id, ke_akun_id, jumlah (mis. tarik saldo Shopee ke bank) |
| `bl_utang` | pihak (tukang/supplier/lain), keterangan, jumlah, jatuh_tempo, status, order_id (opsional) |
| `bl_pembayaran_utang` | tanggal, akun_id, total; `bl_pembayaran_item` memetakan ke banyak utang (batch Selasa) |

Fitur: catat pemasukan/pengeluaran, transfer antar akun, saldo per akun, utang & piutang,
laporan laba-rugi dan arus kas per periode, ekspor CSV.

## 2b. Kas kecil (imprest) dan dua laporan

Kas kecil = akun kas tersendiri untuk **transaksi operasional**, dana awal diambil dari
modal sebesar **Rp 3.000.000** (`plafon`). Setiap minggu dilakukan **pengisian kembali
(replenishment)** sehingga saldo kembali Rp 3.000.000.

Cara kerja:
- `bl_akun_kas` punya kolom `jenis = "kas_kecil"` dan `plafon` (3.000.000, bisa diubah).
- Pengeluaran operasional dicatat sebagai `bl_transaksi` pada akun kas kecil.
- Tiap minggu: pengisian = **plafon − saldo saat ini**, tercatat sebagai `bl_transfer`
  dari akun kas utama/modal ke kas kecil. Jumlahnya otomatis sama dengan total
  pengeluaran kas kecil minggu itu. Endpoint `GET /kas-kecil/pengisian` menghitung
  angkanya, `POST /kas-kecil/pengisian` mencatatnya.
- Pengisian bukan pengeluaran baru: di laporan umum, pengeluaran operasional diakui saat
  dipakai (transaksi kas kecil), sedangkan pengisian hanya pemindahan antar akun.

### Siklus hari Selasa (urutan tetap)

0. **Terima pembayaran penjual lain (reseller)** — mereka membayar tiap Selasa; tercatat sebagai pemasukan "Penjualan reseller" dan melunasi piutang (T3).
1. **Tarik saldo Shopee → kas utama** (`bl_transfer`, akun "Saldo Shopee" → "Kas utama").
2. **Isi kembali kas kecil** dari kas utama sebesar plafon − saldo kas kecil. Hanya bisa
   dilakukan setelah langkah 1 selesai bila saldo kas utama tidak cukup (sistem memperingatkan).
3. **Bayar tukang** (batch) dari kas utama untuk semua order yang sudah diambil.

Halaman "Selasa" di web menampilkan ketiga langkah ini berurutan dengan angkanya.

### Peran
- **admin:** di atas owner. Semua akses owner, plus satu-satunya yang boleh membuat akun admin/owner.
- **owner (Anda):** akses penuh keuangan dan order; hanya boleh membuat akun staf.
- **staf (pemegang kas kecil):** hanya bisa mencatat pengeluaran di akun kas kecil, melihat
  laporan kas kecil, dan mengajukan angka pengisian. Tidak bisa melihat kas utama, order,
  utang, atau laporan umum, dan tidak bisa menghapus/membatalkan transaksi. Pengisian
  kembali dikonfirmasi owner.

Dua laporan:
1. **Laporan kas kecil** (per bulan, dengan rincian mingguan): saldo awal, daftar
   pengeluaran per kategori, total pakai per minggu, pengisian kembali, saldo akhir
   (harus Rp 3.000.000 setelah pengisian). Menandai selisih bila saldo fisik berbeda.
2. **Laporan umum** (laba-rugi, arus kas, saldo semua akun): mencakup semua akun,
   termasuk pengeluaran kas kecil, order, dan utang tukang.

Endpoint tambahan: `GET /laporan/kas-kecil?bulan=YYYY-MM`, `GET /laporan/umum?dari=&sampai=`.

## 3. Modul order produk

| Tabel | Isi utama |
|---|---|
| `bl_produk` | nama, sku, harga_jual_default, biaya_produksi_default, aktif |
| `bl_tukang` | nama, kontak, catatan, aktif |
| `bl_order` | no_order_shopee, tanggal_order, nama_pembeli, status, produk_id, qty, harga_jual, potongan_marketplace, ongkir, tukang_id, biaya_tukang (1 harga borongan: bahan + jasa), tgl_pesan_tukang, tgl_diambil, tgl_selesai, catatan |

### Saluran penjualan (bukan hanya Shopee)

Order bisa datang dari tiga jenis saluran, semuanya masuk ke satu tabel order:

| Jenis saluran | Contoh | Perbedaan perlakuan |
|---|---|---|
| `marketplace` | Shopee, Tokopedia, TikTok Shop | Ada potongan marketplace; uang cair ke saldo marketplace, ditarik tiap Selasa |
| `web` | Toko online web sendiri | Pembayaran dari pembeli langsung (transfer/payment gateway) ke akun bank/e-wallet |
| `reseller` | Penjual online lain yang memesan ke Anda | Harga grosir per reseller; bisa dibayar di muka atau **tempo → piutang** |

Tabel tambahan: `bl_saluran` (nama, jenis, akun kas tujuan pencairan), `bl_pelanggan`
(reseller/pembeli tetap: nama, kontak, harga grosir, tempo hari), dan `bl_harga_grosir`
(produk × pelanggan, opsional). `bl_order` mendapat `saluran_id` dan `pelanggan_id`.
Setiap saluran marketplace punya akun kas sendiri (mis. "Saldo Shopee", "Saldo TikTok").
Order reseller yang belum dibayar menjadi `bl_piutang` (kebalikan utang), dilunasi saat
uangnya masuk ke akun kas.

Produk kayu dari tukang kayu dipakai untuk **dua tujuan**: dijual lewat saluran UMKM sendiri
(marketplace, toko web) dan dipesan **penjual lain (reseller)**. Order reseller melewati alur
produksi yang sama (pesan tukang → diambil → dicat → serah/kirim); bedanya hanya harga
(grosir) dan cara bayar (di muka atau tempo → piutang), dan laba per order dihitung dari
harga grosir itu dikurangi biaya tukang.

**Harga untuk penjual lain = 4 komponen** (tiga per unit di `bl_harga_grosir` per produk × pelanggan, satu flat per order):
1. **barang** (`harga`),
2. **cat dan jasa** (`harga_cat_jasa`; **mengikuti ukuran barang**, jadi diisi per produk; **0 untuk order polos**),
3. **packing**, dua jenis dengan harga berbeda: `biasa` (`harga_packing_biasa`) atau `kayu`
   (`harga_packing_kayu`). Order memilih `jenis_packing`; **packing selalu dibayar, termasuk order polos**,
4. **biaya proses pesanan**: **flat Rp 10.000 per order** (tidak tergantung ukuran atau jumlah). Nilainya
   diatur di **Profil UMKM** (`biaya_proses_order`, bukan hardcode) dan disalin ke order saat dibuat,
   jadi mengubahnya tidak mengubah order lama.

Total order = (barang + cat/jasa + packing) × qty + biaya proses. Order polos (tanpa cat) kadang ada:
`butuh_cat = false` → hanya komponen cat/jasa yang nol dan langkah pengecatan dilewati; packing tetap. Warna cat berbeda-beda
dan kadang custom, tetapi **harga sama**, jadi warna hanya catatan teks (`warna`) di order.
Penjual lain **membayar hari Selasa** (piutang reseller dilunasi di siklus Selasa, bersama
penarikan saldo toko dan pembayaran tukang).

Katalog produk (SKU dan harga yang sudah ada) dipakai bersama semua saluran; harga bisa
dioverride per saluran atau per reseller.

Status: `dipesan → dikerjakan → diambil → dikirim → selesai` (+ `batal`), sama untuk semua saluran.

Efek ke keuangan:
- **diambil** → buat `bl_utang` ke tukang sebesar `biaya_tukang`.
- **selesai** (Shopee cair) → buat transaksi masuk ke akun "Saldo Shopee".
- **bayar Selasa** → satu `bl_pembayaran_utang` melunasi banyak utang tukang
  sekaligus, menghasilkan transaksi keluar kategori "Biaya produksi (tukang)".

## 3b. Gaji karyawan tetap dan bagi hasil

**Gaji karyawan tetap** (biaya tetap bulanan):
- `bl_karyawan`: nama, jabatan, gaji_bulanan, aktif, user_id (opsional, bila karyawan juga staf kas kecil).
- `bl_gaji`: periode (YYYY-MM), karyawan_id, jumlah, tanggal_bayar, akun_id, transaksi_id.
  Membayar gaji membuat satu transaksi keluar kategori **"Gaji karyawan"** dari kas utama.
- Endpoint: `/karyawan` (CRUD, owner/admin), `POST /gaji/generate?periode=` (siapkan daftar
  gaji bulan itu), `POST /gaji/{id}/bayar`.

**Karyawan tetap saat ini (4 orang, semua bergaji bulanan tetap):**

| Peran (`bl_karyawan.peran`) | Tugas | Akun login |
|---|---|---|
| `kas_kecil_packing` | Pemegang kas kecil + packing | Ya (role staf) |
| `order_non_kayu` | Khusus order produk non kayu | Belum (data karyawan saja) |
| `tukang_cat` | Karyawan utama, mengecat produk kayu | Belum |
| `asisten_tukang_cat` | Asisten tukang cat | Belum |

`peran` hanya label (teks bebas bertipe enum longgar), bukan hak akses. Hak akses tetap
admin/owner/staf. Karyawan tanpa login cukup tercatat untuk gaji.

Dampak ke desain order dan katalog:
- Katalog produk punya `jenis_produk`: `kayu` atau `non_kayu` (filter di daftar order).
- Produk non kayu **dibeli dari supplier**: `bl_pemasok` (jenis `tukang_kayu` atau `supplier`) menggantikan tabel tukang; order non kayu: `dipesan → diterima → dikirim → selesai`, `biaya_pokok` = harga beli dari supplier.
- Order kayu punya langkah **pengecatan** setelah barang diambil dari tukang kayu
  (`diambil → dicat → dikirim`); langkah ini dilewati untuk order non kayu.
- Tidak ada biaya jasa cat per order (tukang cat dan asisten sudah bergaji tetap). Biaya cat (bahan cat, thinner, kuas) dicatat sebagai pengeluaran kategori "Bahan cat",
  bukan biaya per order, karena upah tukang cat sudah masuk gaji tetap.

**Bagi hasil admin 40% : owner 60% dari laba bersih:**
- **Tidak di-hardcode.** Proporsi disimpan di `bl_proporsi_bagi_hasil` (penerima `admin`/`owner`,
  persen; total harus tepat 100) dan diubah dari **halaman Profil UMKM**
  (`GET/PUT /profil`, `PUT /profil/proporsi-bagi-hasil`, perubahan proporsi hanya admin).
  Nilai awal Admin 40 / Owner 60 hanya data seed. Penerima bagi hasil hanya dua orang: admin dan owner.
- Setiap perhitungan bagi hasil menyimpan **snapshot** proporsi saat dihitung, jadi mengubah
  proporsi tidak mengubah periode yang sudah dihitung.
- Periode bagi hasil: **bulanan** (`periode` = `YYYY-MM`, satu perhitungan per bulan).
- **Dasar laba = uang yang sudah masuk (basis kas).** Pemasukan hanya dari empat sumber:
  1. saldo toko marketplace (diakui saat dana masuk ke saldo toko, bukan saat ditarik ke
     kas utama, karena penarikan hanya transfer),
  2. toko web sendiri,
  3. pesanan penjual lain (reseller; diakui saat dibayar, bukan saat order bertempo),
  4. pemasukan lain.
  Kategori pemasukan: "Penjualan marketplace", "Penjualan toko web", "Penjualan reseller",
  "Pemasukan lain".
- Laba bersih periode = semua pemasukan − semua pengeluaran periode itu (biaya produksi
  tukang, gaji, operasional kas kecil, transport, packing, dll). **Tidak dihitung sebagai
  biaya:** transfer antar akun, kategori "Prive" dan "Bagi hasil".
- `bl_bagi_hasil`: periode, laba_bersih, bagian_admin, bagian_owner, status
  (`draft` → `dibayar`). Menandai dibayar membuat transaksi keluar kategori "Bagi hasil"
  untuk masing-masing penerima.
- Endpoint: `GET /bagi-hasil/hitung?periode=` (pratinjau), `POST /bagi-hasil` (simpan
  draft), `POST /bagi-hasil/{id}/bayar`. Laba negatif → bagian 0 dan tidak ada pembayaran; kerugian tidak dibawa ke bulan berikutnya.
- Admin dan owner **tidak bergaji**: penghasilan mereka hanya dari bagi hasil (bisa nol bila rugi). Daftar karyawan bergaji hanya untuk karyawan tetap lain.

## 4. Endpoint (`/api/bumi-lestari`)

- Auth: `POST /auth/login`, `/auth/logout`, `GET /auth/me`, `POST /auth/change-password`
- Keuangan: `/akun-kas`, `/kategori`, `/transaksi`, `/transfer`, `/utang`,
  `POST /pembayaran-utang`, `GET /laporan/laba-rugi`, `GET /laporan/arus-kas`,
  `GET /laporan/export.csv`
- Order: `/produk`, `/tukang`, `GET/POST /order`, `PATCH /order/{id}`,
  `POST /order/{id}/status`
- Selasa: `GET /utang/selasa` (daftar utang tukang yang siap dibayar)
- Dashboard: `GET /dashboard?dari=&sampai=`

## 5. Halaman web (frontend repo terpisah, seperti tenant lain)

1. **Dashboard** — saldo semua akun, omzet & laba bulan ini, total utang, "Selasa ini bayar Rp …", order per status.
2. **Transaksi** — catat pemasukan/pengeluaran/transfer, filter akun, kategori, periode.
3. **Order** — tabel + filter status, form tambah, tombol cepat pindah status.
4. **Bayar Tukang** — utang tukang per orang, centang → bayar (batch Selasa).
5. **Utang & Piutang** — semua utang, jatuh tempo.
6. **Laporan** — laba-rugi, arus kas, laba per produk, ekspor.
7. **Master** — akun kas, kategori, produk, tukang.
8. **Profil UMKM** — nama usaha, alamat, kontak, **proporsi bagi hasil** (bisa diubah), ganti password, hari bayar (default Selasa).

## 6. Asumsi yang perlu dikonfirmasi

1. Nama tenant `bumi_lestari` (prefix tabel `bl_`, route `/api/bumi-lestari`).
2. Order dari semua saluran (marketplace, web sendiri, reseller) **diinput manual** dulu; tautan ke sinkron di `marketplace_erp` ditunda.
3. Biaya tukang per order, **satu harga borongan sudah termasuk bahan dan jasa**. Tidak ada pencatatan bahan terpisah per order; laba per order = harga jual bersih − biaya tukang.
4. Kas kecil memakai sistem imprest (plafon Rp 3 juta, diisi kembali tiap minggu). Pencatatan sederhana (kas masuk/keluar + utang), **bukan** akuntansi double-entry penuh. Cukup, atau perlu neraca/jurnal?
5. Satu usaha dan satu pemilik; belum multi-cabang. Staff opsional.
6. Database Postgres baru lewat `DATABASE_URL_BUMI_LESTARI`.

## 7. Tahap pengerjaan

- **T1 (selesai di kode: `tenants/bumi_lestari/`, modul tunggal `modules/bumi_lestari/` — dipecah per modul saat order ditambahkan)** — tenant + auth + akun kas + kategori + transaksi + transfer.
- **T2** — katalog produk (SKU, harga), tukang, saluran, pelanggan/reseller, order + status.
- **T3** — utang, batch bayar Selasa, integrasi order → transaksi.
- **T4** — gaji karyawan tetap, bagi hasil 40/60, dashboard, laporan laba-rugi/arus kas, ekspor.
- **T5** — (opsional) tautan `marketplace_erp`, pengingat Selasa.
