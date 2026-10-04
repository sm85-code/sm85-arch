# Spek Produk & Teknis — Marketplace ERP (sm85-code)

| | |
|---|---|
| **Kode tenant** | `tenants/marketplace_erp` di repo `sm85-arch` |
| **Prefix API** | `/api/marketplace-erp` |
| **DB** | Postgres terpisah via `DATABASE_URL_MARKETPLACE_ERP` |
| **Status sekarang** | **Tahap 1 (fondasi)** sudah ada: auth owner, akun toko, SKU induk, listing |
| **Audiens** | Owner/ops marketplace Ampel Kuning + engineer sm85-code |
| **Versi spek** | 0.1 — draft desain lengkap (bukan implementasi) |
| **Tanggal** | 2026-09-29 (WIB) |

---

## 1. Visi & non-goals

### Visi

**Satu panel operasional** untuk mengelola banyak toko marketplace (multi-shop, multi-platform) dengan:

- **SKU induk terpusat** — satu baris produk fisik → banyak listing di banyak toko/platform.
- **Stok & pesanan sinkron** — update stok sekali, dorong ke semua listing aktif; pesanan masuk ke inbox tunggal.
- **Settlement & laporan** — rekonsiliasi pencairan platform vs penjualan lokal.

Target pengguna: seller yang punya **banyak toko Shopee** (via sub-akun) plus Lazada / Blibli / TikTok Shop, bukan pembeli akhir.

### Non-goals (eksplisit)

| Bukan ini | Alasan |
|---|---|
| **Toko web B2C Ampel Kuning** (`tenants/toko` storefront, iPaymu, Biteship, katalog publik) | Produk & DB terpisah total. Marketplace ERP **tidak** menggantikan toko web. |
| Modul `tenants/toko/modules/erp` sebagai “sumber kebenaran” jangka panjang | Itu prototipe lama di DB toko. Marketplace ERP adalah tenant baru yang berdiri sendiri. |
| Storefront / checkout / SEO / domain publik untuk pembeli | Tidak ada halaman belanja. Hanya admin/ops. |
| Multi-merchant SaaS publik (banyak brand asing berlangganan) | Scope awal = **satu bisnis / satu organisasi** (Ampel Kuning). Multi-tenant SaaS = keputusan terpisah. |
| Enkripsi at-rest penuh / HSM di Tahap 1 | DB sudah isolated; token disimpan kolom biasa (sama pola toko-erp). Hardening secrets = fase berikutnya (lihat §8). |
| Live ads bidding otomatis di M0–M2 | Modul `iklan` baru masuk roadmap lanjut. |

### Prinsip desain (dari kode yang sudah ada)

1. **Produk (SKU induk) = SSOT** fisik; `ProdukListing` = mapping ke `(platform, id_eksternal)` per `AkunMarketplace`.
2. **Satu platform ≠ satu toko** — banyak baris `AkunMarketplace` per platform (mis. 10 toko Shopee).
3. **JWT + cookie sendiri** (`marketplace_erp_token`, `aud/iss` tenant `marketplace_erp`) — tidak bercampur dengan siabumdes / madrasah / toko.
4. **Modul lanjutan hanya menambah tabel** yang mereferensi `Produk` / `AkunMarketplace` by id — tidak merombak fondasi.

---

## 2. Personas & roles

### Personas

| Persona | Kebutuhan utama |
|---|---|
| **Owner** | Hubungkan toko, kelola SKU/listing, lihat stok & laba, atur staff, putuskan harga override per platform. |
| **Staff ops / CS** | Inbox pesanan & chat, proses kirim, jawab buyer, sesuaikan status (nanti scoped per akun toko). |
| **Staff gudang** | Lihat antrean to-ship, cetak label, konfirmasi keluar barang, adjustment stok. |
| **Finance** | Settlement, fee platform, rekonsiliasi pencairan, ekspor laporan. |
| **Engineer / admin sistem** | Seed, OAuth partner keys, webhook URL, job health, rotasi secret. |

### Roles (model akses)

| Role | Status | Akses |
|---|---|---|
| `owner` | **Ada (Tahap 1)** | Full CRUD semua resource tenant. |
| `staff` | **Reserved** di auth (belum endpoint khusus) | Nanti: scoped ke `StaffAkunMarketplace` (pola sama `toko_staff_akun`). |
| `ops` / `gudang` / `finance` | **Belum** | Usulan M3+: pecah `staff` jadi role spesifik **atau** permission flags. Keputusan terbuka (§11). |

**Tahap 1:** semua endpoint bisnis memakai `require_roles_marketplace_erp("owner")`. Register selalu membuat `role=owner`.

---

## 3. Domain model

### 3.1 Yang sudah ada (Tahap 1) — jangan dirombak tanpa alasan kuat

```
UserMarketplaceErp (mpe_users)
  id, nama, email UNIQUE, password_hash, role, timestamps

AkunMarketplace (mpe_akun_marketplace)
  id, platform ∈ {shopee,tiktokshop,lazada,blibli}
  nama_toko, id_toko_eksternal (nullable sampai OAuth selesai)
  access_token, refresh_token, token_kedaluwarsa
  status (default: belum_terhubung), catatan, timestamps
  — unik logis (platform, id_toko_eksternal) saat id_toko_eksternal terisi
    (dicek di service layer; NULL boleh banyak = pending OAuth)

Produk (mpe_produk)  ← SKU induk
  id, sku_induk UNIQUE, nama, deskripsi
  harga_dasar, stok (angka terpusat, 1 gudang implisit)
  foto_url, aktif, timestamps

ProdukListing (mpe_produk_listing)
  id, produk_id → Produk, akun_id → AkunMarketplace
  platform, id_eksternal
  UNIQUE(platform, id_eksternal)
  harga_jual NULL = pakai Produk.harga_dasar
  stok_listing NULL = pakai Produk.stok
  aktif, timestamps
```

**Arti bisnis:** ubah `Produk.stok` sekali → nanti job push mengisi stok di semua listing aktif yang `stok_listing IS NULL` (dan yang override tetap memakai nilai sendiri sampai di-clear).

### 3.2 Entitas lanjutan (belum di kode — spek target)

#### Shop (sudah = `AkunMarketplace`)
Satu toko yang diotorisasi di satu platform. Status usulan:

| Status | Arti |
|---|---|
| `belum_terhubung` | Baris dibuat, OAuth belum selesai |
| `terhubung` / `aktif` | Token valid, sync boleh jalan |
| `token_kadaluarsa` | Refresh gagal / perlu re-auth |
| `nonaktif` | Sengaja dimatikan owner |

*(Sekarang default string `belum_terhubung`; normalisasi enum di M1.)*

#### SKU / Produk
SSOT fisik. `stok` di Tahap 1 = salinan cepat dari ledger; setelah M2, **saldo tersedia** dihitung dari ledger + reservasi, kolom `stok` jadi cache yang di-update atomik.

#### Listing
Mapping 1 Produk → N listing. Satu listing = satu item di satu shop platform.

#### Order (`Pesanan` + `ItemPesanan`) — M2

| Field inti | Catatan |
|---|---|
| `platform`, `id_eksternal`, `akun_id` | Upsert unik `(platform, id_eksternal)` |
| `status` dinormalisasi | `unpaid \| to_ship \| shipped \| completed \| cancelled` (sama konsep toko-erp) |
| `nama_pembeli`, `total`, timestamps | Snapshot |
| `tersinkron_marketplace`, `catatan_sinkron` | Hasil push status lokal → platform |
| Items | Snapshot `nama`, `harga_satuan`, `qty`, `subtotal`; FK opsional ke `Produk` / `ProdukListing` |

Pemetaan status asli platform → status generik **hanya** di adapter `infrastructure/erp_<platform>.py`.

#### Stock ledger (`StokLedger` + opsional `Gudang`) — M2/M3

| Konsep | Detail |
|---|---|
| `Gudang` | M3: multi-lokasi. M2: satu gudang implisit `DEFAULT`. |
| `StokLedger` | Append-only: `produk_id`, `gudang_id?`, `qty_delta`, `reason` (`adjust`, `reserve`, `release`, `ship`, `return`, `sync_in`), `ref_type`/`ref_id` (pesanan/listing), `created_at` |
| `StokReservasi` | Qty tertahan untuk order `to_ship` sampai `shipped` / `cancelled` |
| Invarian | `Produk.stok` (available) = Σ ledger − Σ reservasi aktif (atau di-maintain lewat transaksi) |

**Jangan** kurangi `Produk.stok` langsung dari banyak tempat tanpa ledger setelah M2.

#### Settlement (`Settlement` / `Pencairan`) — M4

| Field | Arti |
|---|---|
| `akun_id`, `platform`, `periode` / `id_eksternal_statement` | Identitas batch pencairan |
| `gross_sales`, `fee_platform`, `fee_payment`, `ongkir_subsidi`, `penalti`, `net` | Breakdown |
| `status` | `draft \| matched \| discrepancy \| paid` |
| Link ke `Pesanan` | Banyak-ke-satu / banyak-ke-banyak via tabel bridge |

Tujuan: owner bisa jawab “uang masuk rekening vs order completed”.

#### Di luar spek inti tapi disebut di kode fondasi

- **Pengiriman** (label, awb, kurir) — M3, sering melekat ke order `to_ship`.
- **Chat** (inbox lintas platform) — M4, terpisah dari chat toko web.
- **Iklan & laporan agregat** — M5.

### 3.3 Diagram relasi (ringkas)

```
UserMarketplaceErp
AkunMarketplace 1───* ProdukListing *───1 Produk
AkunMarketplace 1───* Pesanan 1───* ItemPesanan
Produk 1───* StokLedger
Pesanan ──(reserve/ship)──> StokLedger / StokReservasi
AkunMarketplace 1───* Settlement ──*─ Pesanan
```

---

## 4. Cakupan platform

| Urutan | Platform | Kode string | Prioritas |
|---|---|---|---|
| 1 | **Shopee** | `shopee` | **First** — OAuth partner, webhook, sync produk/order/stok |
| 2 | Lazada | `lazada` | Setelah pola Shopee stabil |
| 3 | Blibli | `blibli` | Setelah Lazada atau paralel jika credential sudah siap |
| 4 | TikTok Shop | `tiktokshop` | Sudah di enum fondasi; implementasi adapter belakangan |

**Credential model (dari placeholder toko-erp, dibawa ke sini):**

- **App-level (env):** `SHOPEE_PARTNER_ID`, `SHOPEE_PARTNER_KEY` (satu partner app untuk semua shop).
- **Push Mechanism (webhook):** `POST /api/marketplace-erp/shopee/push` (tanpa login; keaslian dicek lewat header Authorization = HMAC-SHA256 hex dari `<url callback>|<badan>` dengan *Live Push Partner Key*). Env: `SHOPEE_PUSH_KEY` (wajib, Encrypt), `SHOPEE_PUSH_URL` (opsional: URL callback persis seperti diketik di Open Platform bila berbeda dari yang terlihat server). Permintaan tanpa kode push (isi kosong/bukan push, mis. tombol Verify atau browser) dijawab 200 dan hanya dicatat (`hasil=probe`); `GET` ke alamat yang sama menjawab `{ok:true}` untuk cek jangkauan. Push `order_status` (code 3) dan `order_trackingno` (code 4) memicu tarik perubahan toko itu; push lain hanya dicatat. Semua push tercatat (14 hari) di `mpe_shopee_push`, dilihat lewat `GET /api/marketplace-erp/shopee/push/log`. Shopee hanya menunggu 3 detik dan mencoba ulang pada 5 menit, 30 menit, 3 jam; sinkron berkala 5 menit tetap jadi cadangan.
- **Shop-level (DB `AkunMarketplace`):** `id_toko_eksternal`, `access_token`, `refresh_token`, `token_kedaluwarsa`.

Jangan menyimpan partner_key di DB. Jangan mengembalikan token mentah di list/summary API (detail mask saja).

---

## 5. Roadmap bertahap M0–M5 + Definition of Done

> **M0 ≈ Tahap 1 yang sudah merge (PR #105) + hardening wajib sebelum OAuth live.**

### M0 — Fondasi & hardening *(sebagian sudah live)*

**Scope**

- Auth register/login/logout/me + cookie JWT tenant.
- CRUD `AkunMarketplace`, `Produk`, `ProdukListing`.
- Seed idempotent `GET /seed-now` **dengan gate** (lihat §8).
- FE private repo scaffold + login + halaman akun/SKU/listing (read/write ke API yang ada).
- `JWT_SECRET_MARKETPLACE_ERP` terpisah di production.
- Tes: `tests/test_marketplace_erp_foundation.py` hijau + smoke FE login.

**DoD**

- [ ] Seed tidak bisa dipanggil anonim di `APP_ENV=production` tanpa secret header.
- [ ] Owner bisa login di FE, buat SKU, buat akun pending, mapping listing manual.
- [ ] Token akun **tidak** bocor di response list.
- [ ] Zero coupling import ke `tenants/toko`.

### M1 — Shopee connect (OAuth + catalog)

**Scope**

- OAuth authorize + callback → isi `id_toko_eksternal` + tokens + status `terhubung`.
- Refresh token job sebelum kedaluwarsa.
- Pull katalog Shopee → upsert listing (dan opsi: buat/tautan Produk).
- Push stok/harga dari Produk → listing Shopee (manual trigger dulu).
- Env: `SHOPEE_PARTNER_ID/KEY`, `SHOPEE_REDIRECT_URI`, webhook secret.

**DoD**

- [ ] Owner menyelesaikan OAuth tanpa paste token manual.
- [ ] Re-auth toko yang sama tidak menduplikasi `AkunMarketplace`.
- [ ] Sync produk idempotent pada `(platform, id_eksternal)`.
- [ ] Adapter gagal jelas (503) jika partner key belum diisi — tidak fake-success.

### M2 — Orders + stock ledger

**Scope**

- Tabel `Pesanan` / `ItemPesanan`; pull order Shopee + webhook order.
- Status generik + tombol lokal `to_ship` (push ke platform bila adapter siap).
- `StokLedger` + kurangi/reserve saat order masuk ke `to_ship`.
- Inbox FE: filter status/platform/akun.

**DoD**

- [ ] Order duplikat webhook tidak mendobel baris.
- [ ] Cancel me-release reservasi; ship menulis ledger keluar.
- [ ] `Produk.stok` konsisten dengan ledger (ada tes invariansi).
- [ ] Push gagal tidak memblokir update status lokal (catat `catatan_sinkron`).

### M3 — Pengiriman + multi-gudang (opsional bertahap)

**Scope**

- AWB / kurir / label dari data platform atau input manual.
- `Gudang` + alokasi stok per gudang; reservasi per gudang.
- Role `staff` + scoping `StaffAkunMarketplace`.

**DoD**

- [ ] Staff hanya melihat akun yang diizinkan.
- [ ] Order to-ship punya jejak pengiriman minimal (awb atau “manual”).
- [ ] Transfer antar-gudang = dua baris ledger.

### M4 — Settlement + chat

**Scope**

- Import/sync statement pencairan Shopee; matching ke order.
- Flag discrepancy; ekspor CSV/Excel.
- Inbox chat buyer (read + reply bila API mengizinkan).

**DoD**

- [ ] Satu periode settlement menampilkan net vs Σ order matched.
- [ ] Chat tidak tercampur dengan modul toko web.
- [ ] Finance role (atau owner) saja yang akses settlement detail.

### M5 — Platform berikutnya + iklan/laporan

**Scope**

- Adapter Lazada → Blibli → TikTok Shop (pola sama: OAuth, pull, webhook, push stok).
- Laporan penjualan lintas platform (FE charts).
- Iklan: baca spend/ROAS dasar (bukan autobid).

**DoD**

- [ ] Menambah platform baru = file adapter + enum string, tanpa migrasi ENUM Postgres.
- [ ] Satu SKU bisa listing di ≥2 platform dengan stok terpusat.
- [ ] Dashboard owner: GMV, order, stok kritis, token expiring.

---

## 6. Outline permukaan API

Base: **`/api/marketplace-erp`**. Auth: cookie `marketplace_erp_token` atau `Authorization: Bearer`.

### Sudah ada (Tahap 1)

| Method | Path | Role | Ket |
|---|---|---|---|
| GET | `/seed-now` | *(harus digate)* | create schema + owner default |
| POST | `/auth/register` | public | buat owner |
| POST | `/auth/login` | public | set cookie |
| POST | `/auth/logout` | public | clear cookie |
| GET | `/auth/me` | login | profil |
| GET/POST | `/akun` | owner | list/create |
| GET/PATCH/DELETE | `/akun/{id}` | owner | detail/update/hapus |
| GET/POST | `/produk` | owner | list/create SKU |
| GET/PATCH/DELETE | `/produk/{id}` | owner | |
| GET/POST | `/listing` | owner | `?produk_id=` |
| PATCH/DELETE | `/listing/{id}` | owner | |

### Usulan lanjut (ringkas)

| Area | Path usulan | Milestone |
|---|---|---|
| OAuth | `GET /oauth/{platform}/start?akun_id=` → redirect; `GET /oauth/{platform}/callback` | M1 |
| Sync | `POST /akun/{id}/sync/produk`, `.../sync/pesanan`, `POST /produk/{id}/push-stok` | M1–M2 |
| Webhook | `POST /webhooks/shopee` (raw body + signature) | M1–M2 |
| Pesanan | `/pesanan`, `/pesanan/{id}`, `POST /pesanan/{id}/status` | M2 |
| Stok | `/stok/ledger?produk_id=`, `POST /stok/adjust`, `/gudang` | M2–M3 |
| Staff | `/staff`, `/staff/{id}/akun` | M3 |
| Settlement | `/settlement`, `/settlement/{id}/match` | M4 |
| Chat | `/chat/percakapan`, `/chat/percakapan/{id}/pesan` | M4 |
| Laporan | `/laporan/ringkas?dari=&sampai=` | M5 |

**Konvensi:** error Indonesia singkat (`detail` string); 409 untuk konflik unik; 503 jika integrasi platform belum dikonfigurasi.

---

## 7. Usulan aplikasi frontend

| | |
|---|---|
| **Nama repo (usulan)** | `sm85-code/frontend-marketplace-erp` (**private**) |
| **Stack** | Vite + React 19 + TypeScript, TanStack Query, React Router, axios cookie credentials, Tailwind + Radix/shadcn — **selaras** `frontend-madrasah` / `frontend-siabumdes-ts` |
| **Host** | DigitalOcean Static Site; `CORS_ORIGINS` backend ditambah origin FE |
| **Env FE** | `VITE_API_BASE_URL` → backend sm85-arch |

### Halaman M0–M2

1. Login / logout  
2. Dashboard ringkas (jumlah toko, SKU, listing, token hampir habis)  
3. **Toko** — list akun, mulai OAuth, status koneksi  
4. **SKU induk** — CRUD + stok/harga dasar  
5. **Listing** — mapping produk↔akun, override harga/stok  
6. **Pesanan** (M2) — inbox filter  
7. **Stok** (M2) — ledger + adjust  

Jangan campur UI ke `frontend-toko` / storefront Ampel Kuning.

---

## 8. Keamanan

### Seed gate (wajib sebelum production)

Pola ikut madrasah:

- Env `MARKETPLACE_ERP_SEED_SECRET`
- Header `X-Marketplace-Erp-Seed-Secret` (atau `X-Seed-Secret`)
- Jika `APP_ENV=production` dan secret tidak match / kosong → **tolak** anonim
- Owner yang sudah login boleh seed (opsional, sama ketat)

Default seed hari ini: `owner@marketplace-erp.internal` / `password123` — **ganti segera** setelah seed pertama; dokumentasikan di runbook, jangan commit password production.

### Secrets

| Secret | Tempat | Catatan |
|---|---|---|
| `JWT_SECRET_MARKETPLACE_ERP` | env platform | Jangan fallback jangka panjang ke `JWT_SECRET` bersama |
| `DATABASE_URL_MARKETPLACE_ERP` | env | DB dedicated |
| `SHOPEE_PARTNER_ID/KEY` (+ platform lain) | env | App-level saja |
| Shop tokens | kolom DB | Mask di API; pertimbangkan encrypt-at-rest M1+ (open decision) |
| Webhook secrets | env | Verifikasi signature; tolak body tanpa tanda tangan valid |

### Webhook

- Endpoint publik tanpa cookie user; **wajib** verifikasi HMAC/signature platform.
- Idempotensi: simpan `event_id` / pakai upsert `(platform, id_eksternal)`.
- Jangan log raw token atau PII berlebih.

### Lain

- Rate-limit login (ikuti pola madrasah bila sudah ada shared helper).
- Cookie: `httponly`, `secure`, `samesite` dari `shared.config`.
- Register publik: pertimbangkan tutup atau undang-only setelah owner pertama ada (open decision).

---

## 9. Arsitektur sync (OAuth, webhooks, jobs)

```
[FE admin] ──cookie──> [/api/marketplace-erp]
                              │
                              ├─ OAuth start/callback ──> Shopee Open Platform
                              ├─ CRUD lokal (SKU, listing, akun)
                              └─ enqueue job ──> Worker
                                                   │
[Shopee webhooks] ──POST /webhooks/shopee──> API ─┴─> Worker
                                                   │
                                          Adapter erp_shopee.py
                                          (sign request, pull/push)
                                                   │
                                          Postgres marketplace_erp
```

### OAuth (per shop)

1. Owner buat baris `AkunMarketplace` (status `belum_terhubung`).
2. FE panggil `GET /oauth/shopee/start?akun_id=...` → redirect consent.
3. Callback menukar code → tokens; set `id_toko_eksternal`, status `terhubung`.
4. Job refresh memutar `refresh_token` sebelum `token_kedaluwarsa`.

### Webhooks

- Preferensi: event-driven untuk order/status; polling sebagai fallback.
- Handler tipis: verifikasi → persist raw/event id → enqueue proses domain.

### Jobs (usulan tanggung jawab)

| Job | Frekuensi | Tugas |
|---|---|---|
| `refresh_tokens` | tiap 15–30 mnt | Refresh yang hampir expired |
| `pull_orders` | tiap N mnt / webhook | Upsert pesanan |
| `pull_catalog` | harian / manual | Upsert listing |
| `push_stock` | on-change + debounce | Dorong stok ke listing aktif |
| `pull_settlement` | harian | M4 |

**Runtime:** belum dipilih (ARQ/Redis, Celery, DO cron hit endpoint internal ber-secret). Lihat §11.

### Semantik sync

- **Pull:** platform → DB (katalog, order, settlement).
- **Push:** DB → platform (stok, harga, status `to_ship`).
- Kegagalan push **tidak** memrollback keputusan lokal; catat di `catatan_sinkron`.

---

## 10. Risiko

| Risiko | Dampak | Mitigasi |
|---|---|---|
| Partner Shopee belum approved | M1 macet | Tetap ship M0 FE + CRUD; adapter 503 jujur; token manual sementara (sudah didukung PATCH) |
| Duplikasi dengan `tenants/toko/modules/erp` | Dua sumber kebenaran | Bekukan fitur baru di toko-erp; arahkan kerja ke `marketplace_erp`; rencana cutover eksplisit |
| Stok oversell multi-shop | Order > stok fisik | Ledger + reservasi sejak M2; push stok agresif + safety buffer |
| Token bocor via log/API | Ambillah toko | Mask response; larang log Authorization/token; seed gate |
| Webhook replay / out-of-order | Status kacau | Idempotent upsert; last-write dengan `updated_at` platform bila ada |
| Rate limit API platform | Sync gagal massal | Queue + backoff + jitter; sync per-akun serial |
| Register terbuka | Siapa saja jadi owner | Tutup setelah bootstrap / pakai invite |
| FE belum ada | API sulit dipakai owner | Prioritaskan scaffold FE di M0 |

---

## 11. Keputusan terbuka (untuk user)

1. **Model bisnis tenant:** tetap **single-org** (Ampel Kuning saja) atau dari awal siapkan **multi-merchant SaaS** (schema `org_id` di semua tabel)?
2. **Nasib `tenants/toko/modules/erp`:** freeze segera + cutover ke `marketplace_erp`, dual-run sementara, atau biarkan toko-erp hanya arsip?
3. **Strategi push stok:** real-time on-change (debounce), batch terjadwal, atau hybrid (on-change + rekonsiliasi harian)?
4. **Enkripsi token shop di DB:** wajib sebelum OAuth production, atau cukup DB isolated + IAM sampai volume membesar?
5. **Job runner:** Redis+ARQ di App Platform, Celery, atau cron HTTP internal ber-shared-secret dulu (paling sederhana)?
6. *(bonus)* Pecah role `staff` vs tetap satu role + scoping akun saja sampai M4?
7. *(bonus)* Tutup `POST /auth/register` publik setelah owner pertama?

---

## 12. Lampiran — jejak kode saat ini

| Path | Isi |
|---|---|
| `tenants/marketplace_erp/modules/.../models.py` | User, Akun, Produk, Listing + komentar roadmap modul |
| `.../application/services.py` | Auth + CRUD + validasi platform/duplikat |
| `.../adapters/api/v1/marketplace_erp_router.py` | HTTP surface Tahap 1 |
| `.../infrastructure/auth.py` | Cookie & role gate |
| `.../infrastructure/seeder.py` | `create_all` + owner default |
| `.../infrastructure/database.py` | Engine async terpisah |
| `tests/test_marketplace_erp_foundation.py` | 9 tes fondasi |
| `main.py` | `include_router(..., prefix="/api/marketplace-erp")` |
| `.env.example` | `DATABASE_URL_MARKETPLACE_ERP`, `JWT_SECRET_MARKETPLACE_ERP` |

Referensi pola (bukan dependency runtime): `tenants/toko/modules/erp` (status order, staff-akun, placeholder `erp_shopee.py`) dan seed gate madrasah.

---

*Dokumen ini adalah spek desain. Perubahan schema fondasi Tahap 1 harus jarang dan beralasan — preferensi: tabel baru yang mereferensi id yang sudah ada.*
