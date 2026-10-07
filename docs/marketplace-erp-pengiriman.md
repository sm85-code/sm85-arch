# Pengaturan pengiriman Shopee: Drop Off dan Pickup

Halaman Pesanan dan detail pesanan memakai dialog yang sama untuk memilih metode pengiriman. Pilihan berlaku untuk pesanan yang sedang diproses, bukan preferensi global toko.

## API

Semua endpoint berikut berada di `/api/marketplace-erp`. Pengguna harus admin/owner atau staff yang ditugaskan ke akun marketplace pesanan tersebut.

- `GET /pesanan/{id}/opsi-pengiriman`: baca opsi dari `get_shipping_parameter`, tanpa mengatur pengiriman. Respons `opsi` memuat metode, `tersedia`, `alasan`, field `wajib`, `alamat` beserta `jadwal`, dan `cabang`. Metode dengan kebutuhan yang belum didukung ERP dinonaktifkan.
- `POST /pesanan/{id}/proses`: body opsional. Contoh Drop Off: `{"metode":"dropoff"}`. Contoh Pickup: `{"metode":"pickup","address_id":2,"pickup_time_id":"t2"}`. `branch_id` dan `sender_real_name` digunakan untuk Drop Off jika diwajibkan Shopee.
- `POST /pesanan/proses-massal`: `{"pesanan_ids":["a"],"pengaturan":{"a":{"metode":"dropoff"}}}`. Jika pengaturan diberikan, setiap ID harus memiliki pengaturan, tanpa ID tambahan. Maksimum 25 ID per request; FE mengirim 10 per batch.

Request tunggal tanpa body dan request massal lama yang hanya membawa `pesanan_ids` tetap memakai pemilihan otomatis yang mengutamakan Pickup. Field response existing dipertahankan; `metode_pengiriman` ditambahkan secara nullable. Jalur otomatis lama untuk kurir non-integrasi tetap tersedia.

Pilihan eksplisit divalidasi terhadap opsi Shopee terbaru sebelum `ship_order`. Status pesanan terbaru diperiksa terlebih dahulu untuk mencegah mengirim ulang pesanan yang sudah diproses. Alamat, jadwal, dan cabang dari satu toko/pesanan tidak diterapkan ke toko lain. Metode tidak tersedia menghasilkan penolakan; tidak ada fallback ke metode lain.

## Status, resi, dan timeout

Setelah Shopee menerima pengaturan, status marketplace menjadi `PROCESSED`, sedangkan status ERP tetap `to_ship`. `metode_pengiriman` menyimpan metode aktual dan tidak ditimpa sync. Pickup menampilkan menunggu penjemputan; Drop Off menampilkan menunggu penyerahan ke gerai. Pesanan lama yang tidak memiliki metode memakai label netral.

Nomor resi yang belum tersedia atau gagal dibaca tidak membatalkan keberhasilan pengaturan pengiriman. Cetak resi tetap mengikuti kesiapan dokumen Shopee. Proses & cetak hanya mencetak pesanan yang keberhasilannya telah dikonfirmasi.

Timeout saat pengaturan pengiriman dapat berarti Shopee sudah bertindak. ERP tidak melakukan retry otomatis dan meminta sinkronisasi status sebelum percobaan berikutnya. Jika request batch terputus, FE mempertahankan keberhasilan batch sebelumnya, menghentikan batch berikutnya, dan membedakan hasil belum pasti dari pesanan yang belum dikirim untuk diproses. Pemeriksaan status bukan jaminan eksklusivitas dua request bersamaan; pemrosesan simultan juga bergantung pada penolakan Shopee.

## Deployment dan verifikasi

Deploy BE terlebih dahulu. `ensure_marketplace_erp_schema()` menambahkan kolom nullable `mpe_pesanan.metode_pengiriman VARCHAR(16)` secara idempoten pada PostgreSQL existing. Pastikan migrasi startup berhasil, lalu deploy FE. Data lama tidak perlu diisi ulang; metode yang tidak diketahui tetap NULL. Rollback aplikasi dapat membiarkan kolom tambahan tersebut tetap ada.

Tes BE menggunakan mock API Shopee dan DB SQLite untuk validasi pilihan, isolasi izin staff, body API, hasil sebagian, penyimpanan metode setelah sync, dan resi. Tes FE memeriksa pengaturan per alamat, validasi cabang/nama pengirim, pengiriman payload, pembagian batch, timeout, dan label status. Tidak ada pesanan Shopee live yang diproses selama pengujian ini.

Verifikasi sandbox sebelum produksi: satu Pickup dengan jadwal yang tersedia, satu Drop Off tanpa field tambahan, dan Drop Off yang meminta cabang/nama pengirim apabila kurir mendukungnya. Pastikan resi dapat dicetak dan status berubah melalui sync setelah paket diserahkan.
