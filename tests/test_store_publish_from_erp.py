"""ERP -> store product publish: the endpoint, the upsert rules and the
photo importer's SSRF guards."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PengaturanStok
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as erp_router
from tenants.marketplace_erp.modules.marketplace_erp.application import services as erp_services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    ProdukIn,
    ProdukListingIn,
    ProdukPatch,
    PublishTokoIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.store.modules.store.infrastructure import media_import
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import ProdukStore


async def _session(base):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


@pytest_asyncio.fixture
async def erp():
    engine, s = await _session(MarketplaceErpBase)
    s.add(PengaturanStok(id="global", gudang_aktif=True))
    await s.flush()
    yield s
    await s.close()
    await engine.dispose()


@pytest_asyncio.fixture
async def store():
    engine, s = await _session(StoreBase)
    yield s
    await s.close()
    await engine.dispose()


async def _erp_produk(erp, *, stok=7, foto_url=None):
    produk = await erp_services.create_produk(
        erp,
        ProdukIn(sku_induk="SKU-1", nama="Kopi 250g", deskripsi="Arabika", harga_dasar=Decimal("50000"), stok=stok, foto_url=foto_url),
    )
    akun = await erp_services.create_akun_marketplace(erp, AkunMarketplaceIn(platform="shopee", nama_toko="Toko A"))
    await erp_services.create_listing(
        erp, ProdukListingIn(produk_id=produk.id, akun_id=akun.id, platform="shopee", id_eksternal="x1")
    )
    return produk


async def _publish(erp, store, produk, **opts):
    return await erp_router.publish_produk_ke_toko(
        produk.id, PublishTokoIn(salin_foto=False, **opts), session=erp, store_session=store, _=SimpleNamespace()
    )


@pytest.mark.asyncio
async def test_publish_creates_store_copy_with_provenance(erp, store):
    produk = await _erp_produk(erp)
    out = await _publish(erp, store, produk)
    assert out["dibuat"] is True
    copy = out["produk"]
    assert (copy["nama"], Decimal(copy["harga"]), copy["stok"], copy["sumber"], copy["aktif"]) == (
        "Kopi 250g", Decimal("50000"), 7, "erp", True,
    )
    row = await store.get(ProdukStore, copy["id"])
    assert row.erp_produk_id == produk.id and row.platform_asal == "shopee"


@pytest.mark.asyncio
async def test_republish_updates_price_but_keeps_store_stock(erp, store):
    produk = await _erp_produk(erp, stok=7)
    first = await _publish(erp, store, produk)
    row = await store.get(ProdukStore, first["produk"]["id"])
    row.stok = 2  # the store sold some
    await erp_services.update_produk(erp, produk.id, ProdukPatch(nama="Kopi 500g"))
    again = await _publish(erp, store, produk, harga=Decimal("75000"))
    assert again["dibuat"] is False
    assert again["produk"]["id"] == first["produk"]["id"]
    assert (again["produk"]["nama"], Decimal(again["produk"]["harga"]), again["produk"]["stok"]) == (
        "Kopi 500g", Decimal("75000"), 2,
    )


@pytest.mark.asyncio
async def test_publish_can_start_inactive_with_own_stock(erp, store):
    produk = await _erp_produk(erp, stok=7)
    out = await _publish(erp, store, produk, aktif=False, stok=3)
    assert (out["produk"]["aktif"], out["produk"]["stok"]) == (False, 3)


@pytest.mark.asyncio
async def test_publish_unknown_erp_product_404(erp, store):
    with pytest.raises(HTTPException) as exc:
        await erp_router.publish_produk_ke_toko(
            "nope", PublishTokoIn(salin_foto=False), session=erp, store_session=store, _=SimpleNamespace()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_store_db_dependency_is_501_when_unconfigured(monkeypatch):
    monkeypatch.setattr(erp_router.store_database, "SessionLocal", None)
    with pytest.raises(HTTPException) as exc:
        await erp_router._store_db().__anext__()
    assert exc.value.status_code == 501


# --- photo importer ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://cf.shopee.co.id/a.jpg",  # not https
        "https://127.0.0.1/a.jpg",  # loopback
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://10.0.0.5/a.jpg",  # private
        "https://user:pw@cf.shopee.co.id/a.jpg",  # credentials
        "https://cf.shopee.co.id:8443/a.jpg",  # odd port
    ],
)
def test_importer_rejects_unsafe_urls(url, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be fetched")

    monkeypatch.setattr(media_import.requests, "get", boom)
    monkeypatch.setattr(media_import, "_public_host", lambda host: not host[0].isdigit())
    assert media_import._download_sync(url) is None


class _Resp:
    def __init__(self, status_code=200, ctype="image/jpeg", body=b"\xff\xd8\xff" + b"0" * 20):
        self.status_code, self.headers, self._body = status_code, {"Content-Type": ctype}, body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i : i + n]


def test_importer_downloads_public_https(monkeypatch):
    monkeypatch.setattr(media_import, "_public_host", lambda h: True)
    monkeypatch.setattr(media_import.requests, "get", lambda *a, **k: _Resp())
    data, ctype = media_import._download_sync("https://cf.shopee.co.id/a.jpg")
    assert ctype == "image/jpeg" and data.startswith(b"\xff\xd8\xff")


def test_importer_rejects_oversize_and_redirect(monkeypatch):
    monkeypatch.setattr(media_import, "_public_host", lambda h: True)
    monkeypatch.setattr(media_import, "_MAX_BYTES", 10)
    monkeypatch.setattr(media_import.requests, "get", lambda *a, **k: _Resp())
    assert media_import._download_sync("https://cf.shopee.co.id/a.jpg") is None
    monkeypatch.setattr(media_import.requests, "get", lambda *a, **k: _Resp(status_code=302))
    assert media_import._download_sync("https://cf.shopee.co.id/a.jpg") is None


@pytest.mark.asyncio
async def test_importer_failure_returns_none_not_error(monkeypatch):
    def boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(media_import, "_download_sync", boom)
    assert await media_import.import_foto_dari_url("https://cf.shopee.co.id/a.jpg") is None
    assert await media_import.import_foto_dari_url(None) is None


@pytest.mark.asyncio
async def test_weight_size_and_preorder_follow_the_erp_product(erp, store):
    produk = await erp_services.create_produk(
        erp,
        ProdukIn(
            sku_induk="PO-1", nama="Meja", harga_dasar=Decimal("100000"), stok=1,
            berat_gram=2500, panjang_cm=Decimal("60"), lebar_cm=Decimal("40"), tinggi_cm=Decimal("30"),
            preorder=True, hari_proses=10,
        ),
    )
    await _publish(erp, store, produk)
    toko = (await store.execute(__import__("sqlalchemy").select(ProdukStore))).scalar_one()
    assert (toko.berat_gram, toko.preorder, toko.hari_proses) == (2500, True, 10)
    assert (toko.panjang_cm, toko.lebar_cm, toko.tinggi_cm) == (Decimal("60"), Decimal("40"), Decimal("30"))


def test_erp_preorder_rules():
    base = dict(sku_induk="x", nama="x", harga_dasar=Decimal("1"))
    assert ProdukIn(**base, hari_proses=9).hari_proses == 2
    with pytest.raises(ValueError):
        ProdukIn(**base, preorder=True, hari_proses=2)


@pytest.mark.asyncio
async def test_erp_patch_preorder_validates(erp):
    produk = await erp_services.create_produk(erp, ProdukIn(sku_induk="P2", nama="a", harga_dasar=Decimal("1")))
    await erp_services.update_produk(erp, produk.id, ProdukPatch(preorder=True, hari_proses=5))
    assert (produk.preorder, produk.hari_proses) == (True, 5)
    with pytest.raises(HTTPException) as exc:
        await erp_services.update_produk(erp, produk.id, ProdukPatch(hari_proses=30))
    assert exc.value.status_code == 400
    await erp_services.update_produk(erp, produk.id, ProdukPatch(preorder=False))
    assert produk.hari_proses == 2


@pytest.mark.asyncio
async def test_per_shop_mode_does_not_seed_store_from_warehouse_or_reference(erp, store):
    master = await _erp_produk(erp, stok=7)
    master.stok_referensi = 80
    await erp.delete(await erp.get(PengaturanStok, "global"))
    await erp.flush()
    result = await _publish(erp, store, master)
    assert result["produk"]["stok"] == 0
    assert master.stok == 7 and master.stok_referensi == 80
