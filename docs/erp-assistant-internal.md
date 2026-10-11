# Asisten ERP: kode internal dan Sonnet

Asisten tetap khusus Admin ERP. Tidak ada model GPT/manajer tambahan, API key baru,
worker coding, atau migrasi database produksi yang diperlukan untuk perubahan ini.

## Pembagian kerja

- **Internal:** mengenali permintaan rutin lengkap, membaca API/service resmi,
  menghitung laporan melalui service yang ada, menyimpan catatan, dan mengeksekusi
  argumen draf yang telah disetujui. Token dan biaya model nol.
- **Sonnet:** memahami bahasa bebas, meminta informasi yang kurang, membaca data
  melalui tools, menganalisis dan menyusun draf. Tarif tetap mengikuti konfigurasi
  server/penyedia; tidak menjanjikan akurasi model 100%.
- **Coding:** membantu menjelaskan masalah, langkah reproduksi, kebutuhan, risiko,
  dan kriteria penerimaan. Rencana tersimpan di riwayat percakapan. **Belum ada
  akses kode/shell/GitHub/migrasi/deployment atau eksekusi perbaikan otomatis.**
- Pilihan bidang Operasional/Iklan membatasi definisi tools yang dikirim ke model
  untuk mengurangi konteks. Bidang Coding tidak memberikan tools perubahan.
  Pilihan bidang bukan pemberian hak akses baru.

## Perintah internal tanpa token

Pilih toko untuk data live. Awalan `cek`, `lihat`, `tampilkan`, atau `tolong`
boleh digunakan. Permintaan yang tidak dikenali secara lengkap memakai Sonnet,
bukan dipaksakan ke periode/tindakan yang berbeda.

| Contoh | Hasil |
| --- | --- |
| `bantuan` / `daftar toko` | Bantuan / daftar toko |
| `cari produk rak bambu` | 20 hasil katalog snapshot |
| `daftar pesanan` / `cari pesanan NOMOR` | 20 pesanan snapshot |
| `pesanan perlu diproses` | Daftar sesuai tahap ERP |
| `ringkasan penjualan bulan ini` | Omzet dan pesanan, bukan laba |
| `ringkasan penjualan kemarin` | Periode kemarin dalam WIB |
| `ringkasan penjualan 7 hari terakhir` | Periode eksplisit, maksimum 93 hari |
| `stok kritis` | Maksimum 20 produk gudang internal terkait cakupan toko |
| `produk terlaris bulan ini` | Maksimum 20 hasil snapshot |
| `saldo toko` | Saldo terakhir tercatat pada mutasi, bukan jaminan dana tersedia |
| `pendapatan toko` | Pendapatan Shopee terkini belum cair/sudah dirilis, bukan kas/laba |
| `kotak masuk chat` / `chat belum dibaca` | Percakapan pembeli, belum dibaca berbeda dari belum dibalas |
| `kesehatan toko` | Account Health Shopee |
| `daftar iklan` / `daftar iklan 7 hari terakhir` | Kampanye dan metrik; periode 2–28 hari |
| `daftar promosi` / `kelayakan gmv` | Diskon toko / kelayakan Shop GMV Max |
| `mutasi dana 7 hari terakhir` | Transaksi dana, maksimum 15 hari, halaman pertama |
| `sinkron katalog` | Draf penarikan katalog dan penautan SKU ke ERP |
| `jeda iklan 123` / `lanjutkan iklan 123` | Draf aksi kampanye produk biasa |
| `ubah budget iklan 123 menjadi 50 ribu` | Draf anggaran Rp50.000; bukan Shop GMV Max |
| `catat perbaikan fitur: stok tidak kembali setelah batal` | Catatan permintaan; belum mengubah kode |

Mode **Tanya** tidak memberikan izin perubahan. Pilih **Perintah** dan satu toko
untuk membuat draf tindakan. Kata negatif/permintaan gabungan yang tidak cocok
penuh tidak dieksekusi oleh parser internal. Nama/nomor ambigu dibantu Sonnet.

## Kemampuan tambahan melalui Sonnet

42 tools terdaftar: 24 pembacaan dan 18 tindakan. Selain fitur produk, varian,
iklan produk/keyword, Shop GMV Max dan diskon yang sudah ada, asisten dapat:

- Membaca pesanan, stok kritis, produk terlaris, saldo dan mutasi dana.
- Membaca kotak masuk/riwayat chat dan menyusun satu balasan teks untuk disetujui Admin sebelum dikirim.
- Menyusun draf proses pengiriman satu pesanan menggunakan pengaturan alur ERP.
- Menerima/menolak permintaan pembatalan pembeli melalui service resmi.
- Menyembunyikan/menampilkan listing tanpa menghapus produk.
- Menyimpan modal produk Rp/persen untuk analisis margin dan ROAS impas.
- Menarik katalog ke ERP melalui alur sinkron yang sudah ada.

Data stok gudang, katalog snapshot dan stok live Shopee berbeda. Omzet, pendapatan
terkini, mutasi dana dan laba juga berbeda. Null tidak diubah menjadi nol.
Kemampuan live tetap mengikuti izin aplikasi Shopee serta kondisi toko/pesanan.
Belum ada auto-reply atau pemrosesan pesanan terjadwal melalui agen ini.

## Persetujuan dan keselamatan

1. Model atau parser hanya menghasilkan receipt **draft**; tidak memanggil mutasi.
2. Admin memeriksa toko, sasaran dan seluruh argumen di UI. Draf berlaku 10 menit.
3. `POST /asisten/tindakan/{id}/keputusan` menerima `decision=confirm|cancel`
   dan `all_variants` saja. Tidak menerima argumen pengganti dari browser.
4. Konfirmasi mengecek pemilik, role/sesi, usia draf dan antrean percakapan.
   Perubahan seluruh varian membutuhkan checkbox terpisah; kalimat yang memuat
   “semua varian” tidak lagi dianggap sebagai izin.
5. Argumen tersimpan diantrekan sebagai turn `internal-execution`, tanpa model.
   Konfirmasi berulang mengembalikan turn yang sama. Receipt ditulis sebelum
   mutasi eksternal. Hasil tidak pasti berhenti dan tidak diulang otomatis.
6. Pembatalan tidak menjalankan tindakan. Draf yang ditolak/kedaluwarsa harus
   dibuat ulang; jangan mengganti ID saat hasil pengiriman masih tidak pasti.

Frontend memperbarui query data aplikasi setelah receipt perubahan berhasil.
Error HTTP 4xx definitif (kecuali 408) membuka formulir; timeout/jaringan/5xx
mempertahankan ID operasi untuk pemeriksaan ulang yang aman.

Hasil lengkap dan status asli disimpan pada receipt. Pemotongan hanya untuk
konteks model; `ok=false`, request_id dan status tidak hilang. Status partial/
belum pasti dari provider tidak ditampilkan sebagai keberhasilan penuh.

## Batas dan operasional

- Internal: maksimum 200 turn per Admin per hari WIB, termasuk konfirmasi.
  Tidak memakai kuota token/pesan Sonnet. Tetap berjalan tanpa API key Sonnet.
- Sonnet: batas biaya, pesan, langkah dan tools yang sudah ada tetap berlaku.
- Advisor iklan terpisah: reservasi percobaan dicatat sebelum model dipanggil,
  diserialisasi melalui lock database, dan hari dihitung dalam WIB. Permintaan
  terputus tetap memakai satu percobaan; biaya tanpa jawaban belum bisa dipastikan.
  Kuota advisor terpisah dari budget chat, bukan anggaran gabungan seluruh AI.
- Tidak ada tabel/kolom baru. Menggunakan antrean, receipt dan budget yang ada.
- Rollout: backend dahulu, lalu frontend. UI lama tidak dapat mengonfirmasi draf;
  backend baru tetap tidak mengeksekusi perubahan hanya dari keputusan model.
- Dokumentasi ini menggantikan keterangan lama bahwa mode Perintah langsung
  mengeksekusi perubahan tanpa persetujuan.
