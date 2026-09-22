"""Regression tests for the toko (online shop) tenant module foundation."""


def test_toko_token_round_trip(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-key-for-unit-tests")
    import importlib

    from shared import config as config_module

    importlib.reload(config_module)
    import shared.security as security_module

    importlib.reload(security_module)
    import tenants.toko.modules.toko.infrastructure.auth as toko_auth_module

    importlib.reload(toko_auth_module)

    class FakeUser:
        id = "user-1"
        role = "admin_toko"

    token = toko_auth_module.issue_toko_token(FakeUser())
    payload = security_module.decode_access_token(token)

    assert payload["sub"] == "user-1"
    assert payload["role"] == "admin_toko"


def test_produk_out_serializes_decimal_as_string():
    from decimal import Decimal

    from tenants.toko.modules.toko.application.services import produk_out

    class FakeProduk:
        id = "produk-1"
        nama = "Beras 5kg"
        deskripsi = ""
        kategori = "sembako"
        harga = Decimal("65000.00")
        stok = 10
        foto_url = None
        aktif = True

    out = produk_out(FakeProduk())
    assert out["harga"] == "65000.00"
    assert out["stok"] == 10
