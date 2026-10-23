"""Belanja Supplier: a third Operasional-style page (jenis "supplier") with its own permission and default category."""
import pytest
from sqlalchemy import select

from tenants.marketplace_erp.modules.marketplace_erp.application import akses
from tenants.marketplace_erp.modules.marketplace_erp.application import kategori as kat
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KategoriTransaksi, PengaturanUi, RoleHalaman

from tests.test_marketplace_erp_kategori import client_for, session  # noqa: F401


@pytest.mark.asyncio
async def test_roles_with_operasional_umum_get_belanja_supplier_once(session):  # noqa: F811
    izin = {(r.role_id, r.halaman): r.tingkat for r in (await session.scalars(select(RoleHalaman))).all()}
    for (role, halaman), tingkat in list(izin.items()):
        if halaman == "operasional_umum":
            assert izin[(role, "belanja_supplier")] == tingkat
    # removed by the admin: not given back
    baris = await session.get(RoleHalaman, ("staf_operasional", "belanja_supplier"))
    await session.delete(baris)
    await session.commit()
    await akses.pastikan_bawaan(session)
    assert await session.get(RoleHalaman, ("staf_operasional", "belanja_supplier")) is None
    assert await session.get(PengaturanUi, akses.PENANDA_SUPPLIER) is not None


@pytest.mark.asyncio
async def test_page_works_like_operasional_and_flows_to_arus_kas(session):  # noqa: F811
    async with client_for("staf_operasional", session) as c:
        data = (await c.get("/operasional/supplier", params={"periode": "2026-10"})).json()
        assert [k["id"] for k in data["kategori"]] == ["belanja_supplier"]
        r = await c.post("/operasional/supplier", json={"kategori": "belanja_supplier", "tanggal": "2026-10-05", "uraian": "Rak besi", "jumlah": "150000"})
        assert r.status_code == 201
        # a category of another page is refused here
        r = await c.post("/operasional/supplier", json={"kategori": "ongkir", "tanggal": "2026-10-05", "uraian": "x", "jumlah": "1"})
        assert r.status_code == 422
    async with client_for("staff", session) as c:
        assert (await c.get("/operasional/supplier", params={"periode": "2026-10"})).status_code == 403
    from tenants.marketplace_erp.modules.marketplace_erp.application import operasional as ops
    arus = await ops.untuk_arus_kas(session, "2026-10")
    assert {"tanggal": "2026-10-05", "kategori": "belanja_supplier", "jumlah": "150000.00", "uraian": "Rak besi"} in arus["items"]


@pytest.mark.asyncio
async def test_existing_installation_gets_the_new_category_once(session):  # noqa: F811
    await kat.pastikan_bawaan(session)
    k = await session.get(KategoriTransaksi, "belanja_supplier")
    await session.delete(k)
    await session.commit()
    await kat.pastikan_bawaan(session)
    k = await session.get(KategoriTransaksi, "belanja_supplier")
    assert (k.arah, k.kelompok, k.operasional) == ("keluar", kat.HPP, "supplier")


@pytest.mark.asyncio
async def test_tidy_up_renames_once_and_orders(session):  # noqa: F811
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KategoriTransaksi as K
    await kat.pastikan_bawaan(session)
    (await session.get(K, "non_kayu")).nama = "Belanja Supplier"
    (await session.get(K, "belanja_supplier")).nama = "Belanja supplier"
    marker = await session.get(PengaturanUi, kat.PENANDA_RAPI)
    if marker:
        await session.delete(marker)
    await session.commit()
    await kat.pastikan_bawaan(session)
    assert (await session.get(K, "non_kayu")).nama == "Pembelian Produk (otomatis ERP)"
    assert (await session.get(K, "belanja_supplier")).nama == "Belanja Supplier"
    keluar = (await session.scalars(select(K).where(K.arah == "keluar").order_by(K.urut))).all()
    assert [k.id for k in keluar][:3] == ["non_kayu", "belanja_supplier", "tukang"] and keluar[-1].id == "bukan_keluar"


@pytest.mark.asyncio
async def test_duplicate_names_ignore_case_and_spaces_and_arus_kas_rows_count(session):  # noqa: F811
    async with client_for("admin", session) as c:
        r = await c.post("/kategori-transaksi", json={"arah": "keluar", "nama": "belanja  SUPPLIER", "kelompok": "hpp"})
        assert r.status_code == 409
        items = {k["id"]: k for k in (await c.get("/kategori-transaksi")).json()["items"]}
    assert items["non_kayu"]["dipakai_arus_kas"] == 1 and items["lain_keluar"]["dipakai_arus_kas"] == 2
