# Tenant `kayu` — Dashboard Keuangan & Order Produk Kayu (UMKM)

Status: **usulan struktur** (belum ada kode). Mengikuti pola tenant `marketplace_erp`:
database sendiri, JWT sendiri, router sendiri, tanpa tabel bersama.

## 1. Alur bisnis

```
Order Shopee (partisi kayu)
  → dicatat di app (manual dulu)            status: dipesan
  → pesan ke tukang kayu (harga borongan)   status: dikerjakan
  → 1–2 hari jadi, diambil                  status: diambil   ← utang ke tukang tercatat
  → dikirim / diserahkan ke pembeli         status: dikirim
  → Shopee cair (settlement)                status: selesai   ← pemasukan tercatat
  → tiap Selasa: bayar tukang                                 ← utang dilunasi (batch)
```

Aturan inti: **utang ke tukang muncul saat barang diambil**, dan **dilunasi per batch
setiap Selasa** (satu pembayaran untuk banyak order). Laba per order =
harga jual bersih (setelah potongan Shopee) − biaya tukang − biaya lain.

## 2. Struktur folder (di repo ini)

```
tenants/kayu/
├── __init__.py
├── adapters/api/v1/kayu_router.py          # semua endpoint, prefix /api/kayu
└── modules/kayu/
    ├── application/
    │   ├── schemas.py                      # Pydantic in/out
    │   └── services.py                     # logika: order, utang, batch bayar, laporan
    └── infrastructure/
        ├── auth.py                         # JWT khusus kayu (aud="kayu")
        ├── database.py                     # DATABASE_URL_KAYU, KayuBase
        ├── models.py
        └── seeder.py                       # ensure_kayu_schema + owner awal
tests/test_kayu_*.py
```

Perubahan di file yang sudah ada (kecil): `main.py` (include router, lifespan schema,
`_TENANT_DB_MODULES`), `shared/config.py` (`JWT_SECRET_KAYU`, `JWT_TENANT_KAYU`),
`.env.example` (`DATABASE_URL_KAYU`).

## 3. Model data

| Tabel | Isi utama |
|---|---|
| `kayu_users` | nama, email, password_hash, role (owner/staff), must_change_password |
| `kayu_produk` | nama (mis. "Partisi ruangan kayu jati 2x1m"), sku, harga_jual_default, biaya_tukang_default, aktif |
| `kayu_tukang` | nama, kontak, catatan, aktif |
| `kayu_order` | no_order_shopee, tanggal_order, nama_pembeli, status, produk_id, qty, harga_jual, potongan_marketplace, ongkir, tukang_id, biaya_tukang, tgl_pesan_tukang, tgl_diambil, tgl_selesai, catatan |
| `kayu_pembayaran_tukang` | tukang_id, tanggal_bayar (Selasa), total, metode, bukti/catatan |
| `kayu_pembayaran_item` | pembayaran_id, order_id, jumlah (satu order bisa dibayar sekali) |
| `kayu_pemasukan` | order_id, tanggal, jumlah (pencairan Shopee), sumber |
| `kayu_biaya_lain` | tanggal, kategori (bahan, transport, packing, lain), jumlah, order_id (opsional) |

Status order: `dipesan → dikerjakan → diambil → dikirim → selesai` (+ `batal`).
Status utang tukang diturunkan, bukan disimpan: order `diambil`+ yang belum punya
`kayu_pembayaran_item` = **belum dibayar**.

Pakai string enum biasa (bukan Postgres ENUM), sama seperti tenant lain.

## 4. Endpoint (`/api/kayu`)

- Auth: `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `POST /auth/change-password`
- Master: `/produk`, `/tukang` (CRUD)
- Order: `GET/POST /order`, `PATCH /order/{id}`, `POST /order/{id}/status`
  (tombol cepat: pesan ke tukang, tandai diambil, tandai dikirim, tandai selesai)
- Utang & bayar: `GET /utang-tukang` (per tukang, order belum dibayar),
  `GET /utang-tukang/selasa` (daftar untuk batch Selasa terdekat),
  `POST /pembayaran-tukang` (pilih order → catat satu pembayaran)
- Uang masuk/keluar: `/pemasukan`, `/biaya-lain`
- Dashboard: `GET /dashboard?dari=&sampai=` → omzet, laba kotor, utang berjalan,
  jadwal bayar Selasa, order per status, laba per produk
- Ekspor: `GET /laporan/export.csv`

## 5. Halaman web (frontend, repo terpisah seperti tenant lain)

1. **Dashboard** — kartu: omzet bulan ini, laba, total utang ke tukang, "Selasa ini bayar Rp …"; grafik omzet vs biaya; order per status.
2. **Order** — tabel + filter status; form tambah order; tombol cepat pindah status.
3. **Bayar Tukang** — daftar order `diambil` yang belum dibayar, dikelompokkan per tukang, centang → "Bayar", cetak/salin rekap.
4. **Keuangan** — pemasukan, biaya lain, laba per order/produk, filter periode.
5. **Master** — produk, tukang.
6. **Pengaturan** — ganti password, hari bayar (default Selasa).

## 6. Asumsi yang perlu dikonfirmasi

1. Nama tenant `kayu` — setuju, atau mau nama lain?
2. Order Shopee **diinput manual** dulu. Tenant `marketplace_erp` sudah punya sinkron
   pesanan Shopee; bisa ditautkan nanti (fase 2), tapi akan membuat dua tenant saling
   bergantung — saya sarankan tidak di awal.
3. Biaya tukang = **per order** (borongan), bukan per jam/per meter.
4. Satu pengguna (Anda), peran staff opsional.
5. Backend di repo ini; frontend repo terpisah (seperti tenant lain) dan perlu
   ditambahkan ke `CORS_ORIGINS`.
6. Database: Postgres baru (Neon/DO) lewat `DATABASE_URL_KAYU`.

## 7. Tahap pengerjaan

- **T1** — tenant + auth + master + order + status (tanpa uang).
- **T2** — utang tukang + batch bayar Selasa.
- **T3** — pemasukan Shopee, biaya lain, dashboard & laporan.
- **T4** — (opsional) tautan ke `marketplace_erp`, notifikasi pengingat Selasa.
