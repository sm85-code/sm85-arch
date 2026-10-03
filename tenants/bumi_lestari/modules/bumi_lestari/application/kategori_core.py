"""Aturan kategori (spesifikasi 6.2): kategori sistem, kategori staf, kategori khusus admin, masuk laba.

Disimpan sebagai konstanta (bukan kolom DB) supaya backend dan frontend memakai satu sumber kebenaran:
`GET /kategori` mengembalikan tanda-tanda ini sebagai field terhitung.
"""
from __future__ import annotations

KATEGORI_PENJUALAN_MARKETPLACE = "Penjualan marketplace"
KATEGORI_PENJUALAN_WEB = "Penjualan toko web"
KATEGORI_RESELLER = "Penjualan reseller"
KATEGORI_PRODUKSI = "Biaya produksi / pembelian barang"
KATEGORI_GAJI = "Gaji karyawan"
KATEGORI_TAGIHAN = "Langganan & utilitas"
KATEGORI_BAGI_HASIL = "Bagi hasil"
KATEGORI_BIAYA_MARKETPLACE = "Biaya marketplace"
KATEGORI_KERUGIAN_RETUR = "Kerugian retur"
KATEGORI_BIAYA_IKLAN = "Biaya iklan"
KATEGORI_PRIVE = "Prive"
KATEGORI_SETORAN_MODAL = "Setoran modal"

# Hanya boleh dibuat oleh proses otomatis (pembayaran, penerimaan, gaji, unggah pencairan, ...).
KATEGORI_SISTEM = frozenset({
    KATEGORI_PENJUALAN_MARKETPLACE, KATEGORI_PENJUALAN_WEB, KATEGORI_RESELLER, KATEGORI_PRODUKSI,
    KATEGORI_GAJI, KATEGORI_TAGIHAN, KATEGORI_BAGI_HASIL, KATEGORI_BIAYA_MARKETPLACE, KATEGORI_KERUGIAN_RETUR,
})
# Staf (pemegang kas kecil) hanya boleh memakai empat kategori ini.
KATEGORI_STAF = ("Transport", "Packing", "Operasional", "Pengeluaran lain")
KATEGORI_KHUSUS_ADMIN = frozenset({KATEGORI_PRIVE, KATEGORI_SETORAN_MODAL})
# Tidak menambah/mengurangi laba bersih: distribusi laba dan modal.
KATEGORI_TIDAK_MASUK_LABA = frozenset({KATEGORI_PRIVE, KATEGORI_BAGI_HASIL, KATEGORI_SETORAN_MODAL})

GRUP_KATEGORI = {
    KATEGORI_PENJUALAN_MARKETPLACE: "Penjualan",
    KATEGORI_PENJUALAN_WEB: "Penjualan",
    KATEGORI_RESELLER: "Penjualan",
    "Pemasukan lain": "Pendapatan lain",
    KATEGORI_SETORAN_MODAL: "Modal",
    KATEGORI_PRODUKSI: "HPP",
    KATEGORI_GAJI: "Biaya operasional",
    KATEGORI_TAGIHAN: "Biaya operasional",
    "Transport": "Biaya operasional",
    "Packing": "Biaya operasional",
    "Operasional": "Biaya operasional",
    "Pengeluaran lain": "Biaya operasional",
    KATEGORI_BIAYA_IKLAN: "Biaya operasional",
    KATEGORI_BIAYA_MARKETPLACE: "Biaya marketplace",
    KATEGORI_KERUGIAN_RETUR: "Biaya operasional",
    KATEGORI_BAGI_HASIL: "Distribusi laba",
    KATEGORI_PRIVE: "Distribusi",
}


def is_sistem(nama: str) -> bool:
    return nama in KATEGORI_SISTEM


def untuk_staf(nama: str) -> bool:
    return nama in KATEGORI_STAF


def masuk_laba(nama: str) -> bool:
    return nama not in KATEGORI_TIDAK_MASUK_LABA


def grup(nama: str, jenis: str) -> str:
    return GRUP_KATEGORI.get(nama, "Pendapatan lain" if jenis == "pemasukan" else "Biaya operasional")
