# Batch pengelolaan Shopee: retur, promosi, deskripsi

## Fitur

- Pesanan → Retur & Refund → Detail: persetujuan solusi retur/refund melalui `v2.returns.confirm`. Detail dibaca sebelum keputusan dikirim dan nomor retur dalam respons harus cocok. Persetujuan tidak mengembalikan stok, mengubah pesanan, atau mencatat settlement. Pembacaan setelah sukses yang gagal menghasilkan peringatan, bukan kegagalan mutasi.
- Katalog → Promosi Diskon: daftar dan detail berpaginasi, buat jadwal WIB, tambah produk/varian, ubah harga/batas pembelian, hapus produk/varian, akhiri atau hapus aktivitas. ID Shopee dipertahankan sebagai string pada kontrak ERP untuk menghindari kehilangan presisi JavaScript.
- Katalog → Detail → Edit produk Shopee: deskripsi teks dapat diperbarui terpisah dari nama/SKU. Batas edit ERP 3.000 karakter. Hanya field yang berubah dikirim; berat, dimensi, model, harga dan stok tidak disertakan.

## Alur dan perlindungan

Promosi hanya dapat dikelola owner/admin. Retur mengikuti akses toko owner/admin/staff. Produk promosi harus berasal dari snapshot toko yang dipilih dan model harus cocok. Snapshot model yang belum lengkap ditolak dengan pesan untuk sinkronisasi. Respons gagal per item tetap terlihat, termasuk kode/request ID; tidak dianggap sukses hanya karena HTTP berhasil. Tidak ada retry otomatis pada mutasi. UI menyediakan konfirmasi dan mencegah tindakan bertabrakan selama request.

Promosi dibuat mulai minimal satu jam kemudian, durasi minimal satu jam dan kurang dari 180 hari. Harga mengikuti mata uang toko; harga promosi bukan harga dasar ERP. Stok promo tidak disalin ke stok ERP dan tidak diubah melalui API update item promosi karena kontrak Shopee tidak mendukungnya. Hapus aktivitas ditujukan pada promosi yang belum dimulai; Shopee memutuskan kelayakan tindakan dan permission, dengan error diteruskan.

Kegagalan transport/konfirmasi yang tidak pasti mengharuskan penyegaran sebelum pengulangan agar tidak membuat promosi/keputusan ganda. Keberhasilan tidak menjamin respons baca berikutnya langsung mencerminkan perubahan. Tidak ada migrasi skema database atau perubahan Asisten AI.

## Referensi

- https://open.shopee.com/documents/v2/v2.returns.confirm?module=102&type=1
- https://open.shopee.com/documents/v2/v2.discount.get_discount_list?module=99&type=1
- https://open.shopee.com/documents/v2/v2.discount.get_discount?module=99&type=1
- https://open.shopee.com/documents/v2/v2.discount.add_discount?module=99&type=1
- https://open.shopee.com/documents/v2/v2.discount.add_discount_item?module=99&type=1
- https://open.shopee.com/documents/v2/v2.discount.update_discount_item?module=99&type=1
- https://open.shopee.com/documents/v2/v2.discount.delete_discount_item?module=99&type=1
- https://open.shopee.com/documents/v2/v2.discount.end_discount?module=99&type=1
- https://open.shopee.com/documents/v2/v2.discount.delete_discount?module=99&type=1
- https://open.shopee.com/documents/v2/v2.product.update_item?module=89&type=1

Kontrak dipelajari dari salinan API reference https://github.com/nhutplus/marketplace-api-docs dan panduan https://github.com/sm85-code/api-docs; bukan pengujian live. API bukti/sengketa, pengeditan jadwal aktivitas, voucher/flash sale, pembuatan/salin produk dan pelengkapan settlement/iklan tetap bagian lanjutan. Tahap UI/panduan seluruh fitur belum ditutup.
