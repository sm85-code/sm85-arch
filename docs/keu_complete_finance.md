# Buku besar, piutang dan stok FIFO BUMI Lestari

Implementasi hanya menggunakan tenant BUMI Lestari dan tabel `keu_*`. Checkout, payload, webhook, callback dan halaman pembayaran iPaymu tidak berubah. Penarikan Store/ERP tetap baca-saja; tidak ada pembaruan status ke sistem sumber.

## Penerapan

1. Deploy backend dengan `keu_complete_finance.sql` dan frontend dari PR yang sama. Startup `ensure_keu_schema()` menerapkan DDL tambahan, tanpa memposting histori. SQL manual membutuhkan migrasi `keu_unpost.sql` yang sudah diterapkan sebelumnya, dan dijalankan dalam transaksi pada database tenant BUMI Lestari.
2. Owner yang telah mengganti password membuka **Pengaturan → Buku besar & migrasi histori**, lalu mengetik `AKTIFKAN-BUKU-KEU` dan memilih **Aktifkan & Migrasikan Histori**.
3. Aktivasi berjalan atomik: semua histori keu yang valid diposting menurut tanggal asli. Saldo awal kas/bank menjadi saldo sebelum transaksi pertama dan lawannya modal. Histori batal tidak masuk laporan. Periode lama yang sudah ditutup dapat dimigrasikan khusus pada transaksi aktivasi owner, tetapi posting/unpost berikutnya tetap menolak periode tertutup.
4. Jika settlement tidak memiliki alokasi yang dapat direkonsiliasi, bruto melebihi piutang, atau data lain tidak valid, seluruh aktivasi dibatalkan. Perbaiki data sumber internal, kemudian ulangi aktivasi; tidak ada migrasi setengah jalan.
5. Setelah aktif, backfill yang lebih awal daripada tanggal histori pertama saat aktivasi ditolak agar saldo awal tidak diam-diam bergeser. Koreksi kebijakan tanggal awal memerlukan migrasi terencana, bukan perubahan langsung jurnal.

## Kas dan jurnal

**Transfer / Mutasi Kas** mendebet tujuan dan mengkredit asal dalam satu jurnal. Biaya bank adalah pengeluaran tambahan dari asal dan beban administrasi bank. Pokok transfer tidak menambah pendapatan dan neto arus mutasi antar akun nol. Referensi yang sama dengan data sama tidak menggandakan jurnal; referensi berbeda-data ditolak, referensi dibatalkan tidak dihidupkan kembali.

**Buku Kas per Akun** menampilkan kedua sisi transfer dan saldo berjalan berdasarkan seluruh jurnal sebelumnya, walaupun tampilan difilter tanggal. **Jurnal Manual / Koreksi / Modal** mendukung aset/kewajiban/ekuitas/pendapatan/beban umum dengan jumlah debet sama dengan kredit; piutang, vendor dan stok menggunakan modul khusus.

Kategori kas bawaan memakai konstanta aktual `kategori_core.py`: setoran modal dan distribusi/prive ke ekuitas dengan arus pendanaan; langganan dan biaya marketplace ke beban khusus; produksi/pembelian manual ke HPP manual. Kategori lain memakai pemasukan lain atau beban operasional sampai pemetaan eksplisit dibuat di Pengaturan. Pemetaan baru berlaku ke posting berikutnya; jurnal historis tidak berubah. Pencatatan manual yang secara bisnis menduplikasi transaksi sumber tidak dapat dideduplicasi dari nama kategori saja: owner harus merekonsiliasi dan membatalkan duplikasi secara eksplisit.

## Piutang dan settlement

Status sumber ERP `shipped` / Store `dikirim` membentuk piutang pengiriman dan pendapatan. Status `completed` / `selesai` memindahkan pengiriman ke escrow menggunakan tanggal `sumber_updated_at` dalam zona Asia/Jakarta. Rincian dan audit mencatat tanggal pesanan asli, timestamp sumber, tanggal jurnal, dan provenance. Penarikan pertama/backfill memakai tanggal pesanan asli dan tetap draf internal untuk pemetaan/produksi.

Settlement teralokasi melunasi escrow sebesar bruto, menambah kas sebesar neto, dan mencatat potongan sebagai beban marketplace. Bruto dibagikan proporsional menurut alokasi neto; alokasi neto nol memakai total sumber pesanan. Selisih sen diberikan ke alokasi terakhir sehingga jurnal tetap tepat seimbang. Settlement dengan bruto melebihi piutang ditolak.

Store belum menyediakan sumber pencairan otomatis yang dapat diasumsikan. **Settlement manual** untuk saluran Store mewajibkan nomor referensi bank/tautan dokumen melalui `/keu/settlement/manual-bukti`; dokumen dan pembuat disimpan di rincian/audit. Ini pencatatan internal, bukan perubahan alur pembayaran Store. Pilih item saluran yang sama, alokasikan neto dan posting ke akun penerima. Pembayaran yang sudah dicairkan harus dibatalkan terlebih dahulu sebelum membatalkan piutang penjualan.

## Persediaan dan utang vendor

**Pembelian / Produksi Stok Ready** untuk non-order membentuk persediaan per batch, dengan lawan kas/bank atau utang vendor. **Penuhi Pesanan dari Stok Ready** mengurangi batch FIFO berdasarkan tanggal, waktu pencatatan dan ID batch sebagai pemecah seri. Setiap pemakaian mencatat ID batch, kuantitas dan biaya persisnya, serta jurnal HPP lawan persediaan. Alokasi gabungan stok dan vendor tidak boleh melebihi kuantitas item.

Pembulatan biaya memakai dua desimal; pemakaian terakhir mengambil seluruh sisa biaya batch. Nilai pemakaian yang membulat menjadi nol tetap mempunyai jejak stok. Backdating yang mengubah urutan FIFO ditolak; batalkan pemakaian berikutnya terlebih dahulu. Penerimaan yang sudah dipakai tidak dapat dibatalkan sebelum pemakaiannya. Utang yang sudah dibayar tidak dapat dibatalkan sebelum pembayaran; pembayaran vendor tidak boleh melebihi utang.

## Laporan dan koreksi

**Laporan Keuangan** menyediakan Laba Rugi, Posisi Keuangan dan Arus Kas dengan tanggal awal/akhir. Laba/rugi dan arus kas merupakan pergerakan dalam rentang; neraca adalah saldo kumulatif sampai tanggal akhir. SHU periode sebelumnya dibawa ke ekuitas dari jurnal pendapatan/beban aktual, tanpa nilai pengimbang buatan. Selisih neraca atau rekonsiliasi arus kas tidak nol menyebabkan penolakan penerbitan laporan.

**Batalkan Post** memerlukan alasan, mempertahankan nominal asli dan audit pembatalan, serta mengecualikan seluruh jurnal dibatalkan dari ketiga laporan/buku kas. Unpost jurnal kas/settlement juga membatalkan catatan keu asal. Pembukaan tidak dapat di-unpost; gunakan jurnal koreksi modal seimbang. Reset keuangan terdahulu tetap khusus keu, mencakup buku/jurnal/stok baru, dan mempertahankan master bagan akun serta pemetaan kategori.

## File dan pemeriksaan

Backend: `models_keu_finance.py`, `schemas_keu_finance.py`, `keu_ledger.py`, `keu_accounting.py`, `keu_inventory.py`, `keu_finance_router.py`, `keu_finance_migration.py`, `scripts/generate_keu_finance_migration.py`, `keu_complete_finance.sql`. Integrasi melalui router/services/import/sync/reset keu yang sudah ada.

Frontend: `BookSettings.tsx`, `LedgerFinance.tsx`, `Inventory.tsx`, `Receivables.tsx`, `Reports.tsx`, dan halaman/nav/tipe keu yang diperbarui. Guard admin/owner dan wajib ganti password tetap digunakan.

Tes: seluruh suite backend, lint Ruff, integration PostgreSQL asli (DDL, trigger, concurrency, histori/periode tertutup, rollback, FIFO, unpost), unit frontend, TypeScript, build produksi dan Playwright desktop/mobile. PostgreSQL pengujian memakai schema terisolasi dan pool terbatas; tidak menjalankan migrasi atau reset pada Neon live.
