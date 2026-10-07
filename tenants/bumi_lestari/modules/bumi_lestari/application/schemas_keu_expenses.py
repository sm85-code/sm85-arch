"""Classification is chosen on the server, never from arbitrary client COA IDs."""
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .schemas_keu import Id, InputBase, PositiveMoney, Ref

ExpenseTab = Literal["vendor", "bahan", "operasional", "gaji_iklan"]

# key: (tab, label, system account code)
CATEGORIES = {
    "cat": ("bahan", "Cat", "HPP-BAHAN"),
    "dempul": ("bahan", "Dempul", "HPP-BAHAN"),
    "amplas": ("bahan", "Amplas", "HPP-BAHAN"),
    "lem": ("bahan", "Lem", "HPP-BAHAN"),
    "paku": ("bahan", "Paku", "HPP-BAHAN"),
    "bahan_lain": ("bahan", "Bahan pendukung lainnya", "HPP-BAHAN"),
    "sewa": ("operasional", "Sewa gudang", "BEBAN-SEWA"),
    "listrik": ("operasional", "Listrik", "BEBAN-LANGGANAN"),
    "internet": ("operasional", "Internet", "BEBAN-LANGGANAN"),
    "pemeliharaan": ("operasional", "Pemeliharaan / perbaikan mesin", "BEBAN-PEMELIHARAAN"),
    "atk": ("operasional", "ATK", "BEBAN-OPERASIONAL"),
    "operasional_lain": ("operasional", "Operasional lainnya", "BEBAN-OPERASIONAL"),
    "gaji": ("gaji_iklan", "Gaji karyawan", "BEBAN-GAJI"),
    "insentif": ("gaji_iklan", "Insentif pengelola", "BEBAN-GAJI"),
    "ads_shopee": ("gaji_iklan", "Shopee Ads", "BEBAN-IKLAN"),
    "ads_tiktok": ("gaji_iklan", "TikTok Ads", "BEBAN-IKLAN"),
    "ads_tokopedia": ("gaji_iklan", "Tokopedia Ads", "BEBAN-IKLAN"),
    "iklan_lain": ("gaji_iklan", "Pemasaran / iklan lainnya", "BEBAN-IKLAN"),
}


class PengeluaranIn(InputBase):
    referensi: Ref
    tanggal: date
    akun_kas_id: Id
    tab: Literal["bahan", "operasional", "gaji_iklan"]
    kategori: str = Field(min_length=1, max_length=32)
    jumlah: PositiveMoney
    keterangan: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def matching_category(self):
        category = CATEGORIES.get(self.kategori)
        if category is None or category[0] != self.tab:
            raise ValueError("Kategori tidak sesuai tab pengeluaran")
        return self
