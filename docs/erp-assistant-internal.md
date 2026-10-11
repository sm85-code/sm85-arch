# Asisten ERP: internal, manajer Sol dan spesialis Sonnet

Asisten tetap khusus Admin ERP. Tidak ada worker coding atau migrasi database baru.
API key hanya berada di environment backend; tidak dikirim ke browser atau model lain.

## Pembagian kerja

- **Internal:** mengenali permintaan rutin lengkap, membaca API/service resmi,
  menghitung laporan melalui service yang ada, menyimpan catatan, dan mengeksekusi
  argumen draf yang telah disetujui. Token dan biaya model nol.
- **Sol (mode Otomatis):** menangani bahasa bebas setelah parser internal, membaca data
  lewat tools resmi, dan dapat mendelegasikan satu analisis operasional/iklan/rencana
  coding ke Sonnet. Hasil delegasi diperiksa dan dirangkum manajer. Tidak ada
  perulangan agen: maksimal satu delegasi, tiga panggilan spesialis dan enam
  panggilan model gabungan per pesan.
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
boleh digunakan. Permintaan yang tidak dikenali secara lengkap memakai manajer Sol bila tersedia
(mode Otomatis), atau Sonnet,
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
penuh tidak dieksekusi oleh parser internal. Nama/nomor ambigu dibantu model, bukan ditebak oleh parser.

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
- Sol + Sonnet dalam chat memakai satu cadangan pesan dan budget harian yang sama.
  Token delegasi dijumlahkan; biaya memakai tarif masing-masing model. Reasoning
  tokens OpenAI termasuk output berbayar. Tarif adalah estimasi konfigurasi, bukan
  pengganti tagihan provider. Cadangan tidak cukup menghentikan panggilan berikutnya.
- Advisor iklan terpisah: reservasi percobaan dicatat sebelum model dipanggil,
  diserialisasi melalui lock database, dan hari dihitung dalam WIB. Permintaan
  terputus tetap memakai satu percobaan; biaya tanpa jawaban belum bisa dipastikan.
  Kuota advisor terpisah dari budget chat, bukan anggaran gabungan seluruh AI.
- Tidak ada tabel/kolom baru. Menggunakan antrean, receipt dan budget yang ada.
- Rollout: backend dahulu, lalu frontend. UI lama tidak dapat mengonfirmasi draf;
  backend baru tetap tidak mengeksekusi perubahan hanya dari keputusan model.
- Dokumentasi ini menggantikan keterangan lama bahwa mode Perintah langsung
  mengeksekusi perubahan tanpa persetujuan.


## Konfigurasi manajer di DigitalOcean

Simpan hanya di backend:

```text
OPENAI_API_KEY=<rahasia project OpenAI>
OPENAI_MODEL=gpt-6.1-sol
ERP_AI_DAILY_USD=0.50
ERP_AI_TURN_USD=0.20
```

Tanpa konfigurasi batas khusus, default budget gabungan chat adalah $0,50/hari
WIB dan cadangan $0,20/pesan. Nilai environment lama mengalahkan default: periksa
jika sebelumnya sudah memasang batas lebih tinggi. Ini batas lokal chat, bukan
batas saldo seluruh project OpenAI. Advisor iklan terpisah masih memakai kuota
sendiri. Biaya aktual lihat billing provider, termasuk pemakaian aplikasi lain.

`ANTHROPIC_API_KEY` dan `ERP_AI_MODEL` tetap untuk Sonnet. Mode Otomatis memakai
internal dahulu lalu Sol; memilih Operasional/Iklan/Coding langsung memakai
Sonnet bila tersedia untuk menghemat koordinasi. Jika Sonnet tidak tersedia,
Sol menangani bidang tersebut tanpa delegasi. Jika OpenAI belum dipasang, jalur
Sonnet lama tetap tersedia. Provider gagal tidak otomatis dialihkan/dicoba ulang.

Responses API menggunakan `store=false`, endpoint resmi tetap, effort `low`,
tanpa web search, browser, shell, atau tool di luar registry ERP. Reasoning
terenkripsi hanya dipertahankan dalam loop sementara, tidak dicatat di riwayat.
Model allowlist: `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-astra`; tarif default mengikuti
katalog saat integrasi dan dapat diperbarui lewat `ERP_MANAGER_INPUT_USD_PER_MILLION`
serta `ERP_MANAGER_OUTPUT_USD_PER_MILLION`. Model/key yang tidak dapat diakses
provider tetap bisa ditolak; status “siap” hanya menunjukkan konfigurasi lokal,
bukan bukti uji API/deployment sukses.

Contoh mode Otomatis:
- “Periksa kondisi toko dan tentukan tiga pekerjaan paling penting hari ini.”
- “Minta Sonnet evaluasi iklan, lalu rangkum rekomendasi tanpa mengubah budget.”
- “Minta agen coding susun rencana mengatasi stok yang tidak kembali saat batal.”

Coding saat ini **belum mengedit BE/FE, membuat PR, atau redeploy**. Sol tidak
memperluas daftar kemampuan sistem yang belum diimplementasikan.
