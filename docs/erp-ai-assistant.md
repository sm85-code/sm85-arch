# Asisten AI ERP — T3/T4/T5

## Pengoperasian

Menu **Asisten AI** hanya admin, di sidebar atau Lainnya pada HP. Owner/staf ditolak juga di API. Riwayat percakapan milik masing-masing admin; status tindakan bisnis yang belum pasti dapat diperiksa oleh admin lain agar toko tidak terkunci ketika admin asal tidak tersedia.

Pilih toko dan mode:

- **Tanya:** membaca data dan memberi analisis. Endpoint write tetap ditolak meskipun model meminta write.
- **Jalankan perintah:** instruksi yang jelas langsung dieksekusi pada satu toko terpilih. Model harus meminta klarifikasi target/nilai/jadwal yang ambigu; tidak ada tombol Terapkan tambahan.

Kemampuan: ringkasan usaha/pencarian katalog snapshot ERP; statistik dan diagnosis kualitas produk resmi; metadata/pengaturan/edit informasi, foto existing, kategori/atribut/merek dan model/pilihan varian; performa toko/penalti; analisis dan pengelolaan iklan produk manual/auto, keyword/bid; eligibility, create/edit/products/report Shop GMV Max; diskon toko dan produknya. Field/identitas diperiksa kembali oleh fungsi ERP existing, bukan proxy URL/body bebas. Foto baru harus diunggah melalui pengelolaan katalog terlebih dahulu; asisten tidak memiliki uploader file sendiri. Mutasi listing mengingatkan sinkronisasi snapshot lokal.

Pendaftaran kampanye resmi Shopee dihapus dari lingkup. Tidak tersedia: internet/riset kompetitor, penarikan dana, video/template size chart, Flash Sale/Bundle Deal tanpa spesifikasi, atau akses tenant lain. API/izin Shopee tetap dapat menolak suatu fungsi; kode tidak menganggap semua akun otomatis memenuhi whitelist.

## Pencatatan dan batas

- POST /asisten/pesan mengembalikan 202 segera; polling riwayat mengambil progres. Antrean di DB diproses oleh worker lifespan, sehingga bukan HTTP lama yang bergantung timeout App Platform.
- Operation UUID + hash payload mencegah pesan identik dikirim ulang; ID sama dengan payload berbeda ditolak. Browser menyimpan permintaan belum terkonfirmasi di sessionStorage per user dan memakai ID yang sama untuk pemeriksaan/kirim ulang.
- Hanya satu turn aktif per percakapan. Worker memakai satu lease global DB (10 menit), SELECT FOR UPDATE, dan reservation biaya yang terkunci antarproses.
- Receipt tool disimpan/commit sebelum write. Tool identik dalam turn memakai hasil receipt; tidak ada retry otomatis provider atau model. Reference ID provider berasal dari turn/fingerprint server.
- Timeout/restart saat write menghasilkan **belum pasti**, bukan sukses palsu. Lease yang kedaluwarsa dihentikan, tidak diambil untuk mengulang write. Write baru di toko itu diblokir hingga admin memeriksa data dan mencatat hasil pemeriksaan. Pembacaan tetap tersedia. Catatan pemeriksaan tidak mengulang transaksi dan tidak mengganti hasil provider sebelumnya.
- User/role/session_version diperiksa kembali sebelum tool. Data/nama/deskripsi provider diperlakukan sebagai data tidak terpercaya, bukan instruksi. Tidak ada shell, SQL bebas, browser atau endpoint arbitrary.
- Batas default: 30 pesan/hari untuk asisten, USD10/hari, cadangan USD1.5/pesan, 6 panggilan model, 16 tool dan 5 write per pesan; input serialized maksimal 60KB, output 2.000 token/call, timeout turn 300 detik. Hasil terlalu besar meminta pencarian lebih spesifik.
- Model/tarif estimasi dibekukan pada turn. Penggunaan token disimpan setelah tiap panggilan. Cadangan dibebaskan saat selesai; bila respons tagihan hilang, estimasi konservatif menggunakan seluruh cadangan. Tarif merupakan estimasi biaya Anthropic, bukan tarif yang ditentukan untuk Anthropic. Advisor iklan lama tetap memiliki batas analisisnya sendiri.

## Konfigurasi deploy

Kunci tetap di server, tidak melalui FE. Default aktif jika ANTHROPIC_API_KEY sudah tersedia.

| Environment | Default | Fungsi |
|---|---|---|
| ANTHROPIC_API_KEY | kosong | Kunci Anthropic existing |
| ERP_AI_ENABLED | true | false untuk menonaktifkan asisten |
| ERP_AI_MODEL | claude-sonnet-5-5 | Model asisten |
| ADS_AI_MODEL | claude-sonnet-5-5 | Default advisor iklan existing |
| ERP_AI_DAILY_USD | 10 | Batas estimasi harian asisten |
| ERP_AI_TURN_USD | 1.5 | Cadangan/batas per pesan |
| ERP_AI_DAILY_TURNS | 30 | Batas pesan harian, WIB |
| ERP_AI_INPUT_USD_PER_MILLION | 2 untuk Sonnet5.5 | Tarif estimasi token masuk |
| ERP_AI_OUTPUT_USD_PER_MILLION | 10 untuk Sonnet5.5 | Tarif estimasi token keluar |

Model custom membutuhkan kedua tarif estimasi eksplisit. RATES juga mengenali Sonnet4.6 dan Opus5.5. Tarif Sonnet5.5 diverifikasi pada dokumentasi resmi Claude Platform, 9 Oktober 2026. ENV model existing tetap dipertahankan jika deployment sudah mengaturnya. Konfigurasi biaya invalid menonaktifkan enqueue, tidak mematikan import aplikasi.

Startup create_all menambahkan tabel baru mpe_ai_conversations, mpe_ai_turns, mpe_ai_tool_receipts, mpe_ai_daily_budget, mpe_ai_worker_lease. Tidak mengubah stok gudang atau skema tenant lain. Banyak proses server tetap berbagi antrean/lease pada DB ERP yang sama.

## Validasi

Tests terarah menggunakan Anthropic/Shopee mock; tidak memanggil layanan berbayar atau mengubah toko produksi. PostgreSQL nyata menguji reservasi bersamaan, dedup operasi dan hanya satu worker mengklaim antrean. Browser 390/1440px memeriksa mode, kebutuhan toko, antrean→jawaban, payload satu kali, catatan tindakan, overflow dan akses owner/staf. CI menjalankan lint/test/build dan integrasi PostgreSQL sebelum merge. Kelayakan akun/API live dan deploy DO perlu diperiksa dari deployment; environment pengerjaan tidak memiliki secret/platform access itu.

### Penyajian hasil

Biaya UI asisten dikonversi memakai `KURS_USD_IDR` (default 16000; konfigurasi tidak valid kembali ke default). Batas/reservasi dan pembukuan biaya tetap USD. Jawaban yang mencapai batas diberi status `partial`, mempertahankan teks tersedia, dan tidak menjalankan tool dari respons terpotong. Tidak ada panggilan lanjutan otomatis yang menambah biaya. `ERP_AI_MAX_OUTPUT_TOKENS` default 4000, dibatasi 1000–8000; seluruh panggilan tetap tunduk pada batas biaya pesan. Ringkasan omzet menjelaskan tahap yang dihitung dan memisahkan batal/belum bayar. UI merender paragraf, daftar, dan penekanan Markdown sebagai teks React tanpa HTML mentah.
