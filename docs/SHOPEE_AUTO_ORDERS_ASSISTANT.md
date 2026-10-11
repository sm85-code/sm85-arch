# Pesanan Shopee otomatis dan perluasan asisten

Admin membuka halaman **Toko**, mencentang toko pada panel **Pesanan otomatis — drop off**, lalu menyetujui simpan. Default semua toko nonaktif. Hanya pesanan yang dibuat setelah aktivasi, berstatus `READY_TO_SHIP`, diproses. Pesanan lama tetap manual. Mematikan izin menghentikan proses berikutnya; permintaan yang telah dikirim ke Shopee tidak dibatalkan.

Worker internal berjalan setiap 60 detik setelah sinkronisasi pesanan. Memerlukan konfigurasi live Shopee yang sudah dipakai ERP. Ini bukan panggilan model AI. Jika gudang aktif, SKU harus terpetakan dan stok terreservasi cukup. Jika Shopee membutuhkan cabang drop off atau nama pengirim, Admin melengkapinya di panel. Tidak ada fallback otomatis ke pickup. Paket tetap harus disiapkan dan diserahkan secara fisik.

Satu klaim tersimpan per pesanan sebelum panggilan provider. `FOR UPDATE SKIP LOCKED` dan primary key pesanan mencegah worker ganda. Status `running`/`unknown` juga menahan proses manual. Proses terputus lebih dari 10 menit menjadi `unknown`, tidak diulang. Admin memeriksa Seller Centre sebelum menandai sudah diperiksa dan melanjutkan manual bila perlu. Hasil dan hambatan tampil di panel.

## Asisten

Kode internal, tanpa token:
- `pesanan otomatis`
- `aktifkan pesanan otomatis` / `nonaktifkan pesanan otomatis` (draf + konfirmasi)
- `ringkasan pendapatan shopee`

Pilih toko dan mode Perintah untuk perubahan. Fungsi ERP berikut kini juga terdaftar dalam tools asisten:
- Metadata publikasi, salin draf, publikasi produk baru.
- Informasi promosi/pelanggaran produk; tambah/inisialisasi/hapus varian; hapus listing tanpa menghapus histori.
- Profil, mode libur, jasa kirim/COD/driver, fungsi alamat toko.
- Pelacakan pesanan, ringkasan pendapatan Shopee.
- Konfirmasi retur, alasan/persyaratan sengketa, pengajuan sengketa dengan bukti unggahan ERP.
- Akhiri/hapus promosi.
- Baca dan atur izin pesanan otomatis pada toko terpilih.

Bahasa bebas memakai model yang dikonfigurasi untuk memilih fungsi typed; eksekusinya menggunakan service ERP. Mode Tanya tidak mengizinkan perubahan. Setiap perubahan melalui chat memerlukan draf/konfirmasi dan receipt. ID produk/pesanan dicek terhadap toko yang dipilih. Referensi publikasi dibuat server, bukan model. Foto produk dan bukti retur tetap diunggah lewat UI ERP; tidak boleh mengarang ID/URL bukti. OAuth membutuhkan login interaktif di Shopee. Pengunduhan PDF resi dan unggahan berkas tetap melalui UI ERP, belum melalui balon chat.

Tabel baru `mpe_auto_order_policy` dan `mpe_auto_order_attempt` dibuat oleh startup schema initializer; tidak mengubah atau menghapus tabel lama.
