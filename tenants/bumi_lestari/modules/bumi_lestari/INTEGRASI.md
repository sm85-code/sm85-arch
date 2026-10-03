# Integrasi: Bumi Lestari sebagai rekap keuangan `store` dan `marketplace_erp`

Status: **dirancang, belum dibangun.** Fase 1 hanya menyiapkan kolom dan titik masuk supaya sinkronisasi nanti
bisa idempoten dan tidak menduplikasi logika uang.

## Prinsip

1. **Bumi Lestari tetap sumber kebenaran keuangan.** Tenant lain mengirim fakta (order, pencairan, produk);
   aturan uang (draf/kirim, kategori, kunci, tutup buku) tetap dijalankan oleh service Bumi Lestari.
2. **Idempoten lewat referensi sumber.** Setiap baris hasil sinkronisasi menyimpan `sumber_sistem` + `sumber_ref`
   (unik bersama; NULL untuk entri manual). Job sinkronisasi melakukan *upsert* berdasarkan pasangan ini, jadi
   menjalankan ulang job tidak membuat baris ganda.
3. **Lewat service, bukan tulis tabel langsung.** Job memanggil fungsi yang sama dengan API, dengan pengguna
   sistem (mis. `BlUser(role="admin")` khusus sinkronisasi), sehingga validasi dan log audit tetap berjalan.
4. **Uang dari sinkronisasi masuk sebagai draf.** Admin tetap menekan "Kirim ke laporan keuangan"
   (spesifikasi 8.14), kecuali diputuskan lain.

## Kolom referensi sumber (Fase 1)

| Tabel | Kolom | Contoh nilai |
|---|---|---|
| `bl_order` | `sumber_sistem`, `sumber_ref` (indeks unik `uq_bl_order_sumber`) | `marketplace_erp` + `mpe_item_pesanan:<id>`; `store` + `store_item_pesanan:<id>` |
| `bl_produk` | `sumber_sistem`, `sumber_ref` (`uq_bl_produk_sumber`) | `marketplace_erp` + `mpe_produk:<id>`; pencocokan utama tetap lewat `sku` (= `mpe_produk.sku_induk`) |
| `bl_transaksi` | `sumber_sistem`, `sumber_ref` (`uq_bl_transaksi_sumber`) | `marketplace_erp` + `mpe_settlement:<id>`; `store` + `ipaymu:<gateway_ref>` |
| (Fase 2) `bl_pencairan_baris` | sama | satu baris per order per pencairan (`<platform>:<order_sn>:<payout_id>`) |

Catatan: `bl_order` satu baris = satu barang. Pesanan marketplace berisi beberapa barang menjadi beberapa baris
dengan `no_order` sama; karena itu `sumber_ref` order memakai id **item** pesanan, bukan id pesanan.

## Titik integrasi

### marketplace_erp → Bumi Lestari

| Data asal | Model asal | Masuk ke | Fungsi Bumi Lestari |
|---|---|---|---|
| Pesanan Shopee/TikTok (per item) | `mpe_pesanan` (`platform`, `id_eksternal`, `status`, `tanggal_kirim`) + `mpe_item_pesanan` | `bl_order` (saluran jenis `marketplace`, `no_order` = `id_eksternal`) | `order_services.create_order` / `update_order` / `ubah_status_order` (status kirim → `dikirim`, `tgl_dikirim`) |
| Produk | `mpe_produk` (`sku_induk`) | `bl_produk` | `order_services.create_produk` / `update_produk` |
| Pencairan / settlement | `mpe_settlement` (`gross_sales`, `fee_platform`, `fee_payment`, `ongkir_subsidi`, `penalti`, `net`) | Fase 2: `bl_pencairan` + baris; pemasukan "Penjualan marketplace" + "Biaya marketplace" per jenis potongan ke akun `SALDO_<PLATFORM>` | Fase 2: mesin impor penghasilan (format per marketplace) — sinkronisasi memakai mesin yang sama dengan unggah Excel |
| Iklan | `mpe_iklan_campaign`, `mpe_iklan_metrik_harian` | Fase 2: laporan iklan (bukan uang; uang iklan tetap dari Kas iklan) | — |

### store (toko web) → Bumi Lestari

| Data asal | Model asal | Masuk ke | Fungsi |
|---|---|---|---|
| Pesanan pembeli | `store_pesanan` (`status`, `total`, `gateway_ref`) + `store_item_pesanan` | `bl_order` (saluran jenis `web`) | `order_services.create_order` |
| Pembayaran iPaymu yang cair | `store_pesanan.gateway_ref` | Fase 2: pencairan iPaymu ke `SALDO_IPAYMU`, kategori "Penjualan toko web" | mesin impor yang sama (format iPaymu) |
| Produk | `store_produk` | `bl_produk` (cocokkan lewat SKU) | `order_services.create_produk` |

## Service yang bisa dipanggil job sinkronisasi

Semua menerima `(session, user, ...)` dan tidak bergantung pada HTTP:

- `order_services.create_order`, `update_order`, `ubah_status_order`, `status_bayar_order`
- `services.create_transaksi` (aturan kategori berlaku; kategori sistem hanya lewat service otomatis)
- `pembayaran_services.buat_pembayaran_pemasok`, `buat_penerimaan_reseller`, `tagihan_order`,
  `order_reseller_belum_dibayar`
- `kiriman_services.ringkasan_draf`, `kirim`, `kirim_semua`, `batal_kiriman`
- `audit_core.catat_audit`, `audit_core.bulan_tertutup` (tutup buku Fase 2)

## Yang perlu diputuskan sebelum membangun sinkronisasi

- Pemetaan status `mpe_pesanan.status` / `store_pesanan.status` → alur status `bl_order`.
- Saluran dan pemasok bawaan untuk order hasil sinkronisasi (tukang belum diketahui saat order masuk).
- Pencairan: sinkron dari API platform atau tetap unggah file (spesifikasi 8.3).
- Jadwal job (mis. tiap jam) dan pengguna sistem yang tercatat di log audit.

## Bulan tutup buku (Fase 2.3)

- Setelah bulan ditutup (`bl_tutup_buku.status = 'ditutup'`), setiap penulisan `bl_transaksi`/`bl_transfer` bertanggal di
  bulan itu ditolak 409 — juga dari job sinkronisasi (pengaman di tingkat mapper, `audit_core.py`).
- Data sinkron yang terlambat untuk bulan tertutup dicatat **di bulan berjalan** dengan `koreksi_periode = 'YYYY-MM'`
  (bulan asal); laporan bulan tertutup tetap memakai snapshot.
- Bagi hasil hanya dari bulan tertutup, memakai `snapshot.laba_rugi.laba_bersih`.

## Akun saldo saluran, status cair & retur (Fase 2.6/2.7)

- Saluran bawaan dan akunnya: Shopee → `SALDO_SHOPEE`, TikTok Shop → `SALDO_TIKTOK`, Lazada → `SALDO_LAZADA`,
  Blibli → `SALDO_BLIBLI`, Toko web → `SALDO_IPAYMU` (`seeder.DEFAULT_SALURAN`). Sinkronisasi memetakan platform
  `marketplace_erp` (`shopee`, `tiktok`, …) ke saluran lewat nama/akun ini.
- `bl_order.status_cair` (`belum`/`cair`), `tgl_cair`, `pencairan_baris_id`, `potongan_aktual` **hanya diisi oleh
  pencairan** (unggah file, entri iPaymu, atau sinkronisasi settlement) — tidak ada endpoint untuk mengubahnya manual.
- Retur sebelum cair: `order_services.retur_order` (status `retur`, `tgl_retur`, `alasan_retur`, `kembali_stok`).
  Retur setelah cair datang dari baris `retur`/`penyesuaian` pencairan. Biaya tukang order retur dipindah dari HPP ke
  "Kerugian retur" di laporan (`laba_core.reklas_retur`, bulan = yang lebih akhir antara tanggal retur dan tanggal
  pembayaran tukang), jadi bulan yang sudah tutup buku tidak berubah.
- Belum cair dihitung ulang per tanggal dari order (`laporan_services.belum_cair(session, per_tanggal)`): sudah dikirim,
  belum cair per tanggal itu, belum retur per tanggal itu. Order hasil sinkronisasi ikut otomatis bila `tgl_dikirim` terisi.

## Pencairan & format file penghasilan (Fase 2.4/2.5/2.8)

- Mesin format murni (`application/pencairan_format.py`, tanpa DB): file → `BarisStandar`. Adapter kode cadangan
  didaftarkan di `application/pencairan_adapter.ADAPTER[nama_saluran]` dengan keluaran yang sama.
- **Titik masuk sinkronisasi settlement `marketplace_erp`:** bangun `BarisStandar` dari `mpe_settlement`
  (`gross_sales` → `harga_jual`, `fee_*`/`ongkir_subsidi`/`penalti` → `rincian_biaya`, `net` → `jumlah_cair`), lalu
  `pencairan_services.cocokkan(...)` + `simpan_baris(..., sumber_sistem="marketplace_erp", sumber_ref=<settlement id>)`.
  `kunci_unik` (saluran|kode|jenis|tanggal|jumlah) mencegah dobel dengan unggahan Excel untuk pesanan yang sama;
  `bl_pencairan_unggahan (sumber_sistem, sumber_ref)` unik untuk idempotensi per batch.
- Pembukuan terjadi saat "Kirim ke laporan keuangan" (sumber kiriman `pencairan`): transaksi resmi + order cair
  (`setelah_kirim`); batal kiriman memulihkan (`setelah_batal_kirim`).
- Format Shopee bawaan adalah **SEMENTARA** (draf v1, belum diuji dengan file asli).

## Kolom tambahan (Fase 2.14/2.15)

`bl_order`, `bl_produk`, `bl_pemasok`, `bl_pelanggan`, `bl_transaksi`, `bl_karyawan` punya `kolom_tambahan` (JSONB,
kunci = `bl_definisi_kolom.kunci`). Sinkronisasi boleh mengirim objek `kolom_tambahan` di payload yang sama; nilainya
divalidasi dengan aturan definisi (jenis data, pilihan, wajib) dan **tidak pernah** dibaca rumus keuangan. Kunci yang
tidak dikenal/nonaktif ditolak (422), jadi definisi perlu dibuat dulu di Data master.
