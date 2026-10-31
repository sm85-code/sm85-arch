"""Belanja Tukang: a third Operasional-style page (jenis "tukang") with its own permission and default category."""
import pytest
from sqlalchemy import select

from tenants.marketplace_erp.modules.marketplace_erp.application import akses
from tenants.marketplace_erp.modules.marketplace_erp.application import kategori as kat
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KategoriTransaksi, PengaturanUi, RoleHalaman

from tests.test_marketplace_erp_kategori import client_for, session  # noqa: F401


@pytest.mark.asyncio
async def test_roles_with_operasional_umum_get_belanja_tukang_once(session):  # noqa: F811
    izin = {(r.role_id, r.halaman): r.tingkat for r in (await session.scalars(select(RoleHalaman))).all()}
    for (role, halaman), tingkat in list(izin.items()):
        if halaman == "operasional_umum":
            assert izin[(role, "belanja_tukang")] == tingkat
    # removed by the admin: not given back
    baris = await session.get(RoleHalaman, ("staf_operasional", "belanja_tukang"))
    await session.delete(baris)
    await session.commit()
    await akses.pastikan_bawaan(session)
    assert await session.get(RoleHalaman, ("staf_operasional", "belanja_tukang")) is None
    assert await session.get(PengaturanUi, akses.PENANDA_TUKANG) is not None


@pytest.mark.asyncio
async def test_page_works_like_operasional_and_flows_to_arus_kas(session):  # noqa: F811
    async with client_for("staf_operasional", session) as c:
        data = (await c.get("/operasional/tukang", params={"periode": "2026-10"})).json()
        assert [k["id"] for k in data["kategori"]] == ["belanja_tukang"]
        r = await c.post("/operasional/tukang", json={"kategori": "belanja_tukang", "tanggal": "2026-10-05", "uraian": "Pak Budi", "jumlah": "150000"})
        assert r.status_code == 201
        # a category of another page is refused here
        r = await c.post("/operasional/tukang", json={"kategori": "ongkir", "tanggal": "2026-10-05", "uraian": "x", "jumlah": "1"})
        assert r.status_code == 422
    async with client_for("staff", session) as c:
        assert (await c.get("/operasional/tukang", params={"periode": "2026-10"})).status_code == 403
    from tenants.marketplace_erp.modules.marketplace_erp.application import operasional as ops
    arus = await ops.untuk_arus_kas(session, "2026-10")
    assert {"tanggal": "2026-10-05", "kategori": "belanja_tukang", "jumlah": "150000.00", "uraian": "Pak Budi"} in arus["items"]


@pytest.mark.asyncio
async def test_existing_installation_gets_the_new_category_once(session):  # noqa: F811
    await kat.pastikan_bawaan(session)
    k = await session.get(KategoriTransaksi, "belanja_tukang")
    await session.delete(k)
    await session.commit()
    await kat.pastikan_bawaan(session)
    k = await session.get(KategoriTransaksi, "belanja_tukang")
    assert (k.arah, k.kelompok, k.operasional) == ("keluar", kat.HPP, "tukang")


@pytest.mark.asyncio
async def test_october_joko_payment_keeps_its_linked_row_and_live_category(session):  # noqa: F811
    """Read-only live evidence: the backend maps JOKO correctly; routing to YONO happens in the client."""
    import json
    from datetime import date
    from decimal import Decimal

    from tenants.marketplace_erp.modules.marketplace_erp.application import operasional as ops
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingParty, TransaksiOperasional

    await kat.pastikan_bawaan(session)
    kategori = await session.get(KategoriTransaksi, "tukang")
    kategori.nama, kategori.operasional = "Belanja Tukang", "tukang"
    session.add(PengaturanUi(id="peta_arus_kas:tukang", teks=json.dumps({
        "tukang-1": "Tukang [JOKO]", "tukang-2": "Tukang [YONO]",
        "tukang-4": "Tukang [AYI]", "tukang-5": "Tukang [NUR]",
    })))
    pembayaran = [
        ("tukang-1", "JOKO WAHYONO", "1150000.00", "JOKO"),
        ("tukang-2", "CAHYONO", "3025000.00", "YONO"),
        ("tukang-4", "AI HENDARSO", "2200000.00", "AYI"),
        ("tukang-5", "AHMAD NUR ALIM", "1825000.00", "NUR"),
    ]
    for source_id, nama, jumlah, _ in pembayaran:
        session.add(ProcessingParty(kind="tukang", source_id=source_id, payload_json=json.dumps({"nama": nama})))
        session.add(TransaksiOperasional(jenis="tukang", kategori="tukang", tanggal=date(2026, 10, 31), uraian=nama, jumlah=Decimal(jumlah)))
    await session.commit()

    # Same GET used by the page, with the live category configuration rather than the fresh-install default.
    async with client_for("staf_operasional", session) as c:
        response = await c.get("/operasional/tukang", params={"periode": "2026-10"})
    assert response.status_code == 200
    assert response.json()["ringkasan"]["total"] == "8200000.00"
    arus = await ops.untuk_arus_kas(session, "2026-10")
    assert sorted(arus["items"], key=lambda x: x["uraian"]) == sorted([
        {"tanggal": "2026-10-31", "kategori": "tukang", "jumlah": jumlah, "uraian": f"Tukang [{alias}]"}
        for _, _, jumlah, alias in pembayaran
    ], key=lambda x: x["uraian"])
    # Computing the cash-flow response must leave the source descriptions and amounts intact.
    rows = (await session.scalars(select(TransaksiOperasional))).all()
    assert sorted((r.uraian, str(r.jumlah)) for r in rows) == sorted((nama, jumlah) for _, nama, jumlah, _ in pembayaran)
