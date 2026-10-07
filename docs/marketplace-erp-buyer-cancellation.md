# Permintaan pembatalan pembeli Shopee

Pada daftar Pesanan, status `IN_CANCEL` ditampilkan sebagai **Permintaan Pembatalan Pembeli** dan tidak dapat dipilih untuk proses pengiriman atau cetak resi. Buka detail pesanan untuk menerima atau menolak permintaan. Tindakan tersedia bagi owner/admin serta staff yang memiliki akses toko tersebut.

Backend menyediakan `POST /api/marketplace-erp/pesanan/{id}/pembatalan-pembeli` dengan body `{"operasi":"ACCEPT"}` atau `{"operasi":"REJECT"}`. Ini berbeda dari pembatalan oleh penjual (`/batalkan`), yang tetap tersedia dengan kontrak lama.

Sebelum keputusan dikirim, backend memeriksa status terbaru di Shopee. Hanya `IN_CANCEL` yang diterima. Adapter mengirim `order_sn` dan `operation` ke `/api/v2/order/handle_buyer_cancellation`; `response.update_time` menjadi konfirmasi respons. Error Shopee tetap diteruskan beserta kode dan request ID. Tidak ada retry otomatis pada mutasi.

Setelah sukses, backend membaca status terbaru dan mengikuti transisi pesanan/ledger yang sudah ada. Stok hanya dilepas ketika Shopee mengonfirmasi `CANCELLED`; penerimaan keputusan saja tidak melepaskan stok. Bila status belum berubah atau pembacaan gagal, respons tetap sukses dengan catatan agar pengguna melakukan sinkronisasi, bukan mengulang keputusan. FE mencegah pengiriman ulang setelah berhasil selama detail pesanan masih terbuka. Pembacaan status sebelum setiap tindakan membantu menolak keputusan yang sudah diproses melalui Seller Centre; perlindungan ini bergantung pada status terbaru yang dikirim Shopee.

Tidak ada perubahan skema database. Daftar Pesanan tetap berupa tabel di HP. Panduan singkat tersedia di fitur ini; panduan menyeluruh seluruh fitur tetap bagian tahap akhir.

## Referensi dan validasi

- Referensi resmi: https://open.shopee.com/documents/v2/v2.order.handle_buyer_cancellation?module=94&type=1
- Detail pesanan: https://open.shopee.com/documents/v2/v2.order.get_order_detail?module=94&type=1
- Kontrak dipelajari melalui salinan API reference https://github.com/nhutplus/marketplace-api-docs (dokumen endpoint dicrawl 15 Mei 2026), bersama panduan https://github.com/sm85-code/api-docs.
- Pengujian menggunakan respons tiruan, bukan menjalankan pembatalan pada pesanan toko nyata. Uji regresi mencakup reservasi stok, penerimaan/penolakan, status tertinggal, respons tidak lengkap, error provider, serta akses staff antar toko.
