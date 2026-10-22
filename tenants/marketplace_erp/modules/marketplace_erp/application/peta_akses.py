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
        (r"^/pesanan/[^/]+/pendapatan-shopee$", GET, "settlement"),
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
        (r"^/processing/(candidates|jobs)", None, PROSES),  # which tab a change belongs to: processing_workflow.izin_tab
        (r"^/processing/document-templates", None, "data_template"),
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
        (r"^/laporan-keuangan", None, "laporan_keuangan"),
        (r"^/akun/[^/]+/(sync/settlement|transaksi-dana|saldo-toko|pendapatan-shopee|rincian-pesanan-pendapatan|pelacakan-pendapatan|ringkasan-pendapatan-shopee)", None, "settlement"),
        (r"^/settlement", None, "settlement"),
        # Info Toko, Iklan
        (r"^/akun/[^/]+/(performa-toko|penalti|pengaturan-shopee)", None, "info_toko"),
        (r"^/akun/[^/]+/(produk-boost|otomasi-produk)", None, "otomasi"),
        (r"^/akun/[^/]+/(sync/iklan|iklan|gmv-max)", None, "iklan"),
        (r"^/iklan", None, "iklan"),
        # Pengaturan
        (r"^/shopee/push/", None, "push_shopee"),
        (r"^/(akun|oauth|koneksi-toko|toko-penjualan)(/|$)", None, "kelola_toko"),
        (r"^/asisten", None, "asisten"),
        (r"^/(users|staff-akun|roles)", None, "kelola_pengguna"),
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
