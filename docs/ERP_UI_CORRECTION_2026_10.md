# Koreksi alur kerja ERP — Oktober 2026

Perubahan ini menanggapi bug yang terlihat langsung oleh pengguna. Kontrak yang tersedia tetap kompatibel; tabel tetap digunakan pada HP dan AI yang ada tidak diperluas.

## Perubahan yang terlihat

- Checkbox HP persegi; pilih semua menyertakan resi yang sudah dicetak, dengan konfirmasi cetak ulang.
- Pilihan diperbarui dari data terbaru. Produk, varian dan SKU tetap sejajar per item; jumlah barang selalu terlihat.
- Pesan pembeli `message_to_seller` dipisahkan dari catatan internal `note`. Daftar/detail/mutasi memakai satu serializer snapshot bertipe.
- Modal produk tidak melebar keluar layar HP. Judul membungkus, paragraf panjang rata kanan-kiri, tabel varian menggulir sendiri.
- Sinkronisasi tersedia pada Pesanan, detail Pesanan, Katalog, detail produk, Produk dan Listing. Cakupan: satu item, pilihan, satu toko, seluruh toko yang diizinkan. Segarkan hanya memperbarui tabel ERP.
- Sinkronisasi besar memakai antrean baca tersimpan: satu unit per request, deduplikasi job aktif, jeda/lanjut, progres dan ulangi hanya sasaran gagal. Detail pesanan langsung membaca nomor pesanan tersebut; produk langsung membaca item terkait. Tidak mengubah stok/harga master secara otomatis. Cakupan pesanan seluruh toko mencakup perubahan 15 hari terakhir; pesanan lama dapat dibaca melalui pilihan ID.
- Chat menyediakan inbox lintas toko, filter belum dibaca, pencarian halaman, pagination, riwayat, balasan teks dan tanda sudah dibaca. Pesan dideduplikasi menurut ID, konteks order/produk hanya dipetakan dari referensi API yang tepat pada toko yang sama. Pengiriman punya receipt persisten: timeout tidak memicu kirim ulang otomatis. Media belum dikirim dari ERP; pesan nonteks diarahkan untuk dilihat di Shopee.
- Harga/ongkir nol tetap nol, data uang tidak valid ditolak, biaya settlement yang tidak tersedia tampil kosong. Label penjualan sebelum diskon, konversi iklan 7 hari, mata uang/pecahan dan waktu WIB diperjelas.
- Respons varian standardise dan legacy dinormalisasi. Pembaruan item pesanan memakai identitas item/model; pasangan ambigu tidak menimpa riwayat ERP.

## Verifikasi

Browser Chromium dengan data API tiruan pada 390px/1440px membuktikan checkbox16×16, pilih semua cetak ulang/batal tanpa request cetak, status berubah menghapus aksi cetak, pesan pembeli terlihat, sinkron satu pesanan memakai ID tersebut, modal tidak overflow dan teks justify, serta kirim Chat satu kali dan tanda baca. Tidak ada tindakan marketplace nyata.

Regresi BE mencakup nilai nol, pesan/catatan terpisah, varian standar, urutan item berubah, scope antrean dan kepemilikan job, hasil parsial/retry, pengiriman Chat tidak pasti dan penolakan riwayat percakapan lain. Pemeriksaan existing yang relevan meliputi sinkron pesanan, katalog, pengelolaan produk, resi massal, settlement dan iklan. Build/lint FE serta pemeriksaan navigasi/petunjuk/mata uang dijalankan. CI PR harus hijau sebelum merge.

## Referensi dan batas verifikasi

Referensi pesanan/produk/discount mengikuti salinan dokumentasi pada `sm85-code/api-docs` dan cache endpoint get_order_detail/get_model_list/get_item_base_info/get_discount_list.

Chat: [FAQ137](https://open.shopee.com/faq/137), [riwayat671](https://open.shopee.com/documents?module=109&type=1&id=671&version=2), [kirim672](https://open.shopee.com/documents?module=109&type=1&id=672&version=2), [inbox673](https://open.shopee.com/documents?module=109&type=1&id=673&version=2), [detail674](https://open.shopee.com/documents?module=109&type=1&id=674&version=2), [baca679](https://open.shopee.com/documents?module=109&type=1&id=679&version=2). Endpoint resmi tercantum dalam FAQ yang tersedia; kontrak parameter/response dicocokkan dengan EcomPHP/shopee-php dan easycb/easycb-go. Pembacaan ulang halaman endpoint resmi via Jina diblokir proxy403; bukan verifikasi live permission toko. Error API ditampilkan, bukan fitur dinonaktifkan berdasarkan dugaan permission. Push Chat belum dikonfigurasi; inbox/riwayat memakai polling terbatas saat halaman aktif.

502/504 Promosi produksi belum dapat dibuktikan penyebabnya tanpa log App Platform dan SHA deployment. Timeout provider baca dipendekkan, connect/read dibatasi, dan log fase token/provider beserta request_id/waktu ditambahkan tanpa token/payload pelanggan. Ini membantu diagnosis; tidak membuktikan gangguan origin sudah pulih. Ringkasan keuangan masih berdasarkan snapshot yang tersedia, bukan laporan pendapatan bersih lengkap.

Tabel baru dibuat melalui bootstrap schema additive yang sudah ada. Tidak ada perubahan kolom lama atau migrasi stok/ledger destruktif. Merge tidak sama dengan konfirmasi deployment produksi.
