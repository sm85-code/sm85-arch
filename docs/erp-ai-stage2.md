# Persiapan Asisten AI ERP — tahap 2

## Implementasi tersedia

- GET `/akun/{akun_id}/performa-toko`: admin-only, account-scoped, read-only. Mengambil metric_list dari Account Health dan mempertahankan current_period/last_period/unit/target; tidak membuat skor sendiri. overall_performance diteruskan tanpa menebak arti rating enum. Bukan integrasi riwayat poin penalti.
- GET `/katalog-shopee/{katalog_id}/statistik`: admin-only. Identitas akun/item berasal dari katalog server, bukan body browser. Mengambil sale, views, likes, rating_star, comment_count. Views 30 hari; sale kumulatif. Null/missing tidak diubah menjadi nol.
- PATCH `/akun/{akun_id}/promosi/{promosi_id}`: akses pengelolaan promosi existing tetap. Update nama/mulai/selesai, hanya field yang diberikan. Memeriksa detail/status terbaru, melarang perubahan start pada ongoing dan start yang lebih awal, minimal durasi satu jam; Shopee tetap memvalidasi aturan provider. Konfirmasi discount_id sebelum menyatakan sukses.
- FE: menu Performa Toko khusus admin, statistik dalam detail katalog khusus admin, dialog Ubah Nama/Jadwal pada detail promosi dengan tanggal WIB dan konfirmasi. Jadwal dengan detik tidak ikut terpotong saat hanya nama berubah. Label iklan otomatis tidak lagi menganggap semua auto bidding adalah Shop GMV Max.

Tidak ada perubahan skema DB, kepemilikan stok, permission halaman existing, atau tindakan marketplace produksi. Belum ada alat/percakapan AI yang diaktifkan.

## Acuan dan keterbatasan

api-docs commit 087884e: guide-16 AccountHealth; guide-221 product.get_item_extra_info; update_discount.md kontrak dan permission Seller In House. Contoh respons/field insights diverifikasi terhadap SDK generated yang telah tersimpan di `/workspace/scratch/shopee-generated`, yang menyatakan dihasilkan dari dokumentasi Shopee. SDK adalah sumber pendukung, bukan verifikasi akun produksi. Endpoint resmi tambahan gagal diakses (403 pada Jina dan browser). Kesalahan izin provider diteruskan melalui penanganan error Shopee existing; tidak diterjemahkan sebagai data nol.

Belum lengkap: edit kategori/atribut/foto/tier/model/berat/dimensi/preorder produk existing; typed-contract GMV Max; riwayat poin penalti; skor kualitas listing resmi; pendaftaran kampanye resmi. Publikasi produk baru tetap tersedia dan bukan pengganti edit existing. Jangan mengaktifkan alat eksekusi AI untuk kemampuan belum lengkap. Tidak ada bukti fitur-fitur itu dilarang hanya karena tipe akun; chat/ads memiliki whitelist/eligibility tersendiri.

Dokumen yang perlu ditambahkan ke api-docs: detail account_health.get_shop_performance, product.get_item_extra_info, product.update_item dan update tier/model, account_health.get_penalty_point_history, GMS eligibility/create/edit/performance; FAQ Chat whitelist243. Spesifikasi skor kualitas listing/nomination belum ditemukan, jangan mengarang endpoint. Setelah tersedia, lengkapi fungsi tersebut sebelum tahap akses AI.

## Validasi

18 tes backend terarah (insights/promotion + existing workflow), 22 tes FE nav/roles/catalogue; Ruff dan TypeScript/build lolos. Browser fixture 390/1440 memverifikasi nilai null/zero, tidak ada horizontal overflow, dialog ongoing menonaktifkan mulai, update nama tidak mengirim jadwal, tidak ada pageerror. Tidak memanggil Shopee atau Claude nyata.
