# Retur & Refund — Pesanan ERP

Bagian ini menyediakan pembacaan langsung daftar dan detail retur/refund Shopee. Buka **Pesanan → Retur & Refund**, pilih toko dan tanggal pengajuan, kemudian **Tampilkan**. Tabel tetap digunakan pada HP; geser ke samping untuk melihat kolom lain. Gunakan **Berikutnya** bila Shopee menyatakan masih ada hasil. **Detail** menampilkan barang, alasan, status negosiasi/bukti/kompensasi, resi, dan tenggat yang diberikan Shopee.

Tanggal dihitung sebagai hari kalender lengkap dalam WIB. Rentang maksimum 15 hari; awal halaman memakai 15 hari terakhir termasuk hari ini. Filter berdasarkan tanggal pembuatan permintaan retur, bukan tanggal pesanan dan bukan tanggal terakhir diubah. Untuk permintaan lebih lama, pilih rentang pengajuan sebelumnya. Nominal refund mempertahankan mata uang provider. Refund per item yang tidak diberikan tidak diganti dengan harga item.

## API ERP

- `GET /api/marketplace-erp/akun/{akun_id}/retur?dari=YYYY-MM-DD&sampai=YYYY-MM-DD&halaman=1&per_halaman=40`
- `GET /api/marketplace-erp/akun/{akun_id}/retur/{nomor_retur}`

Daftar mengembalikan `items`, `halaman`, `per_halaman`, `ada_lagi`. Tidak mengarang total yang tidak disediakan Shopee. Adapter mengirim `page_no`, `page_size`, `create_time_from`, `create_time_to` ke `v2.returns.get_return_list`. Detail memakai `return_sn` pada `v2.returns.get_return_detail` dan memverifikasi identitas respons. Kode/status baru tetap dapat ditampilkan.

Akses owner/admin dan staff mengikuti pembatasan toko existing. Pemeriksaan akses dilakukan sebelum API provider dipanggil. Pesanan ERP dihubungkan hanya ketika platform, toko dan nomor pesanan cocok. Jika pesanan belum tersinkron, nomor Shopee tetap terlihat. Error provider diteruskan dengan kode dan request ID; respons tidak lengkap tidak berubah menjadi daftar kosong yang terlihat sukses. Token tetap ditangani helper signed-request existing.

## Batas bagian ini

Tidak ada migrasi database, penambahan stok, perubahan status pesanan, persetujuan retur, sengketa, atau perubahan settlement. Status retur merupakan proses berbeda dari status pesanan. Barang harus benar-benar diterima dan diperiksa sebelum penambahan stok dilakukan lewat alur ERP yang sesuai. Nominal refund pada permintaan tidak berarti dana sudah direkonsiliasi.

Persetujuan, unggah bukti, negosiasi dan sengketa masih ditangani di Seller Centre pada bagian ini. Pelengkapan tindakan retur serta fitur lain dilanjutkan bertahap; panduan pemula menyeluruh tetap tahap akhir. ERP tetap dirancang lintas marketplace, dengan adapter Shopee sebagai implementasi API saat ini.

## Referensi dan pengujian

- https://open.shopee.com/documents/v2/v2.returns.get_return_list?module=102&type=1
- https://open.shopee.com/documents/v2/v2.returns.get_return_detail?module=102&type=1
- Kontrak dipelajari dari salinan https://github.com/nhutplus/marketplace-api-docs (endpoint dicrawl 15 Mei 2026) dan panduan https://github.com/sm85-code/api-docs.

Pengujian menggunakan respons tiruan, tanpa tindakan pada toko nyata: batas tanggal/WIB, paginasi, status baru, refund nol/absen, identitas detail, respons tidak lengkap, error permission, akses staff, tautan pesanan antar toko, serta stok dan ledger yang tetap.
