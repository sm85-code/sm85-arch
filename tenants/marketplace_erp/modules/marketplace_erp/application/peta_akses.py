"""Which page (halaman) a request belongs to, so the role settings can decide it.

A route belongs to the first rule that matches its path and method. Its level is "baca" for GET and "tulis" for
everything else. Rules are drawn so that every route of a page has the same level of sensitivity: what was open to
admin, owner and staff before roles became editable is still open to exactly the same built-in roles by default
(tests/test_marketplace_erp_peta_akses.py checks this against a snapshot of the old fixed rules).

A route that matches no rule keeps its fixed role list. The page "*" means any role that exists (shop list,
notifications, push settings, which everybody needs).
"""
from __future__ import annotations

import re

GET = {"GET", "HEAD"}
# Pages of Data Produksi that hold prices, and every page that may read the data for production.
HARGA = "data_produk_kayu|data_katalog_manual|data_tarif_pengecatan|data_tarif_packing"
PROSES = "processing_tukang|processing_supplier|processing_pengecatan|processing_packing"
LIHAT = PROSES + "|data_tukang|data_supplier|" + HARGA + "|data_katalog_otomatis"
PREFIX_API = "/api/marketplace-erp"
# Dana & Saldo Shopee and Pengaturan Toko were one page each; the old key still opens them until a role is saved again.
SETTLEMENT = "settlement_saldo|settlement_pendapatan|settlement_dana|settlement_mutasi|settlement"
IKLAN = "iklan_performa|iklan_kampanye|iklan_gmv|iklan_agen|iklan_catatan|iklan"
TEMPLATE = "data_template_inv|data_template_po|data_template"
PENGATURAN_TOKO = "pengaturan_toko_profil|pengaturan_toko_pengiriman|pengaturan_toko_alamat|pengaturan_toko_libur|info_toko"

# (regex on the path, methods or None for all, halaman)
ATURAN: list[tuple[re.Pattern, set[str] | None, str]] = [
    (re.compile(p), m, h)
    for p, m, h in [
        # everybody who is signed in with an existing role
        (r"^/akun$", GET, "*"),
        (r"^/akun/[^/]+$", GET, "*"),
        (r"^/notifikasi", None, "*"),
        (r"^/push/", None, "*"),
        (r"^/kategori-transaksi", GET, "*"),  # everybody needs the names; changing them is the page below
        (r"^/pengaturan-ui$", GET, "*"),  # every screen shows these texts; changing them is Kelola Aplikasi
        # Harian
        (r"^/laporan/dashboard$", GET, "dashboard"),
        (r"^/laporan/ringkas$", GET, "laporan_ringkas"),
        (r"^/pesanan/[^/]+/pendapatan-shopee$", GET, SETTLEMENT),
        (r"^/pesanan(/manual)?$", {"POST"}, "pesanan_manual"),
        (r"^/pesanan/[^/]+$", {"DELETE"}, "pesanan_manual"),
        (r"^/pesanan/[^/]+/chat", None, "chat"),
        (r"^/pesanan-otomatis", None, "otomasi"),
        (r"^/pesanan(/|$)", None, "pesanan"),
        (r"^/sinkronisasi", None, "pesanan"),
        (r"^/akun/[^/]+/chat", None, "chat"),
        (r"^/akun/[^/]+/retur", None, "retur"),
        # Produk
        (r"^/akun/[^/]+/promosi", None, "promosi"),
        (r"^/akun/[^/]+/publikasi", None, "publikasi"),
        (r"^/akun/[^/]+/(sync/(pesanan|produk)|push/stok-harga)", None, "sinkron_shopee"),
        (r"^/katalog-shopee/(salin-master|kirim-toko)$", None, "katalog"),
        (r"^/katalog-shopee/[^/]+/(produk|status)$", None, "katalog"),
        (r"^/katalog-shopee(/ringkasan|/[^/]+)?$", GET, "katalog"),
        (r"^/katalog-shopee", None, "katalog_lanjutan"),
        (r"^/produk(-keluarga)?(/|$)", None, "produk"),
        (r"^/listing", None, "listing"),
        (r"^/(gudang|stok|pengaturan-stok)", None, "gudang"),
        # Processing: the work queue is Pemetaan Order; each menu of Data Produksi is its own page. Pemetaan Order may read
        # what it needs to map (prices, parties, the supplier catalog), only the page of that data changes it. A change to
        # a price names the page by its kind of price, which only the request body tells: the route checks that itself
        # (processing_workflow.izin_harga).
        (r"^/processing/candidates(/|$)", GET, "processing_tukang|processing_supplier"),
        (r"^/processing/jobs(/|$)", None, PROSES),  # which tab a change belongs to: processing_workflow.izin_tab
        (r"^/processing/document-templates/INV$", None, "data_template_inv|data_template"),
        (r"^/processing/document-templates/PO$", None, "data_template_po|data_template"),
        (r"^/processing/document-templates", None, TEMPLATE),
        (r"^/processing/parties/tukang", {"PUT"}, "data_tukang"),
        (r"^/processing/parties/supplier", {"PUT"}, "data_supplier"),
        (r"^/processing/paint-colors", {"PUT"}, "data_tarif_pengecatan"),
        (r"^/processing/production-structure", {"PUT"}, "data_produk_kayu"),
        (r"^/processing/supplier-catalog", {"POST", "DELETE"}, "data_katalog_otomatis"),
        (r"^/processing/prices", {"PUT", "POST", "DELETE"}, HARGA),
        (r"^/processing/parties", {"PUT"}, "data_tukang|data_supplier"),  # any other kind is refused by the route itself
        (r"^/processing(/processing)?/(data|prices|paint-colors|production-structure|parties|supplier-catalog)", GET, LIHAT),
        # Keuangan
        (r"^/kategori-transaksi", None, "kategori_transaksi"),
        (r"^/laporan-keuangan/(sumber|saldo-awal|pratinjau|tutup|riwayat|impor-drive)$", None, "laporan_tutup_buku"),
        (r"^/laporan-keuangan/[^/]+/(perbarui|buka-kembali)$", None, "laporan_tutup_buku"),
        (r"^/laporan-keuangan(/operasional|/lembar-manual|/pemasukan-shopee|/saldo-penjual-shopee)?$", GET, "laporan_keuangan|laporan_tutup_buku"),  # the close panel computes live figures from these reads
        (r"^/laporan-keuangan", None, "laporan_keuangan"),
        (r"^/akun/[^/]+/saldo-toko", None, "settlement_saldo|settlement"),
        (r"^/akun/[^/]+/transaksi-dana", None, "settlement_mutasi|settlement"),
        (r"^/akun/[^/]+/(pendapatan-shopee|rincian-pesanan-pendapatan|pelacakan-pendapatan|ringkasan-pendapatan-shopee)", None, "settlement_pendapatan|settlement"),
        (r"^/akun/[^/]+/sync/settlement", None, "settlement_dana|settlement"),
        (r"^/settlement", None, "settlement_dana|settlement"),
        # Info Toko, Iklan
        (r"^/akun/[^/]+/(performa-toko|penalti)", None, "performa_toko|info_toko"),
        (r"^/akun/[^/]+/pengaturan-shopee/profil", None, "pengaturan_toko_profil|info_toko"),
        (r"^/akun/[^/]+/pengaturan-shopee/jasa-kirim", None, "pengaturan_toko_pengiriman|info_toko"),
        (r"^/akun/[^/]+/pengaturan-shopee/alamat", None, "pengaturan_toko_alamat|info_toko"),
        (r"^/akun/[^/]+/pengaturan-shopee/libur", None, "pengaturan_toko_libur|info_toko"),
        (r"^/akun/[^/]+/pengaturan-shopee", None, PENGATURAN_TOKO),
        # Manual catalog tools and scheduled automation have different page permissions.
        (r"^/akun/[^/]+/produk-boost$", None, "katalog_lanjutan"),
        (r"^/akun/[^/]+/otomasi-produk", None, "otomasi"),
        (r"^/akun/[^/]+/iklan/agen", None, "iklan_agen|iklan"),
        (r"^/iklan/agen", None, "iklan_agen|iklan"),
        (r"^/akun/[^/]+/iklan/performa-jam", None, "iklan_performa|iklan"),
        (r"^/iklan-toko", None, "iklan_performa|iklan"),
        (r"^/akun/[^/]+/gmv-max", None, "iklan_gmv|iklan"),
        (r"^/akun/[^/]+/iklan/(kampanye|saran|saran-ai|modal|shopee)", None, "iklan_kampanye|iklan"),
        (r"^/iklan/shopee", None, "iklan_kampanye|iklan"),
        (r"^/akun/[^/]+/sync/iklan", None, IKLAN),  # one sync feeds every tab
        (r"^/iklan(/|$)", None, "iklan_catatan|iklan"),
        (r"^/akun/[^/]+/iklan", None, IKLAN),
        # Pengaturan
        (r"^/shopee/push/", None, "push_shopee"),
        (r"^/toko-penjualan(/|$)", None, "toko_penjualan"),
        (r"^/order-bridge(/|$)", None, "toko_penjualan"),
        (r"^/(akun|oauth|koneksi-toko)(/|$)", None, "kelola_toko"),
        (r"^/asisten", None, "asisten"),
        (r"^/roles", GET, "kelola_role|kelola_pengguna"),  # the Pengguna tab needs the role names for its form
        (r"^/roles", None, "kelola_role"),
        (r"^/(users|staff-akun)", None, "kelola_pengguna"),
        (r"^/pengaturan-ui", None, "kelola_aplikasi"),
    ]
]


def cari(path: str, method: str) -> tuple[str, str] | None:
    """(halaman, level) for a request, or None when no rule covers it (the fixed role list applies)."""
    i = path.find(PREFIX_API)
    if i >= 0:
        path = path[i + len(PREFIX_API):]
    method = method.upper()
    for pola, metode, halaman in ATURAN:
        if pola.search(path) and (metode is None or method in metode):
            return halaman, ("baca" if halaman == "*" or method in GET else "tulis")
    return None
