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
        # Harian
        (r"^/laporan/dashboard$", GET, "dashboard"),
        (r"^/laporan/ringkas$", GET, "laporan_ringkas"),
        (r"^/pesanan/[^/]+/pendapatan-shopee$", GET, "settlement"),
        (r"^/pesanan$", {"POST"}, "pesanan_manual"),
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
        # Processing
        (r"^/processing", None, "processing"),
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
        (r"^/(akun|oauth|koneksi-toko)(/|$)", None, "kelola_toko"),
        (r"^/asisten", None, "asisten"),
        (r"^/(users|staff-akun|roles)", None, "kelola_pengguna"),
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
