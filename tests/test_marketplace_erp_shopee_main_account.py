"""Shopee authorisation from a main account: one code -> many shops."""
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

TOKENS = {
    "access_token": "at",
    "refresh_token": "rt",
    "expire_in": 14400,
    "shop_id_list": [111, 222, 333],
    "merchant_id_list": [],
}


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _placeholder(session, nama="Placeholder"):
    return await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko=nama))


@pytest.mark.asyncio
async def test_binds_all_shops_first_one_on_the_starting_row(session):
    akun = await _placeholder(session)
    toko = await services.hubungkan_shopee_akun_utama(session, akun, TOKENS, {"222": "Toko Dua"})

    assert [t["id_toko_eksternal"] for t in toko] == ["111", "222", "333"]
    assert toko[0]["akun_id"] == akun.id and toko[0]["nama_toko"] == "Placeholder"
    assert [t["nama_toko"] for t in toko[1:]] == ["Toko Dua", "Shopee 333"]
    rows = await services.list_akun_marketplace(session, platform="shopee")
    assert len(rows) == 3 and all(r.status == "terhubung" and r.access_token == "at" for r in rows)


@pytest.mark.asyncio
async def test_reauthorising_updates_tokens_without_duplicates_and_drops_empty_placeholder(session):
    first = await _placeholder(session)
    await services.hubungkan_shopee_akun_utama(session, first, TOKENS)
    second = await _placeholder(session, "Placeholder 2")

    toko = await services.hubungkan_shopee_akun_utama(
        session, second, {**TOKENS, "access_token": "at2", "refresh_token": "rt2"}
    )

    assert not any(t["baru"] for t in toko)
    rows = await services.list_akun_marketplace(session, platform="shopee")
    assert len(rows) == 3  # no duplicates, and the unused placeholder is gone
    assert {r.access_token for r in rows} == {"at2"}
    assert second.id not in {r.id for r in rows}


@pytest.mark.asyncio
async def test_no_shops_returned_is_a_clear_error(session):
    akun = await _placeholder(session)
    with pytest.raises(HTTPException) as exc:
        await services.hubungkan_shopee_akun_utama(session, akun, {**TOKENS, "shop_id_list": []})
    assert exc.value.status_code == 400


# --- callback route ------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_main_account_flow(session, monkeypatch):
    seen = {}

    async def fake_exchange(**kw):
        seen.update(kw)
        return dict(TOKENS)

    async def fake_name(access_token, shop_id):
        return {"111": "Toko Satu"}.get(shop_id)

    monkeypatch.setattr(erp_shopee, "exchange_token", fake_exchange)
    monkeypatch.setattr(erp_shopee, "get_shop_name", fake_name)
    akun = await _placeholder(session)

    out = await router.oauth_shopee_callback(akun.id, code="c", shop_id=None, main_account_id="42", session=session)

    assert seen == {"code": "c", "main_account_id": "42"}
    assert out["ok"] is True and len(out["toko"]) == 3
    assert out["toko"][1]["nama_toko"] == "Shopee 222"  # lookup failed -> fallback name


@pytest.mark.asyncio
async def test_callback_shop_flow_unchanged(session, monkeypatch):
    async def fake_exchange(**kw):
        assert kw == {"code": "c", "shop_id": "555"}
        return {"access_token": "at", "refresh_token": "rt", "expire_in": 14400}

    monkeypatch.setattr(erp_shopee, "exchange_token", fake_exchange)
    akun = await _placeholder(session)
    out = await router.oauth_shopee_callback(akun.id, code="c", shop_id="555", main_account_id=None, session=session)
    assert out["id_toko_eksternal"] == "555" and out["status"] == "terhubung"


@pytest.mark.asyncio
async def test_callback_requires_exactly_one_identifier(session):
    akun = await _placeholder(session)
    for kw in ({"shop_id": None, "main_account_id": None}, {"shop_id": "1", "main_account_id": "2"}):
        with pytest.raises(HTTPException) as exc:
            await router.oauth_shopee_callback(akun.id, code="c", session=session, **kw)
        assert exc.value.status_code == 400


# --- token exchange request ------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_token_sends_main_account_id(monkeypatch):
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1246597")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    sent = {}

    async def fake_post(url, body, **_):
        sent.update(url=url, body=body)
        return {"access_token": "at", "shop_id_list": [1]}

    monkeypatch.setattr(erp_shopee, "_http_post_json", fake_post)
    await erp_shopee.exchange_token(code="c", main_account_id="42")
    assert sent["body"] == {"code": "c", "partner_id": 1246597, "main_account_id": 42}
    assert "/api/v2/auth/token/get" in sent["url"]

    with pytest.raises(ValueError):
        await erp_shopee.exchange_token(code="c")
    with pytest.raises(ValueError):
        await erp_shopee.exchange_token(code="c", shop_id="1", main_account_id="2")
