# Vendor dinamis BUMI Lestari

## Penyimpanan dan kompatibilitas

`KeuVendor` di `tenants/bumi_lestari/modules/bumi_lestari/infrastructure/models_keu.py` menyimpan ID yang tetap, kode unik, nama, kontak, alamat, keterangan, jenis dan `aktif`.

API menerima `tipe: kayu/non_kayu`. Untuk mempertahankan data dan referensi lama, kolom `jenis` tetap menyimpan `tukang_kayu/supplier`. Respons menyertakan `tipe` dan `status: aktif/non_aktif` yang dihitung dari kolom tersebut. Input `jenis` lama masih diterima, tetapi mengirim `jenis` dan `tipe` sekaligus ditolak. Tidak ada batas jumlah vendor per jenis.

Kode manual bersifat unik dan maksimal 128 karakter. Jika tidak diberikan atau bernilai null pada pembuatan, ORM membuat kode `VND-<UUID>`. Update parsial tidak menerima null maupun payload kosong.

## API

Prefix: `/api/bumi-lestari/keu`. Semua endpoint memerlukan owner/admin yang telah mengganti password bawaan.

| Metode | Path | Perilaku |
| --- | --- | --- |
| GET | `/vendor` | Pagination `limit` 1–200, `offset`, pencarian nama/kode/kontak/alamat. Termasuk vendor nonaktif agar riwayat tetap terbaca. |
| GET | `/vendor/{id}` | Detail vendor. |
| POST | `/vendor` | Buat vendor; kode opsional. |
| PATCH | `/vendor/{id}` | Edit kode, nama, tipe, kontak, alamat, keterangan atau `aktif`. |
| DELETE | `/vendor/{id}` | Nonaktifkan tanpa menghapus baris ataupun relasi produksi. Idempotent. |

Contoh body pembuatan lengkap:

```json
{
  "kode": null,
  "nama": "Vendor baru",
  "tipe": "kayu",
  "kontak": "081234567890",
  "alamat": "Jl. Produksi 1",
  "keterangan": "Pembuatan furnitur",
  "aktif": true
}
```

Vendor aktif dapat langsung dialokasikan tanpa `keu_vendor_slot`. Jenis vendor harus cocok dengan produk master aktif. Vendor nonaktif tidak menerima alokasi baru, tetapi biaya dan alokasi terdahulu tetap dihitung. Tipe vendor yang memiliki riwayat alokasi, termasuk yang telah dibatalkan, tidak dapat diubah; buat vendor baru untuk tipe berbeda. Deaktivasi dan alokasi menggunakan penguncian baris vendor, dengan pemeriksaan tambahan di trigger PostgreSQL.

Tabel dan endpoint `vendor-slot` dipertahankan untuk kompatibilitas data/klien lama. Tidak ada slot default pada startup baru. Slot kompatibilitas dibuat ketika diminta dengan nomor positif; batas 5/3 telah dihapus. Mengganti tipe vendor tanpa riwayat akan melepaskan tautan slot lama saja. ID vendor tidak berubah.

## Migrasi

File baru: `keu_dynamic_vendors.sql`, dihasilkan oleh:

```bash
python scripts/generate_keu_vendor_migration.py > keu_dynamic_vendors.sql
```

Migrasi otomatis dijalankan melalui `ensure_keu_schema` pada startup BUMI setelah migrasi metadata produk. Migrasi memakai advisory transaction lock yang sama, aman untuk koneksi pooled PostgreSQL, dan hanya berjalan sekali menurut `keu_schema_versions`.

Untuk eksekusi SQL manual, gunakan koneksi khusus database tenant BUMI yang telah menerapkan `keu_initial.sql` dan `keu_product_metadata.sql`, kemudian jalankan `keu_dynamic_vendors.sql`. Jangan menjalankan ulang migrasi produk lama sesudah migrasi vendor: fungsi guard versi lama masih memuat aturan slot. Skrip vendor dapat dijalankan ulang tanpa menggandakan data dan tidak menghapus tabel.

Vendor lama mempertahankan ID, nama, jenis, kontak, status dan seluruh relasi. Kode diisi dari slot lama (`tk-N/sup-N`) bila tersedia, atau `VND-<ID>` untuk vendor tanpa slot. Kolom alamat/keterangan diisi string kosong. DDL dan invariant dalam generator migrasi awal sengaja dibekukan agar artefak migrasi yang sudah dirilis tidak berubah; migrasi vendor mengganti constraint dan guard yang lama.

Validasi dilakukan dengan SQLite untuk API/service, PostgreSQL 16 untuk skrip migrasi dan konkurensi, serta Playwright desktop/mobile untuk UI vendor dan pembacaan lebih dari 200 vendor. Tidak ada perubahan checkout, pembayaran, callback atau webhook iPaymu, dan tidak ada pengiriman status balik ke Store/ERP.
