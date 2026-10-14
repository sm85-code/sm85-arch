# Laporan keuangan mingguan dan bulanan

Halaman `/laporan-keuangan` dan semua turunannya, serta seluruh endpoint `/api/marketplace-erp/laporan-keuangan/*`, hanya untuk admin. Owner/staff tidak memperoleh akses melalui izin toko. Belum ada pemberian akses laporan kepada role lain.

## Kalender dan penyajian

- Mingguan: Senin 00:00 WIB sampai Minggu 23:59 WIB. Disajikan Selasa berikutnya.
- Bulanan: tanggal 1 00:00 WIB sampai hari terakhir bulan 23:59 WIB. Disajikan pada Selasa setelah cut-off selesai, bukan ditutup pada hari Selasa.
- Contoh Oktober 2026: tutup bulanan 31 Oktober; penyajian 3 November. Minggu 26 Oktober–1 November juga disajikan 3 November. Transaksi 26–31 Oktober milik Oktober; 1 November milik November.
- Jika akhir bulan jatuh Senin, bulanan disajikan Selasa besoknya; laporan mingguan pekan itu menunggu Minggu selesai lalu Selasa berikutnya. Jika akhir bulan jatuh Selasa, cut-off baru selesai pukul 23:59, sehingga penyajian bulanan pada Selasa berikutnya.

Sistem menyimpan cut-off berurutan pada Minggu dan akhir bulan. Dalam split week, simpan saldo akhir bulan dahulu, kemudian sisa pekan sampai Minggu. Laporan mingguan menggabungkan kedua bagian tanpa mengubah tanggal transaksi. Tidak membagi total minggu secara rata-rata berdasarkan jumlah hari. Apabila akhir bulan sama dengan Minggu, satu cut-off dipakai untuk keduanya.

Ringkasan bulanan menggunakan bagian transaksi tanggal 1–akhir bulan saja. Neraca bulanan menggunakan saldo akhir bulan, bukan saldo Minggu setelahnya. Perubahan piutang November menggunakan saldo 31 Oktober sebagai saldo awal. Data minggu pertama atau bulan pertama yang belum tercakup dari awal ditandai **belum lengkap**, bukan final.

Nominal transaksi pelengkap wajib memiliki tanggal. Tanggal di luar interval cut-off ditolak. Settlement ERP harus memiliki tanggal kas masuk aktual; tanggal akhir statement bukan tanggal penarikan. Label periode dan jadwal penyajian dicantumkan terpisah pada halaman dan CSV. Menyimpan cut-off baru diperbolehkan setelah hari cut-off selesai; admin boleh menyiapkan pratinjau lebih awal.

## ERP sebagai sumber

ERP menjadi sumber settlement paid, pemakaian iklan harian toko, daftar pesanan shipped, dan stok fisik (tersedia + reservasi aktif). Data iklan difilter tanggal, tidak ditambahkan lagi dari metrik kampanye. Settlement paid merupakan kandidat; admin memilih yang benar-benar masuk kas dan mengisi tanggal bank aktual. ID settlement yang sudah dipakai ditolak. Escrow yang baru berpindah ke wallet bukan otomatis kas perusahaan.

Piutang pengiriman dikonfirmasi berdasarkan penerimaan bersih, bukan langsung total bruto pesanan. Saldo tertahan toko, biaya persediaan, pembayaran tukang/produk, operasional, langganan, gaji, dan top-up yang belum tersedia sebagai transaksi akuntansi ERP dilengkapi pada laporan. Seluruh transaksi pelengkap, tanggal aktual, sumber terpilih dan catatan tersimpan bersama snapshot. Daftar pesanan/stok merupakan kondisi ERP saat dibaca, tidak diasumsikan sebagai histori.

## Perhitungan

- Pendapatan = penarikan toko + pendapatan produk/jasa + perubahan (piutang pengiriman + saldo tertahan toko).
- HPP = pembayaran tukang/produk + persediaan awal − akhir + utang usaha akhir − awal. Untuk stok/utang tetap, sama dengan pola Bumi Lestari.
- Beban = HPP + operasional + langganan + gaji + pemakaian iklan.
- Top-up mengurangi kas dan menambah iklan prabayar. Pemakaian mengurangi prabayar dan laba. Tanpa top-up, tidak ada kas keluar baru.
- Penyisihan kas gaji memindah kas utama ke kas gaji. Gaji yang diakui menambah utang. Pembayaran mengurangi kas gaji dan utang gaji.
- Transfer antar kas tidak menambah pendapatan; tambahan modal tidak menambah laba.
- Aset/utang tidak boleh negatif dan neraca wajib seimbang saat simpan.

## Penyimpanan, keamanan, dan koreksi

Laporan membaca ERP dan menulis hanya ke tabelnya sendiri: saldo awal, snapshot tutup buku, dan audit. Tidak mengubah pesanan, stok, settlement, iklan, atau memicu sinkronisasi marketplace. Tabel baru dibuat oleh startup schema creation yang sudah ada; tidak ada perubahan kolom ERP lama.

Saldo awal harus seimbang dan terkunci setelah tutup pertama. Fingerprint pratinjau mencakup input, sumber, dan saldo sebelumnya. Jika berubah sebelum simpan, API meminta pratinjau baru. Row lock PostgreSQL pada saldo awal menserialisasi mutasi laporan; tanggal cut-off unik mencegah tutup ganda.

Laporan tersimpan tidak dihitung ulang dari kondisi ERP terbaru. Hanya cut-off terbaru dapat dibuka kembali, dengan alasan; snapshot lama tetap diaudit. Membuka ulang akhir bulan setelah menutup sisa pekan memerlukan pembukaan sisa pekan terlebih dahulu.

Snapshot kalender lama berbasis Selasa dipertahankan dan ditampilkan terpisah. Tidak otomatis dijumlahkan ke laporan kalender baru atau dibagi rata. Jika ada, buka kembali dari terbaru secara berurutan, lalu tutup ulang dengan tanggal dan saldo yang benar.

Validasi mencakup fixture Bumi Lestari, split week 26 Oktober–1 November, tanggal kas masuk aktual, saldo piutang akhir bulan, iklan lintas bulan, tahun kabisat, akhir bulan Senin/Selasa/Minggu, laporan parsial/legacy, snapshot dan koreksi, akses HTTP admin, dan regresi ERP.

## Periode berjalan: Drive sebagai buku utama

Untuk periode yang sudah berjalan, impor tab LAPORAN Google Drive sebagai sumber utama melalui endpoint admin `/impor-drive`. Berkas JSON memuat URL sumber, nama tab, waktu pembacaan, dan nilai mentah B:I. Parser hanya mengimpor kolom berstatus OK; merekonsiliasi pemasukan, beban, laba, semua saldo, dan selisih neraca. Tidak menimpa saldo/riwayat yang sudah ada. Impor berkas sama bersifat idempoten; konflik bersamaan ditolak. Ekspor pribadi tidak dimasukkan ke git.

Angka aktual 6 dan 13 Oktober disimpan menurut periode aslinya (1–6 dan 7–13 Oktober), bukan digeser menjadi tanggal Minggu. Akumulasi Oktober tetap benar. Mingguan Senin–Minggu pada riwayat tersebut belum dapat direkonstruksi tanpa transaksi harian; sistem tidak membagi angka rata-rata. Setelah saldo 13 Oktober, cut-off berikutnya 18 Oktober memuat 14–18 Oktober, lalu minggu selanjutnya mengikuti kalender baru.

Setelah impor, ERP hanya pembanding. Pemakaian iklan dan penarikan dari ERP tidak otomatis memengaruhi laba/kas. Penarikan dan pemakaian iklan buku utama harus dimasukkan sebagai transaksi bertanggal (pemakaian iklan termasuk angka 0 jika nihil). Perbedaan angka ERP tidak memperbarui snapshot Drive.

Bagian pembanding menampilkan dua panel bersebelahan di desktop dan atas-bawah di ponsel, khususnya piutang pengiriman, saldo tertahan, dan total piutang. Saldo tertahan Drive tetap terpisah. ERP tidak mempunyai pemisahan itu; kolomnya disebut piutang belum ditarik (gabungan). Karena ERP saat ini tidak menyediakan saldo gabungan historis terverifikasi, tampilkan status belum tersedia, bukan nol atau saldo buatan. Nilai pesanan shipped di ERP adalah bruto dan posisi saat dibaca; tidak dihitung selisih terhadap piutang bersih Drive. Pemakaian iklan periode yang sama boleh ditampilkan dengan selisih, disertai kelengkapan sinkronisasi. Endpoint pembanding dan impor tetap admin-only.

### Pembanding pemasukan Shopee per toko

GET /laporan-keuangan/pemasukan-shopee?periode=YYYY-MM&jumlah_kolom=5 khusus admin, baca saja. Tabel di bawah Pemasukan memakai minggu Senin–Sabtu, mulai Senin pada minggu tanggal 1; Oktober 2026 dimulai 28 September–3 Oktober. Minggu tidak masuk tabel ini. Filter memakai tanggal dikirim (tanggal_kirim) WIB; pesanan tanpa tanggal kirim tidak dimasukkan dan status terkini, bukan rekonstruksi status historis. Pengiriman memakai total bruto sebagai estimasi, selesai memakai jumlah_cair settlement tersimpan, tanpa penghitungan ganda. Pencairan yang belum tersedia ditandai belum lengkap; nol riil yang sah tetap nol. Semua toko Shopee ditampilkan termasuk tanpa transaksi. Tabel ini tidak mengubah kalender maupun angka buku utama/bulanan dan tidak memicu sinkronisasi marketplace.
