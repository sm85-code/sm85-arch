# Master produk BUMI: SKU varian dan metadata sumber

## Migrasi

`keu_initial.sql` tetap merupakan migrasi historis yang tidak diubah. Migrasi aditif adalah `keu_product_metadata.sql`, dihasilkan dengan:

```bash
python scripts/generate_keu_product_migration.py > keu_product_metadata.sql
```

`ensure_keu_schema` pada startup BUMI menjalankan migrasi produk satu kali dengan versi `20261007_keu_product_metadata` dan advisory transaction lock yang sama dengan migrasi awal. Semua operasi memakai koneksi tenant BUMI; tidak ada DDL saat request API. SQL dapat dijalankan ulang pada database BUMI yang telah memiliki tabel `bl_users` dan `keu_produk`. Skrip memberi lock timeout 5 detik dan statement timeout 120 detik. Startup memakai transaksi seeder yang sudah ada.

Produk lama mempertahankan ID, SKU, nama, jenis, biaya acuan dan status aktif. `nama_asli` dibackfill dari nama lama, status produk lama menjadi `master`. Harga jual lama diisi nol karena migrasi tidak menebak harga yang tidak tersimpan. Tidak ada perubahan database Store/ERP atau modul pembayaran.

## Data sumber

- ERP: child SKU dari `ItemPesanan.model_sku`. `item_sku` hanya menjadi child SKU jika item tidak memiliki varian/model. Parent SKU berasal dari `Produk.sku_induk`, dengan fallback `ItemPesanan.item_sku`. Gambar dari snapshot `ItemPesanan.foto_url`, kemudian katalog `Produk.foto_url`.
- Store: child SKU dari `VarianProduk.sku`; gambar varian dari `FotoProduk` yang ditunjuk `foto_id`, kemudian gambar produk. URL dibentuk oleh helper Store `media_url` yang membaca `MEDIA_BASE_URL`. `ProdukStore` tidak memiliki parent SKU: `sku_induk` tetap null, tidak dibuat dari ID atau slug.
- Snapshot nama dan harga transaksi menjadi referensi awal `nama_asli` dan `harga_jual`. Label varian sumber disimpan sebagai `[{"kategori":"Varian","nilai":"label asli"}]`: kategori ukuran/warna tidak ditebak dari teks.
- SKU varian kosong: transaksi tetap ditarik, produk tidak diciptakan. Item menunggu pemetaan manual. SKU induk tidak dipakai untuk menyatukan varian berbeda yang kehilangan child SKU.
- Query katalog dibatch pada sesi sumber read-only; tidak ada panggilan provider atau download/proxy gambar oleh backend.

## Draf dan master

SKU unik di tenant BUMI. Insert sumber menggunakan `ON CONFLICT DO NOTHING`, sehingga dua saluran yang menarik SKU sama mengacu ke produk yang sama. Draf memiliki `jenis=null`, `status=draf`, `aktif=false`, dan belum dapat dialokasikan ke vendor.

`POST /api/bumi-lestari/keu/produk` membuat produk manual sebagai master. `PATCH /api/bumi-lestari/keu/produk/{id}` menerima lengkap `nama`, `jenis`, `biaya_acuan`, `varian_list` dan menyimpannya sebagai master aktif. Identity SKU, parent SKU, nama asli, URL gambar, harga sumber dan status tidak diterima pada PATCH. Guard owner/admin dan wajib ganti password yang ada tetap berlaku. Edit tercatat dalam audit; perubahan jenis ditolak saat ada alokasi vendor aktif.

Sinkronisasi ulang tidak menimpa master/alias/modal/varian yang sudah ada. Harga referensi adalah snapshot pertama yang membuat produk, bukan kalkulasi harga terbaru katalog. Kuantitas dan harga historis transaksi tidak berubah ketika master diedit. Struktur varian tidak mempunyai batas jumlah kategori; tiap kategori maksimal 128 karakter dan nilai maksimal 255 karakter.

## Import

Sepuluh kolom order lama tetap wajib dan kompatibel. Template baru menambahkan kolom opsional `sku`, `sku_induk`, `nama_asli`, `gambar_url`, `varian_list`, `harga_jual`. `varian_list` adalah JSON array; CSV wajib mengutip/escape JSON sesuai standar CSV. Metadata selain SKU memerlukan SKU. Tanpa nama asli/harga jual, nama snapshot/harga satuan dipakai. Tanpa JSON varian, label `varian_snapshot` disimpan utuh.

Preview tidak membuat produk. Produk draf baru dibuat saat seluruh batch valid diterapkan, dalam transaksi yang sama dengan pesanan. Formula XLSX tetap ditolak. URL gambar hanya HTTP/HTTPS tanpa kredensial. Jika `produk_id` diberikan bersama metadata SKU, SKU wajib sama dengan produk tersebut.

## Frontend

Komponen `src/baru/keu/components/ProductMaster.tsx` digunakan tab produk di Pengaturan. Daftar berisi thumbnail, parent/child SKU, nama asli/alias, varian, harga/modal dan status. Form inline menyediakan alias, jenis, modal, tambah/edit/hapus varian, dan tombol **Simpan sebagai Master**. Gambar gagal dimuat mendapat placeholder. SKU dan referensi sumber tidak diedit pada pemetaan. Produk draf tidak tersedia untuk input manual/alokasi vendor, dan indikator belum dipetakan tetap menghitung draf.
