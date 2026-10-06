# Pesanan internal diabaikan/dibatalkan

## Aksi dan riwayat

Daftar Order menyediakan **Abaikan / Batalkan Pesanan** pada pesanan `draf`. Form meminta alasan 3–2000 karakter dan mengirim `POST /api/bumi-lestari/keu/pesanan/{id}/status` dengan `status=batal`. Status memakai kolom/constraint yang sudah ada; tidak menambah status baru atau mengubah sumber Store/ERP/Shopee. Riwayat audit menyimpan alasan dan perubahan status. Retry pembatalan yang sama bersifat idempotent.

Tab **Pengerjaan** mengambil `GET /keu/pesanan?status=pengerjaan` (default); tab **Dibatalkan** memakai `status=batal`. `status=semua` tersedia untuk pembacaan gabungan. Filter diterapkan sebelum count/pagination/search. History mempertahankan item, tanggal dan nominal snapshot, tetapi tidak menyediakan aksi produksi/pemetaan. Detail order tetap dapat dibaca pengguna berhak.

`GET /keu/item` default menyembunyikan item pesanan batal dari pilihan Produksi dan Rekonsiliasi Settlement. History item dapat dibaca memakai filter yang sama. Guard owner/admin dan wajib ganti password tetap berlaku.

## Pengecualian keuangan

- Nilai/omset dan jumlah pesanan mengabaikan status `batal`.
- Hitungan belum dipetakan, biaya/alokasi vendor dan pilihan item mengabaikan parent order yang batal.
- Settlement terkait diidentifikasi melalui `rincian.order_sn` yang memang diisi oleh ERP sync, dicocokkan dengan nomor order pada saluran yang sama, atau melalui alokasi settlement → item → pesanan. Nomor yang sama di saluran lain tidak ikut tersaring.
- Settlement terkait tidak ditampilkan di daftar rekonsiliasi atau hitungan draf. Source settlement baru untuk order batal diabaikan tanpa membuat baris settlement; envelope masukan tetap tersimpan. Input manual settlement terkait ditolak.
- Posting settlement/transaksi terkait ditolak. Kas/daftar transaksi dan alokasi settlement juga mengabaikan relasi batal bila ada data lama yang tidak konsisten. Tidak ada penghapusan jurnal atau perubahan nominal sumber.
- Pembatalan ditolak jika ada alokasi vendor aktif, alokasi settlement, atau settlement yang sudah diposting dan merujuk nomor sumber tersebut. Alokasi/pencatatan tersebut harus dikoreksi melalui alur keuangan yang berlaku, bukan dihapus oleh tombol pembatalan. Catatan kas manual yang tidak mempunyai relasi settlement/order tetap dihitung; tidak ditebak dari teks keterangan.

Penarikan ulang sumber mempertahankan status internal `batal`, termasuk revisi sumber baru yang mengubah qty/harga. Revisi finansial/item diabaikan; hanya referensi status/timestamp sumber diperbarui. Tidak mengaktifkan kembali atau membuat order duplikat.

## Konkurensi

Pembatalan memegang row lock pesanan. Posting settlement mengunci pesanan terkait dalam urutan ID, sehingga transaksi posting dan pembatalan tidak dapat sama-sama lolos. Posting transaksi terkait mengikuti urutan settlement → transaksi → pesanan, konsisten dengan posting settlement. Tidak ada koneksi tenant tambahan atau perubahan konfigurasi Neon/pgBouncer.

Tes SQLite/API menguji history/pagination, role, retry, audit, penolakan alokasi/posting, pengecualian nominal dan batas saluran. Tes PostgreSQL menguji posting dan pembatalan bersamaan. Playwright desktop/mobile menguji aksi sukses, hilang dari pengerjaan, history readonly dan alasan yang tetap tersedia saat pembatalan ditolak.
