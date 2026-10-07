# Tab pengeluaran keu BUMI Lestari

Halaman Keuangan menggunakan empat tab ketika buku besar keu sudah aktif. Guard admin/owner dan wajib ganti password tetap berlaku. Modul iPaymu, sumber Store/ERP dan tabel transaksi legacy bl tidak diubah.

| Tab | Pencatatan | Dampak laporan |
| --- | --- | --- |
| Pembayaran Tukang & Supplier | Debet Utang Vendor; kredit Kas/Bank | HPP Vendor sudah diakui saat produksi selesai; pelunasan tidak menambah HPP kedua kali |
| Belanja Cat & Bahan Pendukung | Debet HPP Bahan; kredit Kas/Bank | Masuk HPP, kelompok hpp_bahan |
| Belanja Operasional | Debet akun sewa/langganan/pemeliharaan/operasional; kredit Kas/Bank | Beban operasional sesuai kategori |
| Gaji Karyawan & Iklan | Debet Beban Gaji atau Beban Pemasaran/Iklan; kredit Kas/Bank | Beban operasional/pemasaran |

Bahan pendukung pada tab kedua dibebankan langsung saat pembayaran; tab ini tidak mengklaim pencatatan persediaan bahan atau konsumsi FIFO bahan baku. Persediaan barang ready tetap memakai modul Persediaan yang sudah ada.

Setiap tab mempunyai tanggal awal/akhir, pencarian keterangan/kategori/referensi/vendor, pagination, daftar aktif dan riwayat dibatalkan. Batal Post memakai endpoint jurnal yang sudah ada: alasan wajib, transaksi asli/audit dipertahankan, periode tertutup tetap dilindungi. Seluruh laporan dan saldo kas mengecualikan jurnal yang dibatalkan.

Input pengeluaran baru mempunyai referensi idempotensi, tanggal, kas/bank, tab, kategori, jumlah positif dan keterangan. Backend memilih akun berdasarkan kategori; client tidak dapat mengirim COA atau role sewenang-wenang. Referensi yang sama tidak menggandakan pengeluaran, dan jurnal dibatalkan tidak dihidupkan kembali pada retry.

Endpoint tambahan pada router keu terjaga:

- GET /keu/pengeluaran/kategori: pilihan kategori menurut tab.
- POST /keu/pengeluaran: langsung membuat jurnal pengeluaran seimbang atomik.
- GET /keu/pengeluaran: tab, tanggal_awal, tanggal_akhir wajib; search/status/limit/offset opsional.

Tab vendor memakai POST /keu/vendor/pembayaran dan GET /keu/utang-vendor yang sudah ada. Batal Post memakai POST /keu/jurnal/{id}/unpost. Form kas generik pada buku aktif hanya untuk pemasukan; pengeluaran baru menggunakan tab yang sesuai.

Tidak ada perubahan struktur tabel atau ALTER TABLE. Tiga akun sistem tambahan HPP-BAHAN, BEBAN-GAJI dan BEBAN-IKLAN masuk CHART keu dan dibuat idempotent oleh chart() saat aktivasi/posting. Buku yang sudah aktif tetap dapat memposting pengeluaran baru tanpa migrasi ulang histori. JSONB rincian jurnal menyimpan tab/kategori/nama kategori. Jurnal historis tetap utuh: daftar membaca akun/kategori yang benar-benar tersimpan. Riwayat kas Gaji karyawan dan Biaya iklan ditampilkan di tab gaji/iklan tanpa mengubah nominal atau audit historis. Kategori lama produksi/pembelian yang tidak memiliki identitas vendor/bahan tidak ditebak ulang dan tetap dapat dilihat di Buku Kas/Jurnal.

Bug vendor_hpp sebelumnya menggunakan await di dalam generator yang diberikan ke list.extend(), sehingga menjadi async_generator dan gagal saat produksi selesai atau aktivasi histori produksi. Lookup akun utang sekarang diselesaikan sebelum membangun daftar baris; tes mencakup produksi baru, migrasi histori produksi dan pelunasan tanpa HPP ganda.

Pemeriksaan: seluruh backend/Ruff, PostgreSQL asli dengan concurrent retry, klasifikasi empat tab dan unpost, unit frontend, TypeScript/build dan Playwright desktop/mobile. Tes menggunakan database terisolasi; tidak mengubah keuangan Neon live.
