"""Biteship rates: request building, option parsing and the server-side shipping price (network replaced by a fake)."""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.store.adapters.api.v1 import store_buyer_router as buyer_module
from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import PengirimanIn
from tenants.store.modules.store.infrastructure import shipping_biteship as bs
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import ItemPesanan, PembeliStore, PesananStore, ProdukStore

ITEM = bs.ItemKirim(nama="Rak", nilai=100000, qty=2, berat_gram=800, panjang_cm=30)
PRICING = {
    "success": True,
    "pricing": [
        {"courier_code": "JNT", "courier_name": "J&T", "courier_service_code": "ez", "courier_service_name": "EZ", "price": 21000, "duration": "2 - 3 days"},
        {"courier_code": "jne", "courier_name": "JNE", "courier_service_code": "reg", "courier_service_name": "Reguler", "price": 18000.0, "duration": "1 - 2 days"},
        {"courier_code": "jne"},  # malformed row is skipped
    ],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BITESHIP_API_KEY", "biteship_test.key")
    monkeypatch.delenv("BITESHIP_ORIGIN_POSTAL", raising=False)


@pytest.mark.asyncio
async def test_not_configured_is_501(monkeypatch):
    monkeypatch.delenv("BITESHIP_API_KEY")
    assert not bs.aktif()
    with pytest.raises(HTTPException) as exc:
        await bs.cek_ongkir(kode_pos_tujuan="40115", items=[ITEM])
    assert exc.value.status_code == 501


@pytest.mark.asyncio
async def test_rates_request_and_sorted_options(monkeypatch):
    seen = {}

    def fake(kode_pos, items):
        seen["kode_pos"], seen["items"] = kode_pos, items
        return PRICING

    monkeypatch.setattr(bs, "_rates_sync", fake)
    options = await bs.cek_ongkir(kode_pos_tujuan="40115", items=[ITEM])
    assert [(o.kurir, o.layanan, o.ongkir) for o in options] == [("jne", "reg", "18000"), ("jnt", "ez", "21000")]
    assert options[0].layanan_nama == "Reguler" and options[0].estimasi == "1 - 2 days"
    assert seen["kode_pos"] == "40115"


def test_item_payload_and_body(monkeypatch):
    assert bs._item_payload(ITEM) == {"name": "Rak", "value": 100000, "quantity": 2, "weight": 800, "length": 30}
    sent = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return PRICING

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, json=json, headers=headers)
        return Resp()

    monkeypatch.setattr(bs.requests, "post", fake_post)
    bs._rates_sync("40115", [ITEM])
    assert sent["url"].endswith("/rates/couriers")
    assert sent["headers"] == {"Authorization": "biteship_test.key"}
    assert sent["json"]["origin_postal_code"] == 46396 and sent["json"]["destination_postal_code"] == 40115
    assert "jne" in sent["json"]["couriers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kode_pos", ["", "4011", "abcde", "401155"])
async def test_bad_postal_code_is_400(kode_pos):
    with pytest.raises(HTTPException) as exc:
        await bs.cek_ongkir(kode_pos_tujuan=kode_pos, items=[ITEM])
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_upstream_error_and_empty_result(monkeypatch):
    def refuse(kode_pos, items):
        raise HTTPException(status_code=400, detail="x")

    monkeypatch.setattr(bs, "_rates_sync", refuse)
    with pytest.raises(HTTPException) as exc:
        await bs.cek_ongkir(kode_pos_tujuan="40115", items=[ITEM])
    assert exc.value.status_code == 400
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i: {"success": True, "pricing": []})
    with pytest.raises(HTTPException) as exc:
        await bs.cek_ongkir(kode_pos_tujuan="40115", items=[ITEM])
    assert exc.value.status_code == 404


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _order(session):
    user = PembeliStore(nama="P", email="p@test.com", password_hash="x")
    produk = ProdukStore(nama="Rak", harga=Decimal("100000"), stok=5, berat_gram=0)
    session.add_all([user, produk])
    await session.flush()
    pesanan = PesananStore(user_id=user.id, status="menunggu_pembayaran", total=Decimal("200000"))
    session.add(pesanan)
    await session.flush()
    session.add(ItemPesanan(pesanan_id=pesanan.id, produk_id=produk.id, nama_produk="Rak", harga_satuan=Decimal("100000"), qty=2, subtotal=Decimal("200000")))
    await session.flush()
    return user, await services.get_pesanan(session, pesanan.id)


def _form(**kw):
    base = dict(kurir="jne", layanan="reg", nama_penerima="A", telepon_penerima="081234567890", alamat_tujuan="Jl. X 1", kode_pos_tujuan="40115", ongkir=Decimal("1"))
    base.update(kw)
    return PengirimanIn(**base)


@pytest.mark.asyncio
async def test_missing_weight_uses_the_default_and_order_lines_feed_the_rates(session, monkeypatch):
    monkeypatch.setenv("BITESHIP_DEFAULT_WEIGHT_GRAM", "700")
    _user, pesanan = await _order(session)
    items = await services.item_kirim_pesanan(session, pesanan)
    assert [(i.qty, i.berat_gram, i.nilai) for i in items] == [(2, 700, 100000)]


@pytest.mark.asyncio
async def test_shipping_price_comes_from_biteship_not_from_the_buyer(session, monkeypatch):
    user, pesanan = await _order(session)
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i: PRICING)
    out = await buyer_module.isi_alamat_pengiriman(pesanan.id, _form(ongkir=Decimal("1")), session, user)
    assert Decimal(out["ongkir"]) == 18000 and out["layanan_nama"] == "Reguler" and out["kurir"] == "jne"
    assert (await services.get_pesanan(session, pesanan.id)).total == Decimal("218000")


@pytest.mark.asyncio
async def test_unknown_service_is_rejected_and_nothing_is_saved(session, monkeypatch):
    user, pesanan = await _order(session)
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i: PRICING)
    with pytest.raises(HTTPException) as exc:
        await buyer_module.isi_alamat_pengiriman(pesanan.id, _form(layanan="yes"), session, user)
    assert exc.value.status_code == 400
    assert (await services.get_pesanan(session, pesanan.id)).total == Decimal("200000")


@pytest.mark.asyncio
async def test_without_biteship_the_old_behaviour_stays(session, monkeypatch):
    monkeypatch.delenv("BITESHIP_API_KEY")
    user, pesanan = await _order(session)
    out = await buyer_module.isi_alamat_pengiriman(pesanan.id, _form(kurir="Menunggu konfirmasi", layanan="-"), session, user)
    assert Decimal(out["ongkir"]) == 0
    assert (await services.get_pesanan(session, pesanan.id)).total == Decimal("200000")


ORDER_OK = {"success": True, "id": "bo-1", "status": "confirmed", "courier": {"tracking_id": "trk-1", "waybill_id": "JNE123"}}


async def _dengan_pengiriman(session, status="dibayar"):
    user, pesanan = await _order(session)
    await services.buat_pengiriman_lokal(session, pesanan.id, _form(ongkir=Decimal("18000")))
    pesanan.status = status
    await session.flush()
    return user, pesanan


@pytest.mark.asyncio
async def test_booking_the_courier_saves_the_waybill_and_moves_the_order_on(session, monkeypatch):
    _user, pesanan = await _dengan_pengiriman(session)
    sent = {}

    def fake(method, path, body=None):
        sent.update(method=method, path=path, body=body)
        return ORDER_OK

    monkeypatch.setattr(bs, "_request_sync", fake)
    out = await services.buat_order_biteship(session, pesanan.id)
    assert out.tracking_id == "JNE123" and out.biteship_order_id == "bo-1" and out.biteship_tracking_id == "trk-1"
    assert sent["method"] == "POST" and sent["path"] == "/orders"
    assert sent["body"]["courier_company"] == "jne" and sent["body"]["courier_type"] == "reg"
    assert sent["body"]["destination_postal_code"] == 40115 and sent["body"]["items"][0]["quantity"] == 2
    assert (await services.get_pesanan(session, pesanan.id)).status == "diproses"


@pytest.mark.asyncio
async def test_booking_is_refused_for_unpaid_orders_and_a_second_time(session, monkeypatch):
    monkeypatch.setattr(bs, "_request_sync", lambda m, p, b=None: ORDER_OK)
    _user, belum = await _dengan_pengiriman(session, status="menunggu_pembayaran")
    with pytest.raises(HTTPException) as exc:
        await services.buat_order_biteship(session, belum.id)
    assert exc.value.status_code == 409
    belum.status = "dibayar"
    await services.buat_order_biteship(session, belum.id)
    with pytest.raises(HTTPException) as exc:
        await services.buat_order_biteship(session, belum.id)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_tracking_walks_the_shipment_and_order_status_forward(session, monkeypatch):
    _user, pesanan = await _dengan_pengiriman(session)
    monkeypatch.setattr(bs, "_request_sync", lambda m, p, b=None: ORDER_OK)
    await services.buat_order_biteship(session, pesanan.id)
    riwayat = {
        "success": True,
        "status": "delivered",
        "waybill_id": "JNE123",
        "history": [
            {"status": "picked", "note": "Diambil", "updated_at": "2026-10-04T08:00:00Z"},
            {"status": "delivered", "note": "Diterima", "updated_at": "2026-10-05T09:00:00Z"},
        ],
    }
    monkeypatch.setattr(bs, "_request_sync", lambda m, p, b=None: riwayat)
    out = await services.lacak_pengiriman(session, pesanan.id)
    assert out["status"] == "diterima" and out["riwayat"][0]["catatan"] == "Diterima"
    assert (await services.get_pesanan(session, pesanan.id)).status == "selesai"


@pytest.mark.asyncio
async def test_tracking_without_a_booking_is_404(session):
    _user, pesanan = await _dengan_pengiriman(session)
    with pytest.raises(HTTPException) as exc:
        await services.lacak_pengiriman(session, pesanan.id)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_can_still_add_shipping_to_a_paid_order_without_changing_the_total(session):
    user, pesanan = await _order(session)
    pesanan.status = "dibayar"
    await session.flush()
    await services.buat_pengiriman_lokal(session, pesanan.id, _form(ongkir=Decimal("18000")))
    assert (await services.get_pesanan(session, pesanan.id)).total == Decimal("200000")
