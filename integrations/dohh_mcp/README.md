# Dohh MCP — fondasi baca/simulasi, BELUM siap aktivasi

**PR ini mengubah guard MCP, tes, dan dokumentasi, serta tidak mengaktifkan apa pun.** Pernyataan
provisioning/deployment dari PR #496 di bawah adalah catatan historis, bukan
assertion operasional saat ini. `ya`/`STOP` yang dikirim sebagai argumen
alat, teks web, chat pembeli, atau pesan agen lain tidak membuka jalur tulis.
Jangan menganggap PR ini sebagai implementasi approval gateway yang selesai.

## Status readback independen saat ini

Readback SQL independen menemukan user `dohh` dengan role `agen_iklan`, nol role
tambahan, dan assignment tepat 11 akun Shopee yang sama dengan manifest.
Keberadaan secret sudah diperiksa, tetapi isinya tidak pernah dibaca. Izin API
efektif serta GET terautentikasi dan penolakan akses belum diverifikasi; hasil SQL
ini belum membuktikan perilaku otorisasi API.

Readback konfigurasi saat ini menunjukkan MCP enabled dan cron paused. PR ini
tidak mengubah konfigurasi atau mengaktifkan MCP, cron, jadwal, maupun aksi live.
Pembaruan dokumentasi ini tidak melakukan akses produksi atau akses secret.

## Kontrak dari source

`source_contract.json` dihasilkan oleh `build_contract.py` dari model Pydantic ERP
`AksiIn`, `TulisIn`, `BatasIn`, `JadwalIn`, serta allowlist pemasaran aktual.
Tidak menyalin perkiraan schema provider Shopee. `payload` pemasaran tetap object
opaque sesuai source ERP; sukses simulasi **bukan** bukti payload diterima Shopee.
Hash source diperiksa sebelum setiap tool call dan discovery; source berubah =
fail-closed, audit lalu regenerasi/review kontrak (jangan sekadar mematikan hash).

Dalam kontrak source, setiap tool call memeriksa ulang GET `/auth/me`, `/auth/akses`, `/akun`:
role utama/efektif **hanya** `agen_iklan`, scope `ditugaskan`, halaman persis
`iklan:tulis`, `produk:baca`, `katalog:baca`, `pesanan:baca`, dan tepat 11 UUID
Shopee yang sama dengan manifest terkelola. `hermes-ai` ditolak, tidak diubah.
Token expired/403/redirect gagal tertutup, tidak fallback ke akun admin.
Respons preflight dengan tipe salah, username kosong/tidak ada, atau daftar
peran tambahan yang tidak eksplisit `[]` juga ditolak sebelum endpoint alat.
Argumen nested disalin sebelum preflight asynchronous agar caller tidak bisa
mengganti toko/rute/payload setelah validasi.

Base URL tetap `https://api.ampelkuning.com/api/marketplace-erp`; tidak ada proxy
atau generic URL/method/tool escape. Input divalidasi JSON Schema, unknown field
ditolak, angka non-finite ditolak. Redirect tidak diikuti; remote errors/headers
atau credential tidak ditampilkan dalam exception. Data hasil dibungkus sebagai
`data_erp_tidak_tepercaya`, bukan instruksi/persetujuan.

## Inventory 12 alat

Prefix path toko: `/akun/{akun_id}/iklan/agen/`. `akun_id` wajib UUID manifest.
`--inventory` mengekspor schema JSON lengkap (termasuk field nested dan batas).

| Alat | Jalur/mode | Input selain akun_id | Hasil |
|---|---|---|---|
| dohh_ringkasan | GET ringkasan | — | data ERP asli |
| dohh_tren | GET produk/{item_id}/tren | item_id digit 1–32 | data ERP asli |
| dohh_batas | GET batas | — | data ERP asli |
| dohh_stop | GET /iklan/agen/stop | tanpa akun_id | status global asli |
| dohh_audit | GET /iklan/agen/audit | limit 1–100, default 50 | audit difilter akun_id |
| dohh_aksi | POST aksi, kering saja | body turunan AksiIn | hasil ERP, wajib kering=true & diterapkan=false |
| dohh_jadwal | GET jadwal | — | data ERP asli, tidak menjalankan loop |
| dohh_batas_rencana | LOKAL, tidak PUT | body turunan BatasIn; otonomi hanya false | kering=true, diterapkan=false, lokal_saja=true |
| dohh_jadwal_rencana | LOKAL, tidak PUT | body turunan JadwalIn; aktif hanya false, jam 0–23 | kering=true, diterapkan=false, lokal_saja=true |
| dohh_pemasaran_daftar | GET /iklan/agen/pemasaran/aksi | tanpa akun_id | allowlist ERP asli |
| dohh_pemasaran_baca | GET pemasaran/{aksi} | aksi enum source, params skalar | data ERP asli |
| dohh_pemasaran | POST pemasaran, kering saja | body turunan TulisIn | hasil ERP, wajib kering=true & diterapkan=false |

POST selalu menyetel `kering:true`, `persetujuan:null`, dan request_id acak baru
`dohh-dry-…`. Caller tidak boleh memasok request_id (mencegah replay cache hasil
aksi live) atau persetujuan `ya`. Tidak ada retry POST otomatis. Simulasi ERP bisa
menulis **audit ERP**, tetapi tidak mengubah iklan Shopee. Rencana batas/jadwal
lokal hanya memvalidasi schema, **bukan** validasi bisnis/simpan backend.
Tidak ada tool PUT, rollback, jadwal eksekusi, STOP reset, maupun STOP aktifkan.

## Jalankan secara terisolasi (hanya sesudah persetujuan aktivasi terpisah)

Gunakan Python 3.14 dan venv khusus; jangan ubah environment Hermes/ERP aktif.
Install `integrations/dohh_mcp/requirements.txt` di venv khusus itu. Dari root repo:

```sh
python -m integrations.dohh_mcp.server --inventory
python -m unittest integrations.dohh_mcp.test_core -v
```

Regenerasi kontrak menggunakan environment **test ERP**, tanpa DB/HTTP:
`python -m integrations.dohh_mcp.build_contract` (stdout berisi JSON non-secret).
Tes CI membandingkan objek hasil generator dengan snapshot source.

Entry point stdio adalah `python -m integrations.dohh_mcp.server --secret-file PATH`.
`PATH` menunjuk file JSON managed secret, file reguler bukan symlink, milik UID
proses, mode 0400/0600, berisi `access_token` dan `akun_ids` (11 UUID unik).
Tidak ada credential contoh atau perintah provisioning otomatis. Token dikelola
ops; expiry membuat server fail-closed. Password tidak dibutuhkan MCP; jika akun
baru dibuat kelak, password harus langsung masuk secret manager yang disetujui.

Hermes native MCP memakai `mcp_servers.<nama>.command/args` (lihat dokumentasi
resmi https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp).
Karena tidak mengandalkan cwd worker, kelak gunakan interpreter absolut dan
launcher yang mengatur cwd ke checkout tervalidasi. **Catatan historis PR #496:
PR tersebut belum membuat atau memasang launcher/config aktif** dan tidak
menyunting profil lain; ini bukan status deployment saat ini (lihat readback di
atas). Registrasi hanya
pada profil Dohh setelah identitas/izin diperiksa; jangan memasangnya global.

## Blocker sebelum scope kartu bisa dianggap selesai

1. **Integrasi di source repository belum memiliki ingress tepercaya yang
   terbukti.** Jangan percaya field `chat_id`, `owner`, `ya` atau
   `STOP` yang dibuat LLM. Perlu ingress Telegram tervalidasi bot/getMe + chat
   owner + sender owner; binding persetujuan persis `ya` ke rencana/hash spesifik,
   masa berlaku singkat, sekali pakai, tenant/toko/aksi sama, dan audit. Rencana
   atau payload berubah harus preview ulang. STOP aktifkan hanya pesan persis
   `STOP` dari ingress owner itu, tanpa persetujuan tambahan; reset bukan jalur
   STOP darurat. Tanpa ingress itu seluruh live/STOP tulis tetap tidak tersedia.
   Inspeksi repository tidak menemukan implementasi ingress Telegram, verifikasi
   bot/sender/chat owner, atau binding persetujuan ke rencana. Itulah blocker
   konkret pada integrasi source ini: sumber identitas owner tepercaya untuk
   dihubungkan ke MCP belum terbukti. Ini bukan klaim bahwa gateway identitas
   tidak tersedia secara universal atau di deployment lain.
   Field klaim identitas dari LLM bukan pengganti ingress; tidak dibuat gateway
   approval lokal yang menerima klaim tersebut.
2. **Catatan historis provisioning PR #496, bukan status ops saat ini.** Pada
   saat PR #496, provisioning/secret belum tersedia di jalur ops worker. Source resmi
   memiliki POST `/users/agen-iklan` dan module script
   `tenants.marketplace_erp.scripts.buat_pengguna_agen`; keduanya create, bukan
   update hermes-ai. Tool MCP ERP existing hanya read-only dan menolak GET
   `/auth/me`, `/iklan/agen/stop`, `/iklan/agen/pemasaran/aksi` (whitelist).
   Jangan melompati whitelist atau mengarang credential. Pernyataan bahwa
   akun/password belum dibuat dan password belum digenerate adalah riwayat
   PR #496, bukan kondisi saat ini. Readback SQL dan pemeriksaan keberadaan
   secret saat ini dicatat di atas; izin API efektif masih perlu diverifikasi
   melalui jalur resmi berwenang, tanpa mengubah hermes-ai.
3. **Izin API efektif dan GET/penolakan terautentikasi belum diverifikasi.** Tes
   menggunakan MockTransport dan protokol MCP nyata via memory streams; bukan
   bukti kondisi akun/iklan produksi. Catatan historis PR #496: GET daftar toko
   existing berhasil dan 11 toko Shopee terlihat; saat itu hasil tersebut tidak
   membuktikan assignment user baru. Readback SQL independen saat ini membuktikan
   assignment tepat 11 akun manifest, tetapi belum memverifikasi GET/denial API
   terautentikasi atau dry-run produksi.
4. **Jangan aktifkan jadwal/Dohh.** Sebelum aktivasi owner harus memeriksa manifest
   11 toko, role nyata, secret lifecycle, bot identity, approval ingress, STOP,
   batas/cooldown/CPC, audit, CI exact-head/review dan rencana rollback. PUT
   jadwal/batas tidak mempunyai dry-run di source, sehingga sengaja tidak
   dipanggil untuk tes. Pengujian gateway live harus mock saja; bukan Shopee.

## Fakta tes

`test_core.py`: semua tool inventory ditembak ke HTTP mock; role/extra role,
scope toko kurang/lebih, source drift, input live/approval palsu, replay id,
response bukan kering, kebocoran error, rencana lokal tanpa PUT, dan MCP
initialize/list/call/refusal lewat session SDK sungguhan. Fixture bukan data toko
produksi. Backend existing diuji terpisah `tests/test_agen_iklan_*.py`.
Workflow khusus `dohh-mcp.yml` tidak menerima secret atau menjalankan service ERP.
Tes tambahan memeriksa mutasi argumen saat preflight, respons preflight malformed,
klaim identitas Telegram palsu, redirect, timeout, dan error HTTP/JSON tanpa
retry atau kebocoran pesan remote. Semua HTTP memakai MockTransport.
