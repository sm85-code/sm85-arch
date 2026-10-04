from decimal import Decimal
from types import SimpleNamespace

from tenants.bumi_lestari.modules.bumi_lestari.application.erp_pencairan import rincian_biaya

def test_selisih_ongkir_dan_biaya_dijumlahkan():
    baris = SimpleNamespace(
        rincian='{"commission_fee": "1000", "order_ams_commission_fee": "250", "order_original_price": "50000"}',
        komisi=1000, layanan=500, transaksi=0, ongkir=-300, penyesuaian=0,
    )
    biaya = rincian_biaya(baris)
    assert biaya["selisih_ongkir"] == Decimal("300")
    assert biaya["order_ams_commission_fee"] == Decimal("250")
    assert "order_original_price" not in biaya
    assert sum(biaya.values()) == Decimal("1000") + Decimal("250") + Decimal("500") + Decimal("300")
