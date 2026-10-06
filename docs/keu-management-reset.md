# Pengelolaan master dan reset keu

## Lingkup reset yang disetujui

Reset hanya mencakup tabel operasional keu: `keu_pesanan`, `keu_item`, `keu_alokasi_vendor`, `keu_settlement`, `keu_alokasi_settlement`, `keu_transaksi`, `keu_impor`, `keu_masukan`, dan `keu_cursor`.

Master `keu_produk`, `keu_vendor`, `keu_pelanggan`, `keu_akun`, `keu_saluran` serta konfigurasi kompatibilitas `keu_vendor_slot` dipertahankan. Seluruh tabel pencatatan lama `bl_*`, kategori, pengguna, schema versions dan audit tetap dipertahankan. Saldo awal adalah atribut master akun dan tidak diubah. Ledger kas/bank keu menggunakan `KeuTransaksi`; tidak dibuat tabel jurnal yang tidak ada dalam model saat ini.

Reset tidak dijalankan terhadap database live dalam implementasi ini. Hanya pengguna owner yang sudah mengganti password bawaan dapat mengeksekusinya melalui UI/API; admin tetap dapat mengelola master.

## Konfirmasi dan atomisitas

1. `POST /api/bumi-lestari/keu/reset/pratinjau` mengunci tabel untuk snapshot konsisten, menghitung data dan membuat challenge acak sekali pakai yang terikat owner, dengan expiry 5 menit. Hash token dan fingerprint data disimpan dalam `BlAuditLog`, bukan token/password plaintext.
2. Modal menampilkan jumlah data yang akan dihapus. Pengguna memilih Lanjutkan konfirmasi, mengetik `RESET-KEUANGAN` persis, dan memasukkan password owner.
3. `POST /api/bumi-lestari/keu/reset` memverifikasi role, password, challenge, expiry dan fingerprint. Perubahan data setelah pratinjau menyebabkan 409 dan memerlukan konfirmasi baru.
4. PostgreSQL mengunci sembilan tabel secara eksklusif lalu menjalankan TRUNCATE eksplisit dalam transaksi request. Tidak memakai CASCADE, tidak menonaktifkan trigger, dan tidak menyertakan master. Kesalahan menyebabkan rollback seluruh perubahan. Audit dan penandaan challenge terpakai dilakukan dalam transaksi yang sama.

Perhitungan fingerprint memakai pagination keyset 500 baris agar penggunaan memori terbatas dan tidak meninggalkan portal server-side asyncpg yang dapat menghalangi TRUNCATE. Penguncian memakai timeout lokal transaksi, sesuai koneksi pooled PostgreSQL. Tidak dibuat engine/pool baru.

Body reset lengkap:

```json
{
  "challenge_id": "ID_DARI_PRATINJAU",
  "token": "TOKEN_DARI_PRATINJAU",
  "konfirmasi": "RESET-KEUANGAN",
  "password": "PASSWORD_OWNER"
}
```

Sesudah reset, posisi sinkronisasi dan data impor lama ikut dibersihkan. Penarikan baru dapat mengisi kembali transaksi dari sumber tanpa mengirim perubahan status ke Store/ERP. Master tetap tersedia untuk pemetaan kembali.

## CRUD master

- Produk: POST/PATCH yang sudah ada tetap digunakan; edit alias/varian/modal tidak mengubah identitas SKU sumber. Mengedit master nonaktif tidak mengaktifkannya secara diam-diam. Menyimpan produk draf sebagai master tetap mengaktifkannya.
- Akun, pelanggan dan saluran: PATCH `/{resource}/{id}` menerima schema pembuatan yang sudah ada sebagai body edit lengkap. Nama/kontak boleh diperbaiki. Saldo awal/jenis akun atau identitas sumber saluran yang sudah digunakan tidak boleh diubah; buat master baru atau lepaskan relasi terlebih dahulu.
- Vendor: PATCH `/vendor/{id}` tetap berlaku. DELETE `/vendor/{id}` lama tetap bermakna Nonaktifkan untuk kompatibilitas.
- Semua lima master: DELETE `/master/{resource}/{id}` menghapus permanen hanya jika tidak memiliki FK yang terkait. Relasi historis juga dilindungi; pesanan batal tidak menjadi alasan menghapus foreign key. Jika terkait, response 409 menawarkan Nonaktifkan.
- Soft delete/reaktivasi semua master: PATCH `/master/{resource}/{id}/status` dengan body `{"aktif":false}` atau `{"aktif":true}`. Produk draf tidak boleh langsung diaktifkan sebelum pemetaan. Akun/pelanggan nonaktif dikecualikan dari pilihan transaksi baru; riwayat tetap tersedia.

Prefix seluruh endpoint adalah `/api/bumi-lestari/keu`. Resource hanya `produk`, `akun`, `vendor`, `pelanggan`, atau `saluran`. Auth owner/admin dan guard wajib ganti password tetap berlaku. Semua edit/status/hapus diaudit.

Tidak diperlukan migrasi DDL: reset menggunakan tabel dan audit yang sudah ada. Modul checkout, callback, webhook, payload pembayaran dan halaman iPaymu tidak diubah.
