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
| `bl_kategori` | nama, jenis (pemasukan/pengeluaran), mis. Penjualan, Bahan, Upah tukang, Transport, Packing, Operasional, Prive |
| `bl_transaksi` | tanggal, akun_id, kategori_id, jenis (masuk/keluar), jumlah, keterangan, order_id (opsional), pembayaran_id (opsional) |
| `bl_transfer` | tanggal, dari_akun_id, ke_akun_id, jumlah (mis. tarik saldo Shopee ke bank) |
| `bl_utang` | pihak (tukang/supplier/lain), keterangan, jumlah, jatuh_tempo, status, order_id (opsional) |
| `bl_pembayaran_utang` | tanggal, akun_id, total; `bl_pembayaran_item` memetakan ke banyak utang (batch Selasa) |

Fitur: catat pemasukan/pengeluaran, transfer antar akun, saldo per akun, utang & piutang,
laporan laba-rugi dan arus kas per periode, ekspor CSV.

## 3. Modul order produk

| Tabel | Isi utama |
|---|---|
| `bl_produk` | nama, sku, harga_jual_default, biaya_produksi_default, aktif |
| `bl_tukang` | nama, kontak, catatan, aktif |
| `bl_order` | no_order_shopee, tanggal_order, nama_pembeli, status, produk_id, qty, harga_jual, potongan_marketplace, ongkir, tukang_id, biaya_tukang, tgl_pesan_tukang, tgl_diambil, tgl_selesai, catatan |

Status: `dipesan → dikerjakan → diambil → dikirim → selesai` (+ `batal`).

Efek ke keuangan:
- **diambil** → buat `bl_utang` ke tukang sebesar `biaya_tukang`.
- **selesai** (Shopee cair) → buat transaksi masuk ke akun "Saldo Shopee".
- **bayar Selasa** → satu `bl_pembayaran_utang` melunasi banyak utang tukang
  sekaligus, menghasilkan transaksi keluar kategori "Upah tukang".

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
8. **Pengaturan** — ganti password, hari bayar (default Selasa).

## 6. Asumsi yang perlu dikonfirmasi

1. Nama tenant `bumi_lestari` (prefix tabel `bl_`, route `/api/bumi-lestari`).
2. Order Shopee **diinput manual** dulu; tautan ke sinkron di `marketplace_erp` ditunda.
3. Biaya tukang per order (borongan).
4. Pencatatan sederhana (kas masuk/keluar + utang), **bukan** akuntansi double-entry penuh. Cukup, atau perlu neraca/jurnal?
5. Satu usaha dan satu pemilik; belum multi-cabang. Staff opsional.
6. Database Postgres baru lewat `DATABASE_URL_BUMI_LESTARI`.

## 7. Tahap pengerjaan

- **T1** — tenant + auth + akun kas + kategori + transaksi + transfer.
- **T2** — master produk/tukang + order + status.
- **T3** — utang, batch bayar Selasa, integrasi order → transaksi.
- **T4** — dashboard, laporan laba-rugi/arus kas, ekspor.
- **T5** — (opsional) tautan `marketplace_erp`, pengingat Selasa.
