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

## 8. Aturan yang sudah dikodekan (T3/T4)

**Pembayaran tukang kayu dan supplier (tiap Selasa)**
- Halaman "pesanan ke tukang" memakai `GET /pembayaran-pemasok/siap`: order yang sudah **selesai dikerjakan dan
  diambil karyawan Senin–Sabtu minggu sebelumnya** dan belum dibayar. Diambil Minggu atau Senin–Selasa ini
  → Selasa berikutnya. Order yang terlewat tetap muncul, ditandai `terlambat`.
- Tombol **"Kirim ke laporan"** (`POST /pembayaran-pemasok`): dicatat **1 kali tiap Selasa**. Di laporan
  keuangan muncul sebagai **1 transaksi bertotal** (kategori "Biaya produksi / pembelian barang"); rincian per
  order/barang (jumlah, pemasok) ada di `GET /pembayaran-pemasok/{id}`. Dibatalkan = transaksi ikut batal.
- Tukang kayu dan supplier dibayar bersamaan di Selasa yang sama; dibayar dari kas utama (saldo harus cukup).

**Penjual lain (reseller):** order dikirim/selesai = piutang (`GET /piutang-reseller`); pembayaran dicatat dengan
`POST /penerimaan-reseller` (pemasukan "Penjualan reseller"). Kolom "tempo hari" dihapus (mereka bayar tiap Selasa).

**Marketplace:** pemasukan dicatat sendiri saat dana cair (transaksi masuk ke akun Saldo Shopee), penarikan ke
kas utama dicatat sendiri sebagai transfer. Tidak ada pencatatan otomatis dari status order.

**Gaji:** `POST /gaji/siapkan` membuat gaji bulan itu untuk karyawan aktif; `POST /gaji/bayar` membayar semuanya
(tanggal 1 bulan berikutnya = `jatuh_tempo`), satu transaksi "Gaji karyawan" per karyawan, dari kas utama.

**Bagi hasil:** `GET /bagi-hasil/hitung?periode=YYYY-MM` (pratinjau), `POST /bagi-hasil` (simpan, snapshot proporsi),
`POST /bagi-hasil/{id}/bayar` (ditarik tunai dari kas utama, tidak ditahan). Laba ≤ 0 → bagian 0, tidak ada pembayaran.

**Kewenangan admin (selain owner):** mengubah profil UMKM (termasuk biaya proses dan proporsi bagi hasil),
mengubah pengguna (`PATCH /users/{id}`: nama, email, role, aktif), dan mengatur ulang password pengguna lain
(`POST /users/{id}/reset-password`, pengguna wajib menggantinya saat login). Owner hanya boleh melihat profil.

**Bagan akun (COA):** tidak perlu disiapkan. Akun kas + kategori yang di-seed berfungsi sebagai bagan akun
sederhana; kategori baru ditambah dari halaman Master.

## 9. Dokumen cetak: Purchase Order dan Invoice (cocok dengan contoh PDF)

Backend menyusun **data** (JSON); tampilan/PDF (logo, tata letak) dibuat frontend. Logo ada di
`docs/assets/bumi-lestari-logo.png`.

- **Purchase Order ke tukang/supplier** (`GET /po/siap?tanggal=`, `GET /po/pembayaran/{id}`): satu PO per pemasok.
  Nomor `PO/MG.{minggu}-{kode}/{bulan romawi}/{tahun}` (mis. `PO/MG.4-005/IX/2026`, kode 005 = kode tukang di
  `bl_pemasok.kode`, otomatis dan bisa diubah). Isi: periode Senin–Sabtu, Tgl. Pembayaran = Selasa, kepada
  (nama, bank, no. rekening), tabel Tanggal Selesai (+hari) / Kode Pesanan / Nama Barang / Ukuran / Qty / Harga
  Barang / Total, serta grand total.
- **Invoice ke penjual lain** (`GET /invoice-reseller?tanggal=&pelanggan_id=`): satu invoice per pelanggan.
  Nomor `INV/MG.{minggu}-{kode}/{bulan romawi}/{tahun}` (`bl_pelanggan.kode`). Tgl. invoice = **Sabtu minggu lalu**
  (akhir periode), jatuh tempo = **Selasa minggu ini**; dikirim ke penjual lain untuk pesanan Senin–Sabtu minggu lalu. Kolom: Tanggal (**tanggal barang jadi dan diambil dari tukang**, sama dengan "Tanggal Selesai" di PO) / Nama Barang / Ukuran / Harga Barang / Biaya Jasa
  Pengecatan (cat + jasa + packing) / Biaya Proses Pesanan / Total. Syarat pembayaran dan tujuan transfer
  (`info_pembayaran` di profil, mis. "QRIS Pangeran Homeware").
- **Minggu ke-N:** Senin–Sabtu; minggu pertama adalah minggu yang memuat tanggal 1, bulan mengikuti hari Sabtu
  (21–26 September 2026 = Minggu ke-4 September; 28 Sep–3 Okt = Minggu ke-1 Oktober).
- **Nama badan usaha:** CV sampai 3 Okt 2026, **PT mulai 4 Okt 2026**. Disimpan di profil UMKM (`nama_usaha`,
  `nama_usaha_lama`, `nama_usaha_berlaku_mulai`); dokumen bertanggal sebelum tanggal peralihan tetap mencetak
  nama lama. Nilai awal di atas bisa diubah admin.
- Produk punya `ukuran` (mis. 100x20x200); varian ditulis di nama produk (mis. "Partisi Rak Tengah [2 rak]").

## 10. Kas iklan (imprest, seperti kas kecil)

Biaya iklan diambil dari modal dengan jatah **Rp 2.000.000 per bulan** dan **digenapkan lagi tiap minggu**:
akun `KAS_IKLAN` (jenis `kas_iklan`, plafon 2.000.000), kategori pengeluaran "Biaya iklan". Cara kerjanya sama
dengan kas kecil: pengeluaran dicatat di akun ini (tidak boleh minus), lalu tiap Selasa saldo dikembalikan ke
plafon dengan transfer dari kas utama (`GET/POST /kas-iklan/pengisian`). Hanya admin/owner yang mengakses
kas iklan; staf kas kecil tidak. Siklus Selasa: terima bayar reseller → tarik saldo toko → isi kas kecil →
**isi kas iklan** → bayar tukang/supplier.

## 11. Gaji dan langganan dicicil 4 minggu (Dana cadangan)

Gaji dibayar bulanan (tanggal 1 bulan berikutnya), tetapi **di laporan keuangan dicicil 4 minggu** supaya uangnya
tersedia di akhir bulan dan bisa dibayar di awal bulan. Hal yang sama berlaku untuk **langganan bulanan**: listrik,
air, wifi, kebersihan, iuran BUMDES, langganan Komplace (daftar bisa ditambah; nominal diisi di halaman Langganan,
nilai awal 0 = belum disisihkan).

- Akun **Dana cadangan** (`DANA_CADANGAN`) menampung uang yang disisihkan.
- **Tiap Selasa (Selasa ke-1 sampai ke-4 bulan itu)** tombol *Sisihkan*: `GET /sisihan/hitung` (pratinjau) lalu
  `POST /sisihan`. Sistem mentransfer 1/4 gaji + 1/4 langganan dari kas utama ke Dana cadangan **dan** mencatat
  1/4 itu sebagai beban minggu itu di laporan. Cicilan ke-4 menampung sisa pembulatan. Selasa ke-5 tidak ada cicilan.
- **Awal bulan berikutnya**: `POST /gaji/bayar` dan `POST /tagihan/bayar` membayar dari Dana cadangan. Pembayaran
  hanya menyesuaikan selisih (mis. listrik lebih mahal dari perkiraan, diakui sebagai beban saat dibayar) dan
  **tidak dihitung dua kali** sebagai biaya. Kalau Dana cadangan kurang, transfer dulu dari kas utama.
- Siklus Selasa: terima bayar reseller → tarik saldo toko → isi kas kecil → isi kas iklan →
  **sisihkan dana gaji & langganan** → bayar tukang/supplier.
- Semua bisa dibatalkan (transfer, beban, dan transaksi ikut dibatalkan).

**Catatan proses penjual lain:** seluruh pengerjaan dilakukan UMKM (ambil dari tukang → cat oleh karyawan → tempel
resi → kirim); penjual lain hanya mengirim resi ke UMKM (resi menjadi tanggung jawab penjual lain, tidak dicatat
di sistem). Karena itu order sudah
**bisa ditagih sejak barang jadi dan diambil dari tukang** (`tgl_diambil`), tidak menunggu dicat atau dikirim.
Satu order = satu baris (di PO dan invoice, jumlah order = jumlah baris).
