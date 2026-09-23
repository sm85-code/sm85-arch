"""Exercises the toko-erp (marketplace) submodule -- product copy-to-web,
order status transitions filtered by platform, chat, and the admin/erp role
guard -- against a real (SQLite, in-memory) async session sharing the same
TokoBase metadata as the toko web module, same pattern as the other
tests/test_toko_*.py files.
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.toko.modules.erp.application import services as erp_services
from tenants.toko.modules.erp.application.schemas import AkunMarketplaceIn, PercakapanERPIn, ProdukERPIn, ProdukERPPatch
from tenants.toko.modules.erp.infrastructure.models import ItemPesananERP, PesananERP
from tenants.toko.modules.toko.application import services as toko_services
from tenants.toko.modules.toko.infrastructure.auth import require_roles_toko
from tenants.toko.modules.toko.infrastructure.database import TokoBase
from tenants.toko.modules.toko.infrastructure.models import UserToko


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _make_user(session, email="pembeli@test.com", role="pembeli") -> UserToko:
    user = UserToko(nama="Pembeli Test", email=email, password_hash="x", role=role)
    session.add(user)
    await session.flush()
    return user


async def _make_akun(session, *, platform: str, nama_toko: str = "Toko Test"):
    return await erp_services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform=platform, nama_toko=nama_toko)
    )


# --- Produk ERP + copy-to-web -----------------------------------------------


@pytest.mark.asyncio
async def test_create_produk_erp_rejects_duplicate_platform_id_eksternal(session):
    akun = await _make_akun(session, platform="shopee")
    await erp_services.create_produk_erp(
        session,
        ProdukERPIn(
            platform="shopee", akun_id=akun.id, id_eksternal="SHP-1", nama="Sabun", harga=Decimal("5000"), stok=10
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.create_produk_erp(
            session,
            ProdukERPIn(platform="shopee", akun_id=akun.id, id_eksternal="SHP-1", nama="Sabun v2", harga=Decimal("6000")),
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_create_produk_erp_rejects_unknown_platform(session):
    akun = await _make_akun(session, platform="shopee")
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.create_produk_erp(
            session, ProdukERPIn(platform="tokopedia", akun_id=akun.id, id_eksternal="X-1", nama="Sabun", harga=Decimal("5000"))
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_create_produk_erp_requires_akun_id(session):
    with pytest.raises(Exception):
        ProdukERPIn(platform="shopee", id_eksternal="X-2", nama="Sabun", harga=Decimal("5000"))


@pytest.mark.asyncio
async def test_create_produk_erp_rejects_mismatched_akun_platform(session):
    akun_lazada = await _make_akun(session, platform="lazada")
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.create_produk_erp(
            session,
            ProdukERPIn(platform="shopee", akun_id=akun_lazada.id, id_eksternal="X-3", nama="Sabun", harga=Decimal("5000")),
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_copy_produk_ke_web_creates_independent_snapshot(session):
    akun = await _make_akun(session, platform="lazada")
    produk_erp = await erp_services.create_produk_erp(
        session,
        ProdukERPIn(
            platform="lazada",
            akun_id=akun.id,
            id_eksternal="LZD-9",
            nama="Kaos Polos",
            deskripsi="Katun combed 30s",
            harga=Decimal("45000.00"),
            stok=25,
            foto_url="https://example.com/kaos.jpg",
        ),
    )

    produk_toko = await erp_services.copy_produk_ke_web(session, produk_erp.id)

    # Snapshot fields copied correctly, provenance recorded.
    assert produk_toko.id != produk_erp.id
    assert produk_toko.nama == "Kaos Polos"
    assert produk_toko.deskripsi == "Katun combed 30s"
    assert produk_toko.harga == Decimal("45000.00")
    assert produk_toko.stok == 25
    assert produk_toko.foto_url == "https://example.com/kaos.jpg"
    assert produk_toko.aktif is True
    assert produk_toko.sumber_erp_produk_id == produk_erp.id
    assert produk_toko.platform_asal == "lazada"

    # Independent row: editing the web copy afterwards must not touch the
    # ERP source, and vice versa -- no live reference.
    await erp_services.update_produk_erp(session, produk_erp.id, ProdukERPPatch(nama="Kaos Polos v2", stok=1))
    await session.refresh(produk_toko)
    assert produk_toko.nama == "Kaos Polos"
    assert produk_toko.stok == 25

    produk_toko_2 = await toko_services.get_produk(session, produk_toko.id)
    produk_toko_2.stok = 999
    await session.flush()
    produk_erp_reloaded = await erp_services.get_produk_erp(session, produk_erp.id)
    assert produk_erp_reloaded.stok == 1  # unaffected by the web-side edit


@pytest.mark.asyncio
async def test_copy_produk_ke_web_unknown_id_raises_404(session):
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.copy_produk_ke_web(session, "does-not-exist")
    assert exc_info.value.status_code == 404


# --- Pesanan ERP: list/filter by platform + status transitions -------------


async def _make_pesanan_erp(session, *, platform: str, id_eksternal: str, status_: str = "unpaid") -> PesananERP:
    akun = await _make_akun(session, platform=platform, nama_toko=f"Toko {platform} {id_eksternal}")
    pesanan = PesananERP(
        platform=platform, akun_id=akun.id, id_eksternal=id_eksternal, status=status_, nama_pembeli="Budi", total=Decimal("10000")
    )
    session.add(pesanan)
    await session.flush()
    session.add(
        ItemPesananERP(
            pesanan_id=pesanan.id, nama_produk="Barang", harga_satuan=Decimal("10000"), qty=1, subtotal=Decimal("10000")
        )
    )
    await session.flush()
    await session.refresh(pesanan, attribute_names=["items"])
    return pesanan


@pytest.mark.asyncio
async def test_list_pesanan_erp_filters_by_platform(session):
    await _make_pesanan_erp(session, platform="shopee", id_eksternal="S-1")
    await _make_pesanan_erp(session, platform="lazada", id_eksternal="L-1")
    await _make_pesanan_erp(session, platform="blibli", id_eksternal="B-1")

    semua = await erp_services.list_pesanan_erp(session)
    assert len(semua) == 3

    hanya_shopee = await erp_services.list_pesanan_erp(session, platform="shopee")
    assert len(hanya_shopee) == 1
    assert hanya_shopee[0].platform == "shopee"


@pytest.mark.asyncio
async def test_list_pesanan_erp_filters_by_akun_id(session):
    p1 = await _make_pesanan_erp(session, platform="shopee", id_eksternal="S-10")
    await _make_pesanan_erp(session, platform="shopee", id_eksternal="S-11")

    hanya_akun_1 = await erp_services.list_pesanan_erp(session, akun_id=p1.akun_id)
    assert len(hanya_akun_1) == 1
    assert hanya_akun_1[0].id == p1.id


@pytest.mark.asyncio
async def test_ubah_status_pesanan_erp_valid_and_invalid_transition(session):
    pesanan = await _make_pesanan_erp(session, platform="blibli", id_eksternal="B-2", status_="unpaid")

    updated = await erp_services.ubah_status_pesanan_erp(session, pesanan.id, "to_ship")
    assert updated.status == "to_ship"

    with pytest.raises(HTTPException) as exc_info:
        await erp_services.ubah_status_pesanan_erp(session, pesanan.id, "completed")
    assert exc_info.value.status_code == 409


# --- Chat ERP (grouped inbox) -----------------------------------------------


@pytest.mark.asyncio
async def test_chat_erp_send_and_list_grouped_inbox(session):
    akun_shopee = await _make_akun(session, platform="shopee")
    akun_lazada = await _make_akun(session, platform="lazada")
    percakapan_shopee = await erp_services.get_or_create_percakapan_erp(
        session, PercakapanERPIn(platform="shopee", akun_id=akun_shopee.id, id_eksternal_pembeli="buyer-1", nama_pembeli="Ani")
    )
    percakapan_lazada = await erp_services.get_or_create_percakapan_erp(
        session, PercakapanERPIn(platform="lazada", akun_id=akun_lazada.id, id_eksternal_pembeli="buyer-2", nama_pembeli="Budi")
    )

    await erp_services.kirim_pesan_erp(session, percakapan_shopee.id, isi="Halo, pesanan saya kapan dikirim?", pengirim_admin=False)
    await erp_services.kirim_pesan_erp(session, percakapan_shopee.id, isi="Besok kami proses ya", pengirim_admin=True)

    semua = await erp_services.list_percakapan_erp(session)
    assert {p.id for p in semua} == {percakapan_shopee.id, percakapan_lazada.id}

    hanya_shopee = await erp_services.list_percakapan_erp(session, platform="shopee")
    assert len(hanya_shopee) == 1

    hanya_akun_shopee = await erp_services.list_percakapan_erp(session, akun_id=akun_shopee.id)
    assert len(hanya_akun_shopee) == 1
    assert hanya_akun_shopee[0].id == percakapan_shopee.id

    detail = await erp_services.get_percakapan_erp(session, percakapan_shopee.id)
    out = erp_services.percakapan_erp_out(detail, dengan_pesan=True)
    assert len(out["pesan"]) == 2
    assert out["pesan"][0]["pengirim_admin"] is False
    assert out["pesan"][1]["pengirim_admin"] is True
    # Admin reply flips unread_admin back off (was set True by the buyer's
    # message, then the admin's own reply clears it).
    assert detail.unread_admin is False


# --- Role guard: pembeli rejected from admin/erp endpoints ------------------


@pytest.mark.asyncio
async def test_require_roles_toko_rejects_pembeli():
    guard = require_roles_toko("admin_toko", "owner")
    pembeli = UserToko(nama="Pembeli", email="p@test.com", password_hash="x", role="pembeli")

    with pytest.raises(HTTPException) as exc_info:
        await guard(user=pembeli)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_roles_toko_allows_admin_toko():
    guard = require_roles_toko("admin_toko", "owner")
    admin = UserToko(nama="Admin", email="a@test.com", password_hash="x", role="admin_toko")

    result = await guard(user=admin)
    assert result is admin


# --- Status change -> one-way marketplace sync (product decision A) --------


@pytest.mark.asyncio
async def test_ubah_status_to_ship_attempts_adapter_push_and_still_updates_locally(session):
    """No credentials configured on the akun/adapter, so the Shopee adapter
    raises ShopeeNotConfigured (503) -- the push attempt must be soft-fail:
    the local status still moves to to_ship, and the outcome is recorded."""
    pesanan = await _make_pesanan_erp(session, platform="shopee", id_eksternal="S-SYNC-1", status_="unpaid")

    updated = await erp_services.ubah_status_pesanan_erp(session, pesanan.id, "to_ship")

    assert updated.status == "to_ship"
    assert updated.tersinkron_marketplace is False
    assert updated.catatan_sinkron
    assert "shopee" in updated.catatan_sinkron.lower() or "Shopee" in updated.catatan_sinkron


@pytest.mark.asyncio
async def test_ubah_status_to_ship_success_marks_tersinkron_true(session, monkeypatch):
    """When the adapter call actually succeeds (mocked here since real
    credentials never exist in tests), tersinkron_marketplace must be True
    and catatan_sinkron should say so."""
    from tenants.toko.modules.erp.infrastructure import erp_shopee

    async def _fake_proses_pesanan(akun, pesanan):
        return None

    monkeypatch.setattr(erp_shopee, "proses_pesanan", _fake_proses_pesanan)

    pesanan = await _make_pesanan_erp(session, platform="shopee", id_eksternal="S-SYNC-2", status_="unpaid")
    updated = await erp_services.ubah_status_pesanan_erp(session, pesanan.id, "to_ship")

    assert updated.status == "to_ship"
    assert updated.tersinkron_marketplace is True
    assert "berhasil" in updated.catatan_sinkron.lower()


@pytest.mark.asyncio
async def test_ubah_status_to_ship_dispatches_by_platform(session, monkeypatch):
    from tenants.toko.modules.erp.infrastructure import erp_lazada

    called = {}

    async def _fake_proses_pesanan(akun, pesanan):
        called["platform"] = akun.platform

    monkeypatch.setattr(erp_lazada, "proses_pesanan", _fake_proses_pesanan)

    pesanan = await _make_pesanan_erp(session, platform="lazada", id_eksternal="L-SYNC-1", status_="unpaid")
    await erp_services.ubah_status_pesanan_erp(session, pesanan.id, "to_ship")

    assert called["platform"] == "lazada"


@pytest.mark.asyncio
async def test_ubah_status_to_shipped_does_not_call_adapter(session, monkeypatch):
    """'shipped' (Kirim Pesanan) must NOT touch any adapter at all -- the
    marketplace's own logistics handles pickup automatically once to_ship
    was acknowledged."""
    from tenants.toko.modules.erp.infrastructure import erp_shopee

    def _boom(*args, **kwargs):
        raise AssertionError("proses_pesanan should not be called for 'shipped' transition")

    monkeypatch.setattr(erp_shopee, "proses_pesanan", _boom)

    pesanan = await _make_pesanan_erp(session, platform="shopee", id_eksternal="S-SYNC-3", status_="to_ship")

    updated = await erp_services.ubah_status_pesanan_erp(session, pesanan.id, "shipped")

    assert updated.status == "shipped"
    # No push attempted for this transition -- sync fields stay at default.
    assert updated.tersinkron_marketplace is False
    assert updated.catatan_sinkron is None


@pytest.mark.asyncio
async def test_pesanan_erp_out_includes_sync_fields(session):
    pesanan = await _make_pesanan_erp(session, platform="blibli", id_eksternal="B-SYNC-1", status_="unpaid")
    updated = await erp_services.ubah_status_pesanan_erp(session, pesanan.id, "to_ship")
    out = erp_services.pesanan_erp_out(updated)
    assert "tersinkron_marketplace" in out
    assert "catatan_sinkron" in out
