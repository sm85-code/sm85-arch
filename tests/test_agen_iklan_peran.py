"""The ads-agent role can change ads and read products and orders. hermes-ai is left alone."""
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException

from tenants.marketplace_erp.modules.marketplace_erp.application import akses, agen_pengguna, peta_akses
from shared.security import verify_password


def test_the_role_is_ads_plus_read_only_product_and_orders_and_has_no_password():
    peran = next(r for r in akses.BAWAAN if r["id"] == "agen_iklan")
    assert peran["sistem"] is True and peran["akses_toko"] == "ditugaskan"
    assert peran["halaman"] == {"iklan": "tulis", "produk": "baca", "katalog": "baca", "pesanan": "baca"}
    assert "password" not in peran
    teks = Path("tenants/marketplace_erp/scripts/buat_pengguna_agen.py").read_text()
    assert "password123" not in teks and "AGEN_IKLAN_PASSWORD" in teks
    assert peta_akses.cari("/api/marketplace-erp/users/agen-iklan", "POST") == ("kelola_pengguna", "tulis")


def test_the_command_refuses_a_missing_password_and_hermes():
    with pytest.raises(HTTPException) as kosong:
        agen_pengguna.periksa_env({"AGEN_IKLAN_USERNAME": "agenbot"})
    assert kosong.value.status_code == 422
    with pytest.raises(HTTPException) as hermes:
        agen_pengguna.periksa_env({"AGEN_IKLAN_USERNAME": "hermes-ai", "AGEN_IKLAN_PASSWORD": "rahasia123"})
    assert hermes.value.status_code == 409
    siap = agen_pengguna.periksa_env({"AGEN_IKLAN_USERNAME": "agenbot", "AGEN_IKLAN_PASSWORD": "rahasia123", "AGEN_IKLAN_AKUN": "toko-1,toko-1"})
    assert siap["akun_ids"] == ["toko-1"] and siap["password"] == "rahasia123"


@pytest_asyncio.fixture
async def sesi():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace, UserMarketplaceErp

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        s.add(AkunMarketplace(id="toko-1", platform="shopee", nama_toko="Toko A"))
        s.add(UserMarketplaceErp(id="h", nama="Hermes", username="hermes-ai", password_hash="tetap", role="admin"))
        await s.commit()
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_startup_adds_the_role_once_and_does_not_overwrite_an_edit(sesi):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import RoleHalaman

    await akses.pastikan_bawaan(sesi)
    await sesi.commit()
    assert (await sesi.get(RoleHalaman, ("agen_iklan", "iklan"))).tingkat == "tulis"
    assert (await sesi.get(RoleHalaman, ("agen_iklan", "pesanan"))).tingkat == "baca"
    baris = await sesi.get(RoleHalaman, ("agen_iklan", "iklan"))
    baris.tingkat = "baca"
    await sesi.commit()
    await akses.pastikan_bawaan(sesi)
    await sesi.commit()
    assert (await sesi.get(RoleHalaman, ("agen_iklan", "iklan"))).tingkat == "baca"


@pytest.mark.asyncio
async def test_creating_the_agent_assigns_shops_and_does_not_touch_hermes(sesi):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp

    with pytest.raises(HTTPException) as exc:
        await agen_pengguna.buat(sesi, agen_pengguna.PenggunaAgenIn(username="hermes-ai", password="rahasia123", akun_ids=["toko-1"]))
    assert exc.value.status_code == 409
    hermes = await sesi.get(UserMarketplaceErp, "h")
    assert hermes.role == "admin" and hermes.password_hash == "tetap"
    hasil = await agen_pengguna.buat(
        sesi, agen_pengguna.PenggunaAgenIn(username="agenbot", password="rahasia123", akun_ids=["toko-1"]),
    )
    assert hasil["role"] == "agen_iklan" and hasil["akun_ids"] == ["toko-1"] and "password" not in hasil
    user = await sesi.get(UserMarketplaceErp, hasil["id"])
    assert user.role == "agen_iklan" and verify_password("rahasia123", user.password_hash)
    assert user.password_hash != "rahasia123"
