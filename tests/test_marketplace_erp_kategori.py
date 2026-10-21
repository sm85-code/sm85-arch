from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.kategori_router import router as kategori_router
from tenants.marketplace_erp.adapters.api.v1.operasional_router import router as operasional_router
from tenants.marketplace_erp.modules.marketplace_erp.application import akses
from tenants.marketplace_erp.modules.marketplace_erp.application import kategori as kat
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import (
    MarketplaceErpBase,
    get_db_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import RoleHalaman


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        await akses.pastikan_bawaan(s)
        await s.commit()
        yield s
    await engine.dispose()


def client_for(role, session):
    app = FastAPI()
    app.include_router(kategori_router)
    app.include_router(operasional_router)

    async def db():
        yield session

    async def current():
        return SimpleNamespace(id=None, role=role)

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = current
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def by_id(data):
    return {k["id"]: k for k in data["items"]}


@pytest.mark.asyncio
async def test_a_fresh_installation_starts_with_the_known_categories(session):
    async with client_for("admin", session) as c:
        data = (await c.get("/kategori-transaksi")).json()
    items = by_id(data)
    assert len(data["items"]) == len(kat.BAWAAN) == 21
    assert [k["id"] for k in data["items"] if k["arah"] == "masuk"][:3] == ["saldo_toko", "produk", "cat"]
    assert items["gaji"]["kelompok"] == "Beban operasional" and items["gaji"]["operasional"] is None
    assert items["perl_packing"]["kelompok"] == "Harga pokok penjualan" and items["perl_packing"]["operasional"] == "packing"
    assert items["atk_eceran"]["operasional"] == "packing" and items["atk"]["operasional"] == "umum"
    assert items["bukan_masuk"]["kelompok"] == "" and items["bukan_keluar"]["sistem"] is True
    assert data["kelompok"] == {"masuk": ["Pemasukan"], "keluar": ["Harga pokok penjualan", "Beban operasional"]}


@pytest.mark.asyncio
async def test_everybody_reads_but_only_who_may_changes(session):
    for role in ("admin", "owner", "staff", "staf_keuangan", "staf_produksi"):
        async with client_for(role, session) as c:
            assert (await c.get("/kategori-transaksi")).status_code == 200, role
    baru = {"arah": "keluar", "nama": "Biaya Parkir", "kelompok": "Beban operasional"}
    for role in ("owner", "staff", "staf_keuangan"):
        async with client_for(role, session) as c:
            assert (await c.post("/kategori-transaksi", json=baru)).status_code == 403, role
            assert (await c.put("/kategori-transaksi/gaji", json={"nama": "Gaji", "kelompok": "Beban operasional", "aktif": True})).status_code == 403
            assert (await c.put("/kategori-transaksi/urutan", json={"arah": "keluar", "ids": []})).status_code == 403
    # an admin can hand it to a role in the role settings
    session.add(RoleHalaman(role_id="staf_keuangan", halaman="kategori_transaksi", tingkat="tulis"))
    await session.commit()
    async with client_for("staf_keuangan", session) as c:
        assert (await c.post("/kategori-transaksi", json=baru)).status_code == 201


@pytest.mark.asyncio
async def test_new_category_is_placed_where_the_admin_says(session):
    async with client_for("admin", session) as c:
        made = await c.post("/kategori-transaksi", json={"arah": "keluar", "nama": "Biaya Parkir", "kelompok": "Beban operasional", "operasional": "umum"})
        assert made.status_code == 201
        k = made.json()
        assert k["id"] == "biaya_parkir" and k["kelompok"] == "Beban operasional" and k["operasional"] == "umum" and k["sistem"] is False and k["aktif"] is True
        assert k["urut"] == max(x["urut"] for x in by_id((await c.get("/kategori-transaksi")).json()).values() if x["arah"] == "keluar")
        masuk = (await c.post("/kategori-transaksi", json={"arah": "masuk", "nama": "Jasa Konsultasi", "kelompok": "Pemasukan"})).json()
        assert masuk["arah"] == "masuk" and masuk["operasional"] is None
        # the same name again gets refused, in any case, but is free on the other side
        assert (await c.post("/kategori-transaksi", json={"arah": "keluar", "nama": "biaya parkir", "kelompok": "Harga pokok penjualan"})).status_code == 409
        assert (await c.post("/kategori-transaksi", json={"arah": "masuk", "nama": "Biaya Parkir", "kelompok": "Pemasukan"})).status_code == 201


@pytest.mark.asyncio
async def test_placement_is_checked(session):
    async with client_for("admin", session) as c:
        post = lambda **kw: c.post("/kategori-transaksi", json={"arah": "keluar", "nama": "Baru", "kelompok": "Beban operasional", **kw})  # noqa: E731
        assert (await post(kelompok="Pemasukan")).status_code == 422  # a cash-out category cannot land under income
        assert (await post(arah="masuk", kelompok="Beban operasional")).status_code == 422
        assert (await post(arah="masuk", kelompok="Pemasukan", operasional="umum")).status_code == 422  # only expenses go on the Operasional pages
        assert (await post(operasional="gudang")).status_code == 422
        assert (await post(arah="lain")).status_code == 422
        assert (await post(nama="A")).status_code == 422
        assert (await post(nama="x" * 81)).status_code == 422


@pytest.mark.asyncio
async def test_renaming_keeps_the_id_and_what_points_at_it(session):
    async with client_for("admin", session) as c:
        entry = {"kategori": "atk", "tanggal": "2026-10-05", "uraian": "Kertas", "jumlah": "275000"}
        assert (await c.post("/operasional/umum", json=entry)).status_code == 201
        edit = {"nama": "Belanja alat tulis", "kelompok": "Beban operasional", "operasional": "umum", "aktif": True}
        assert (await c.put("/kategori-transaksi/atk", json=edit)).json()["nama"] == "Belanja alat tulis"
        page = (await c.get("/operasional/umum?periode=2026-10")).json()
        assert {"id": "atk", "label": "Belanja alat tulis", "aktif": True} in page["kategori"]
        assert [i["kategori"] for i in page["items"]] == ["atk"]  # the entry still belongs to the same category
        assert Decimal(page["ringkasan"]["per_kategori"]["atk"]) == 275000
        assert (await c.put("/kategori-transaksi/atk", json={**edit, "nama": "Gaji"})).status_code == 409  # taken by another
        assert (await c.put("/kategori-transaksi/tidak_ada", json=edit)).status_code == 404


@pytest.mark.asyncio
async def test_hiding_stops_new_entries_but_keeps_the_old_ones(session):
    async with client_for("admin", session) as c:
        entry = {"kategori": "langganan", "tanggal": "2026-10-05", "uraian": "Aplikasi", "jumlah": "450000"}
        made = (await c.post("/operasional/umum", json=entry)).json()
        edit = {"nama": "Langganan", "kelompok": "Beban operasional", "operasional": "umum", "aktif": False}
        assert (await c.put("/kategori-transaksi/langganan", json=edit)).json()["aktif"] is False
        assert (await c.post("/operasional/umum", json=entry)).status_code == 422
        page = (await c.get("/operasional/umum?periode=2026-10")).json()
        assert {"id": "langganan", "label": "Langganan", "aktif": False} in page["kategori"]  # still listed: it holds an entry
        # ... and that entry can still be corrected in its own category, but not moved into a hidden one
        assert (await c.put(f"/operasional/umum/{made['id']}", json={**entry, "jumlah": "500000"})).status_code == 200
        assert (await c.put(f"/operasional/umum/{made['id']}", json={**entry, "kategori": "langganan", "uraian": "x"})).status_code == 200
        empty = (await c.get("/operasional/umum?periode=2026-09")).json()
        assert "langganan" not in [k["id"] for k in empty["kategori"]]  # an unused hidden category is not offered at all
        await c.put("/kategori-transaksi/langganan", json={**edit, "aktif": True})
        assert (await c.post("/operasional/umum", json=entry)).status_code == 201


@pytest.mark.asyncio
async def test_moving_a_category_to_the_other_page_keeps_its_entries_visible(session):
    async with client_for("admin", session) as c:
        entry = {"kategori": "atk", "tanggal": "2026-10-05", "uraian": "Kertas", "jumlah": "100"}
        assert (await c.post("/operasional/umum", json=entry)).status_code == 201
        edit = {"nama": "Belanja ATK", "kelompok": "Beban operasional", "operasional": "packing", "aktif": True}
        assert (await c.put("/kategori-transaksi/atk", json=edit)).status_code == 200
        umum = (await c.get("/operasional/umum?periode=2026-10")).json()
        packing = (await c.get("/operasional/packing?periode=2026-10")).json()
        assert {"id": "atk", "label": "Belanja ATK", "aktif": False} in umum["kategori"] and len(umum["items"]) == 1
        assert {"id": "atk", "label": "Belanja ATK", "aktif": True} in packing["kategori"] and packing["items"] == []
        assert (await c.post("/operasional/umum", json=entry)).status_code == 422
        assert (await c.post("/operasional/packing", json=entry)).status_code == 201


@pytest.mark.asyncio
async def test_category_placement_can_be_changed_and_the_system_ones_are_protected(session):
    async with client_for("admin", session) as c:
        edit = {"nama": "Iklan", "kelompok": "Harga pokok penjualan", "operasional": None, "aktif": True}
        k = (await c.put("/kategori-transaksi/iklan", json=edit)).json()
        assert k["kelompok"] == "Harga pokok penjualan" and k["operasional"] is None
        bukan = {"nama": "Bukan pengeluaran", "kelompok": "", "operasional": None, "aktif": True}
        assert (await c.put("/kategori-transaksi/bukan_keluar", json={**bukan, "nama": "Tidak dihitung"})).json()["nama"] == "Tidak dihitung"
        assert (await c.put("/kategori-transaksi/bukan_keluar", json={**bukan, "aktif": False})).status_code == 409
        assert (await c.put("/kategori-transaksi/bukan_keluar", json={**bukan, "kelompok": "Beban operasional"})).json()["kelompok"] == ""  # lands nowhere, always


@pytest.mark.asyncio
async def test_order_can_be_changed(session):
    async with client_for("admin", session) as c:
        before = [k["id"] for k in (await c.get("/kategori-transaksi")).json()["items"] if k["arah"] == "masuk"]
        assert (await c.put("/kategori-transaksi/urutan", json={"arah": "masuk", "ids": ["cat", "produk"]})).status_code == 200
        after = [k["id"] for k in (await c.get("/kategori-transaksi")).json()["items"] if k["arah"] == "masuk"]
        assert after[:2] == ["cat", "produk"] and sorted(after) == sorted(before)  # the ones left out follow, in their old order
        assert after[2:] == [i for i in before if i not in ("cat", "produk")]
        assert (await c.put("/kategori-transaksi/urutan", json={"arah": "masuk", "ids": ["cat", "cat"]})).status_code == 422
        assert (await c.put("/kategori-transaksi/urutan", json={"arah": "masuk", "ids": ["gaji"]})).status_code == 422  # another kind
        assert (await c.put("/kategori-transaksi/urutan", json={"arah": "lain", "ids": []})).status_code == 422


@pytest.mark.asyncio
async def test_seeding_does_not_bring_back_changes(session):
    async with client_for("admin", session) as c:
        await c.put("/kategori-transaksi/gaji", json={"nama": "Gaji karyawan", "kelompok": "Beban operasional", "operasional": "umum", "aktif": True})
    await kat.pastikan_bawaan(session)  # what startup does after every deploy
    async with client_for("admin", session) as c:
        assert by_id((await c.get("/kategori-transaksi")).json())["gaji"]["nama"] == "Gaji karyawan"


@pytest.mark.asyncio
async def test_gaji_and_iklan_are_typed_in_arus_kas_not_on_the_operasional_pages(session):
    async with client_for("admin", session) as c:
        for id_ in ("gaji", "iklan"):
            k = (await c.put(f"/kategori-transaksi/{id_}", json={"nama": id_.title(), "kelompok": "Beban operasional", "operasional": "umum", "aktif": True})).json()
            assert k["operasional"] is None
            entry = {"kategori": id_, "tanggal": "2026-10-05", "jumlah": "1000", "uraian": ""}
            assert (await c.post("/operasional/umum", json=entry)).status_code == 422


@pytest.mark.asyncio
async def test_an_installation_that_still_offers_gaji_and_iklan_is_corrected_at_startup(session):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KategoriTransaksi

    await kat.pastikan_bawaan(session)
    for id_ in ("gaji", "iklan"):
        (await session.get(KategoriTransaksi, id_)).operasional = "umum"
    await session.commit()
    await kat.pastikan_bawaan(session)
    assert (await session.get(KategoriTransaksi, "gaji")).operasional is None
    assert (await session.get(KategoriTransaksi, "iklan")).operasional is None


@pytest.mark.asyncio
async def test_arus_kas_gets_the_entries_per_date_and_category_without_gaji_and_iklan(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application import operasional as ops
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import TransaksiOperasional

    await kat.pastikan_bawaan(session)
    for jenis, kategori, tanggal, jumlah in (
        ("umum", "atk", "2026-10-05", "100"), ("umum", "atk", "2026-10-05", "50"), ("umum", "ongkir", "2026-10-06", "75.50"),
        ("packing", "perl_packing", "2026-10-06", "20"), ("umum", "gaji", "2026-10-07", "999"), ("umum", "iklan", "2026-10-07", "999"),
        ("umum", "atk", "2026-11-01", "5"),
    ):
        session.add(TransaksiOperasional(jenis=jenis, kategori=kategori, tanggal=date.fromisoformat(tanggal), uraian="", jumlah=Decimal(jumlah)))
    await session.commit()
    data = await ops.untuk_arus_kas(session, "2026-10")
    assert data["items"] == [
        {"tanggal": "2026-10-05", "kategori": "atk", "jumlah": "150.00"},
        {"tanggal": "2026-10-06", "kategori": "ongkir", "jumlah": "75.50"},
        {"tanggal": "2026-10-06", "kategori": "perl_packing", "jumlah": "20.00"},
    ]


@pytest.mark.asyncio
async def test_groups_have_ids_names_can_change_and_groups_can_be_added(session):
    async with client_for("admin", session) as c:
        data = (await c.get("/kategori-transaksi")).json()
        assert data["kelompok_daftar"]["keluar"] == [{"id": "hpp", "nama": "Harga pokok penjualan"}, {"id": "beban", "nama": "Beban operasional"}]
        assert by_id(data)["tukang"]["kelompok_id"] == "hpp" and by_id(data)["tukang"]["kelompok"] == "Harga pokok penjualan"
        # the name changes, the id (and everything filed under it) stays
        assert (await c.put("/kategori-transaksi/kelompok/hpp", json={"nama": "Biaya langsung"})).json()["nama"] == "Biaya langsung"
        after = (await c.get("/kategori-transaksi")).json()
        assert by_id(after)["tukang"]["kelompok_id"] == "hpp" and by_id(after)["tukang"]["kelompok"] == "Biaya langsung"
        assert after["kelompok"]["keluar"][0] == "Biaya langsung"
        # a new group takes categories by id; the old name text is still understood
        new = (await c.post("/kategori-transaksi/kelompok", json={"arah": "keluar", "nama": "Biaya lain"})).json()
        assert new["id"].startswith("kel_")
        put = {"nama": "Tukang", "kelompok": new["id"], "operasional": None, "aktif": True}
        assert (await c.put("/kategori-transaksi/tukang", json=put)).json()["kelompok_id"] == new["id"]
        put["kelompok"] = "Beban operasional"
        assert (await c.put("/kategori-transaksi/tukang", json=put)).json()["kelompok_id"] == "beban"
        # names stay unique within a kind, and a group of the other kind is not accepted
        assert (await c.post("/kategori-transaksi/kelompok", json={"arah": "keluar", "nama": "biaya lain"})).status_code == 409
        assert (await c.put("/kategori-transaksi/kelompok/beban", json={"nama": "Biaya langsung"})).status_code == 409
        put["kelompok"] = "pemasukan"
        assert (await c.put("/kategori-transaksi/tukang", json=put)).status_code == 422
        assert (await c.put("/kategori-transaksi/kelompok/tidak_ada", json={"nama": "X Y"})).status_code == 404


@pytest.mark.asyncio
async def test_categories_saved_with_a_group_name_are_pointed_at_its_id(session):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KategoriTransaksi

    await kat.pastikan_bawaan(session)
    (await session.get(KategoriTransaksi, "tukang")).kelompok = "Harga pokok penjualan"
    await session.commit()
    await kat.pastikan_bawaan(session)
    assert (await session.get(KategoriTransaksi, "tukang")).kelompok == "hpp"

