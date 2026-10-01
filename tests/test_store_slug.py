"""SEO-friendly product URLs: slug generation, uniqueness, stability and lookup."""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import ProdukIn, ProdukPatch
from tenants.store.modules.store.application.slug import slugify, with_suffix
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import ProdukStore
from tenants.store.modules.store.infrastructure.seeder import backfill_slugs


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.mark.parametrize(
    ("nama", "expected"),
    [
        ("Partisi Ruangan Kayu Estetik", "partisi-ruangan-kayu-estetik"),
        ("  Kopi   Arabika 250g!! ", "kopi-arabika-250g"),
        ("Café Crème & Más", "cafe-creme-mas"),
        ("100% Katun / Premium", "100-katun-premium"),
        ("!!!", "produk"),
        ("", "produk"),
    ],
)
def test_slugify(nama, expected):
    assert slugify(nama) == expected


def test_slugify_is_length_limited_and_never_ends_with_a_dash():
    slug = slugify("kata " * 40)
    assert len(slug) <= 80 and not slug.endswith("-")
    assert len(with_suffix(slug, 12)) <= 80 and with_suffix(slug, 12).endswith("-12")


def test_with_suffix_leaves_the_first_one_plain():
    assert with_suffix("beras", 1) == "beras"
    assert with_suffix("beras", 3) == "beras-3"


@pytest.mark.asyncio
async def test_create_gives_a_slug_and_same_names_get_numbered(session):
    a = await services.create_produk(session, ProdukIn(nama="Beras 5kg", harga=Decimal("65000")))
    b = await services.create_produk(session, ProdukIn(nama="Beras 5kg", harga=Decimal("66000")))
    c = await services.create_produk(session, ProdukIn(nama="Beras 5kg!", harga=Decimal("67000")))
    assert (a.slug, b.slug, c.slug) == ("beras-5kg", "beras-5kg-2", "beras-5kg-3")


@pytest.mark.asyncio
async def test_renaming_keeps_the_slug_so_links_and_rankings_survive(session):
    p = await services.create_produk(session, ProdukIn(nama="Beras 5kg", harga=Decimal("65000")))
    await services.update_produk(session, p.id, ProdukPatch(nama="Beras Premium 5kg"))
    assert p.slug == "beras-5kg"


@pytest.mark.asyncio
async def test_public_lookup_accepts_slug_and_old_id_links(session):
    p = await services.create_produk(session, ProdukIn(nama="Beras 5kg", harga=Decimal("65000")))
    assert (await services.get_produk_by_ref(session, "beras-5kg")).id == p.id
    assert (await services.get_produk_by_ref(session, p.id)).id == p.id
    with pytest.raises(HTTPException) as exc:
        await services.get_produk_by_ref(session, "tidak-ada")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_produk_out_exposes_the_slug(session):
    p = await services.create_produk(session, ProdukIn(nama="Beras 5kg", harga=Decimal("65000")))
    assert services.produk_out(p)["slug"] == "beras-5kg"


@pytest.mark.asyncio
async def test_erp_publish_gets_a_slug_too(session):
    p, dibuat = await services.upsert_produk_dari_erp(
        session, erp_produk_id="e1", nama="Kopi 250g", deskripsi="", harga=Decimal("50000"), stok=3,
        platform_asal=None, foto_key=None, aktif=True,
    )
    assert dibuat and p.slug == "kopi-250g"


@pytest.mark.asyncio
async def test_backfill_fills_missing_slugs_oldest_first_and_skips_taken_ones(session):
    session.add(ProdukStore(nama="Teh Hijau", slug="teh-hijau", harga=1, stok=1))
    old = ProdukStore(nama="Teh Hijau", harga=1, stok=1)  # no slug yet, "teh-hijau" is taken
    session.add(old)
    await session.flush()
    new = ProdukStore(nama="Gula", harga=1, stok=1)
    session.add(new)
    await session.flush()
    assert await backfill_slugs(session) == 2
    assert old.slug == "teh-hijau-2" and new.slug == "gula"
    assert await backfill_slugs(session) == 0  # idempotent


@pytest.mark.asyncio
async def test_public_catalog_heals_missing_slugs_on_read(session):
    session.add(ProdukStore(nama="Teh Hijau", harga=1, stok=1))
    session.add(ProdukStore(nama="Teh Hijau", harga=1, stok=1))
    await session.flush()
    rows = await services.list_produk_publik(session)
    assert sorted(p.slug for p in rows) == ["teh-hijau", "teh-hijau-2"]
    # Already healed: a second read changes nothing and does not touch the slugs again.
    assert sorted(p.slug for p in await services.list_produk_publik(session)) == ["teh-hijau", "teh-hijau-2"]


@pytest.mark.asyncio
async def test_slug_lookup_finds_a_product_that_predates_slugs(session):
    session.add(ProdukStore(nama="Kopi Gayo", harga=1, stok=1))
    await session.flush()
    assert (await services.get_produk_by_ref(session, "kopi-gayo")).nama == "Kopi Gayo"
