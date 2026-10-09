# Persiapan Asisten AI ERP — tahap 2

## Pembaruan T2 — dokumentasi dc31614

Implementasi mengacu pada dokumentasi API Shopee dalam repo api-docs commit dc31614.

- Diagnosis kualitas listing resmi melalui POST get_item_content_diagnosis_result tersedia dalam detail katalog (admin). Level kualitas mengikuti enum Shopee, tidak diubah menjadi skor buatan. Respons level Excellent yang tidak memiliki unfinished_task diterima.
- Riwayat poin penalti kuartal berjalan tersedia di Performa Toko, dengan pagination dan nilai poin sebelum/sesudah.
- Dialog edit produk existing: judul, deskripsi, kategori/atribut, merek, foto, berat/dimensi induk dan preorder; SKU, berat dan preorder per varian; nama pilihan varian dengan identitas/index/foto pilihan dipertahankan. Kontrak backend juga mendukung dimensi dan GTIN per varian. Perubahan fisik induk membutuhkan konfirmasi menimpa seluruh model.
- Mutasi produk membaca identitas provider terbaru sebelum menulis, hanya mengirim field perubahan, dan memperbarui snapshot katalog setelah konfirmasi. Jika refresh snapshot gagal setelah write berhasil, hasil tetap menyatakan write berhasil dengan peringatan sinkronisasi; tidak menyuruh pengguna mengulang write.
- Shop GMV Max memiliki kontrak terpisah: cek eligibility, create, edit budget/jadwal/ROAS, pause/resume/start, tambah/hapus produk, laporan kampanye dan per produk. Tanggal provider DD-MM-YYYY; laporan memakai POST sesuai dokumentasi, batas periode menggunakan bulan kalender. Create ditolak jika eligibility false. Error tidak dicoba ulang otomatis.
- Seluruh endpoint manajemen baru admin-only dan scoped ke akun katalog/toko. Tidak ada proxy endpoint/body bebas, perubahan kepemilikan stok, atau pemanggilan provider nyata saat validasi.

Pendaftaran kampanye resmi Shopee dikeluarkan dari T2 sesuai instruksi pengguna. Screenshot ShopFlashSale/BundleDeal belum merupakan spesifikasi request/response, sehingga integrasi baru untuk keduanya tidak dibuat. Pengunggahan video dan pemilihan template size chart memerlukan dokumentasi pendukung; belum diekspos sebagai kemampuan AI. T3 (eksekusi durable, budget/token/quota) dan percakapan/tool AI belum diaktifkan. Kelayakan/permission akun produksi harus diperiksa melalui provider; ketersediaan dokumentasi bukan bukti izin akun.

Validasi tambahan: 24 tes backend terarah management/insights/workflows lulus, TypeScript dan build FE lulus. Pengujian menggunakan mock, tidak mengubah toko marketplace produksi. Release/PR belum dibuat pada tahap ini.

Fungsi tranche awal tetap tersedia: statistik produk (views 30 hari dan sale kumulatif), performa toko, serta edit nama/jadwal promosi dengan pembatasan status. Null statistik dipertahankan dan tidak dianggap nol.

Browser fixture tambahan 390/1440 px: eligibility false menonaktifkan tombol create, tidak ada horizontal overflow/pageerror, dan tidak ada request write. Ruff/Oxlint lulus.
