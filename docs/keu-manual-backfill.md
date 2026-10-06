# Penarikan manual pesanan historis BUMI

## Rentang tanggal

`src/baru/keu/pages/Sync.tsx` menyediakan tanggal awal dan akhir terpisah per saluran dan per entitas. Kedua tanggal inklusif dalam WIB. Backend `keu_sync.pull` memfilter tanggal transaksi Store (`PesananStore.created_at`) atau ERP (`coalesce(Pesanan.dipesan_at, Pesanan.created_at)`). Rentang manual tidak membaca atau memajukan cursor incremental; transaksi lama tetap dapat ditarik walaupun watermark sudah melewatinya. Batch maksimal 100, dengan tombol halaman berikutnya yang mempertahankan rentang. Mengubah tanggal menghapus posisi halaman lama.

## Status internal dan alur kerja

Status asli `dikirim`/`selesai` (Store) dan `shipped`/`completed` (ERP) disimpan sebagai `status_sumber`. Tidak ada filter yang menghilangkan status historis tersebut. `keu_services.create_order` menetapkan status internal pesanan baru secara eksplisit menjadi `draf`. Penarikan ulang/revisi sumber tidak mengulang produksi atau membuka kembali pesanan internal yang telah selesai.

Alur: auto-populate produk draf dari child SKU sumber → konfirmasi master/pemetaan → alokasi vendor di Produksi → perubahan status internal → rekonsiliasi settlement → posting kas. SKU sumber yang kosong tetap menunggu pemetaan manual; tidak diciptakan SKU fiktif. Penarikan ulang menggunakan referensi sumber dan hash revisi, sehingga tidak menggandakan pesanan, item, alokasi atau kas. Revisi harga/kuantitas yang sudah dialokasikan/disettlement tetap meminta rekonsiliasi manual.

Tanggal pesanan mempertahankan tanggal transaksi asli. Settlement ERP mempertahankan `dirilis_at` sebagai tanggal pencairan dalam WIB. Kas hasil posting memakai `tanggal_cair` asli, bukan tanggal saat backfill dilakukan. Periode tutup buku yang sudah terkunci tetap dilindungi; date override tidak membuka periode tersebut.

Store saat ini tidak menyediakan bukti/tanggal pencairan melalui sumber sinkronisasi. Backfill order Store tidak menciptakan settlement atau kas otomatis. Tidak ada perubahan integrasi pembayaran/iPaymu untuk menyediakan data yang belum ada.

## Tanpa efek ke sumber

Sesi Store/ERP hanya digunakan untuk SELECT order, item, katalog dan settlement yang ada. Pada PostgreSQL sesi sumber diawali `SET TRANSACTION READ ONLY`. Tidak ada panggilan API provider, callback, webhook, perubahan status atau commit data sumber. Semua pencatatan, audit dan tahap produksi/keuangan terjadi di database tenant BUMI.

`tests/test_keu_backfill.py` memverifikasi empat kombinasi sumber/status historis, override cursor, pemetaan/alokasi, penarikan ulang setelah produksi selesai dan revisi sumber baru, tanggal order/kas asli, pembatasan settlement Store, tidak ada DML pada sesi sumber, dan tidak ada request HTTP provider. Tes Playwright menguji input rentang Store, request yang dikirim, penjelasan status serta navigasi ke master produk; tes rentang/pagination ERP yang ada tetap dijalankan.
