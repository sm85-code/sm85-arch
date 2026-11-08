# Pemisahan Pesanan dan Pesanan EasyBoss

- `/pesanan`: Shopee Open API (`Pesanan.platform == shopee`, tanpa receipt EasyBoss).
- `/pesanan-easyboss`: pesanan yang receipt BridgeOrder-nya berasal dari Lazada atau TikTok Shop.
- Frontend menggunakan PesananPage dan kolomPesanan yang sama untuk desktop, kartu HP dan tabel HP. Parameter sumber memisahkan URL API dan cache; pergantian menu mereset instance halaman.
- Endpoint daftar, ringkasan status, dan pilihan toko menggunakan batas sumber yang sama, kemudian cakupan penugasan toko. Query `sumber` tidak dapat mengganti batas URL endpoint.
- Izin baru `pesanan_easyboss` muncul dalam registry Role dan navigasi/Kelola Aplikasi. Migrasi satu kali menyalin tingkat izin Pesanan sebelumnya; pencabutan berikutnya tidak ditimpa.
- Endpoint detail dan aksi tetap memakai ID pesanan lama; pemeriksaan izin menentukan halaman dari receipt server, termasuk setiap ID dalam aksi massal. Tautan lama dari pemetaan/keuangan diarahkan frontend ke detail EasyBoss lalu kembali ke daftar EasyBoss.
- Notifikasi biasa dan push memisahkan sumber, izin halaman, penugasan toko dan tujuan tautan. Dashboard tetap menghitung seluruh sumber; angka agregat semua toko tidak ditautkan ke satu inbox yang hanya memuat sebagian angka. Tautan per toko mengikuti sumbernya.
- Halaman EasyBoss membaca heartbeat sinkron yang sudah ada. Tombol sinkron manual tetap memerlukan izin `toko_penjualan` yang dipakai Order Bridge sebelumnya. Membuka halaman tidak menjalankan impor atau mengubah scheduler. Cetak resi tetap menggunakan adapter sumber yang sudah ada.
- Tidak membuat salinan pesanan, mengubah ID, atau mengubah alur pemetaan produksi dan keuangan. Pesanan manual biasa tidak dimasukkan ke dua inbox sumber marketplace ini.

Deploy backend sebelum frontend. Tidak ada migrasi tabel atau penghapusan data.
