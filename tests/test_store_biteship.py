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

    def fake(kode_pos, items, cod_nilai=0, pengaturan=None):
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
    def refuse(kode_pos, items, cod_nilai=0, pengaturan=None):
        raise HTTPException(status_code=400, detail="x")

    monkeypatch.setattr(bs, "_rates_sync", refuse)
    with pytest.raises(HTTPException) as exc:
        await bs.cek_ongkir(kode_pos_tujuan="40115", items=[ITEM])
    assert exc.value.status_code == 400
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i, c=0, p=None: {"success": True, "pricing": []})
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
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i, c=0, p=None: PRICING)
    out = await buyer_module.isi_alamat_pengiriman(pesanan.id, _form(ongkir=Decimal("1")), session, user)
    assert Decimal(out["ongkir"]) == 18000 and out["layanan_nama"] == "Reguler" and out["kurir"] == "jne"
    assert (await services.get_pesanan(session, pesanan.id)).total == Decimal("218000")


@pytest.mark.asyncio
async def test_unknown_service_is_rejected_and_nothing_is_saved(session, monkeypatch):
    user, pesanan = await _order(session)
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i, c=0, p=None: PRICING)
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
    assert out["status"] == "diterima" and out["riwayat"][0]["catatan"] == "Paket sudah diterima" and out["riwayat"][0]["catatan_asli"] == "Diterima"
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


@pytest.mark.asyncio
async def test_seller_can_switch_courier_before_booking_and_the_paid_shipping_stays(session, monkeypatch):
    _user, pesanan = await _dengan_pengiriman(session)
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i, c=0, p=None: PRICING)
    opsi = await services.opsi_kurir_pesanan(session, pesanan.id)
    assert {(o.kurir, o.layanan) for o in opsi} == {("jne", "reg"), ("jnt", "ez")}
    out = await services.ganti_kurir(session, pesanan.id, "JNT", "ez")
    assert (out.kurir, out.layanan, out.layanan_nama) == ("jnt", "ez", "EZ")
    assert out.ongkir == Decimal("18000")
    assert (await services.get_pesanan(session, pesanan.id)).total == Decimal("218000")  # items + shipping paid
    with pytest.raises(HTTPException) as exc:
        await services.ganti_kurir(session, pesanan.id, "jne", "nope")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_courier_can_be_switched_after_a_failed_booking_but_not_a_working_one(session, monkeypatch):
    _user, pesanan = await _dengan_pengiriman(session)
    monkeypatch.setattr(bs, "_request_sync", lambda m, p, b=None: ORDER_OK)
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i, c=0, p=None: PRICING)
    await services.buat_order_biteship(session, pesanan.id)
    with pytest.raises(HTTPException) as exc:
        await services.ganti_kurir(session, pesanan.id, "jnt", "ez")
    assert exc.value.status_code == 409
    pengiriman = await services.get_pengiriman(session, pesanan.id)
    pengiriman.status = "bermasalah"
    await session.flush()
    out = await services.ganti_kurir(session, pesanan.id, "jnt", "ez")
    assert out.biteship_order_id is None and out.status == "menunggu_pickup" and out.kurir == "jnt"
    rebooked = await services.buat_order_biteship(session, pesanan.id)
    assert rebooked.biteship_order_id == "bo-1"


@pytest.mark.asyncio
async def test_switching_courier_needs_a_paid_order(session, monkeypatch):
    _user, pesanan = await _dengan_pengiriman(session, status="menunggu_pembayaran")
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i, c=0, p=None: PRICING)
    with pytest.raises(HTTPException) as exc:
        await services.opsi_kurir_pesanan(session, pesanan.id)
    assert exc.value.status_code == 409


COD_PRICING = {
    "success": True,
    "pricing": [
        {"courier_code": "jne", "courier_name": "JNE", "courier_service_code": "reg", "courier_service_name": "Reguler", "price": 20000, "shipping_fee": 18000, "cash_on_delivery_fee": 2000, "available_for_cash_on_delivery": True, "duration": "1 - 2 days"},
        {"courier_code": "jnt", "courier_name": "J&T", "courier_service_code": "ez", "courier_service_name": "EZ", "price": 21000, "shipping_fee": 21000, "cash_on_delivery_fee": 0, "available_for_cash_on_delivery": False},
    ],
}


@pytest.mark.asyncio
async def test_cod_rates_keep_only_cod_couriers_and_split_the_fee(monkeypatch):
    seen = {}

    def fake(kode_pos, items, cod_nilai=0, pengaturan=None):
        seen["cod"] = cod_nilai
        return COD_PRICING

    monkeypatch.setattr(bs, "_rates_sync", fake)
    options = await bs.cek_ongkir(kode_pos_tujuan="40115", items=[ITEM], cod_nilai=200000)
    assert seen["cod"] == 200000
    assert [(o.kurir, o.ongkir, o.biaya_cod) for o in options] == [("jne", "18000", "2203")]  # fee on items + shipping + fee


def test_cod_fields_are_sent_to_biteship(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return COD_PRICING

    monkeypatch.setattr(bs.requests, "post", lambda url, json, headers, timeout: sent.update(json=json) or Resp())
    bs._rates_sync("40115", [ITEM], 200000)
    assert sent["json"]["destination_cash_on_delivery"] == 200000
    assert sent["json"]["destination_cash_on_delivery_type"] == "7_days"
    sent.clear()
    bs._rates_sync("40115", [ITEM])
    assert "destination_cash_on_delivery" not in sent["json"]


async def _keranjang(session, cod=True, harga="200000", qty=1):
    from tenants.store.modules.store.infrastructure.models import ItemKeranjang

    user = PembeliStore(nama="P", email="c@test.com", password_hash="x")
    produk = ProdukStore(nama="Talenan", harga=Decimal(harga), stok=5, berat_gram=500, cod=cod)
    session.add_all([user, produk])
    await session.flush()
    session.add(ItemKeranjang(user_id=user.id, produk_id=produk.id, qty=qty))
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_cod_checkout_makes_an_order_waiting_for_the_seller(session):
    user = await _keranjang(session)
    pesanan = await services.checkout(session, user.id, cod=True)
    assert pesanan.status == "menunggu_konfirmasi" and pesanan.metode_pembayaran == "cod"


@pytest.mark.asyncio
async def test_cod_is_refused_for_non_cod_products_big_orders_and_without_shipping(session, monkeypatch):
    bukan = await _keranjang(session, cod=False)
    with pytest.raises(HTTPException) as exc:
        await services.checkout(session, bukan.id, cod=True)
    assert exc.value.status_code == 400 and "Talenan" in exc.value.detail
    mahal = PembeliStore(nama="Q", email="q@test.com", password_hash="x")
    session.add(mahal)
    await session.flush()
    from tenants.store.modules.store.infrastructure.models import ItemKeranjang

    produk = ProdukStore(nama="Lemari", harga=Decimal("600000"), stok=5, cod=True)
    session.add(produk)
    await session.flush()
    session.add(ItemKeranjang(user_id=mahal.id, produk_id=produk.id, qty=1))
    await session.flush()
    with pytest.raises(HTTPException) as exc:
        await services.checkout(session, mahal.id, cod=True)
    assert exc.value.status_code == 400 and "500.000" in exc.value.detail
    monkeypatch.delenv("BITESHIP_API_KEY")
    ok = await _keranjang_ok(session)
    with pytest.raises(HTTPException) as exc:
        await services.checkout(session, ok.id, cod=True)
    assert exc.value.status_code == 409


async def _keranjang_ok(session):
    from tenants.store.modules.store.infrastructure.models import ItemKeranjang

    user = PembeliStore(nama="R", email="r@test.com", password_hash="x")
    produk = ProdukStore(nama="Sendok", harga=Decimal("50000"), stok=5, cod=True)
    session.add_all([user, produk])
    await session.flush()
    session.add(ItemKeranjang(user_id=user.id, produk_id=produk.id, qty=1))
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_cod_order_total_carries_shipping_and_cod_fee_and_goes_through_confirmation(session, monkeypatch):
    user = await _keranjang(session)
    monkeypatch.setattr(bs, "_rates_sync", lambda k, i, c=0, p=None: COD_PRICING if c else PRICING)
    pesanan = await services.checkout(session, user.id, cod=True)
    out = await buyer_module.isi_alamat_pengiriman(pesanan.id, _form(), session, user)
    assert Decimal(out["ongkir"]) == 18000 and Decimal(out["biaya_cod"]) == 2203
    pesanan = await services.get_pesanan(session, pesanan.id)
    assert pesanan.total == Decimal("220203")  # 200000 items + 18000 shipping + 2203 COD fee (fee covers the whole amount)
    # The courier cannot be booked before the seller confirms the order.
    with pytest.raises(HTTPException) as exc:
        await services.buat_order_biteship(session, pesanan.id)
    assert exc.value.status_code == 409
    # COD orders are not paid online.
    with pytest.raises(HTTPException) as exc:
        await buyer_module.mulai_pembayaran(pesanan.id, session, user)
    assert exc.value.status_code == 409
    await services.ubah_status_pesanan(session, pesanan.id, "diproses")
    sent = {}

    def fake(method, path, body=None):
        sent.update(body=body)
        return ORDER_OK

    monkeypatch.setattr(bs, "_request_sync", fake)
    await services.buat_order_biteship(session, pesanan.id)
    assert sent["body"]["destination_cash_on_delivery"] == 220203


@pytest.mark.asyncio
async def test_cancelling_a_cod_order_gives_the_stock_back(session):
    user = await _keranjang(session, qty=2)
    pesanan = await services.checkout(session, user.id, cod=True)
    produk = await session.get(ProdukStore, pesanan.items[0].produk_id)
    assert produk.stok == 3
    await services.ubah_status_pesanan(session, pesanan.id, "dibatalkan")
    await session.refresh(produk)
    assert produk.stok == 5


def test_pickup_settings_validate_couriers_phone_and_postal_code():
    from pydantic import ValidationError

    from tenants.store.modules.store.application.schemas import PengaturanPengirimanPatch

    ok = PengaturanPengirimanPatch(kurir_aktif=["JNE", " jnt ", "jne"], asal_telepon="081234567890", asal_kode_pos="46396")
    assert ok.kurir_aktif == ["jne", "jnt"]
    for salah in ({"kurir_aktif": ["gojek"]}, {"asal_telepon": "abc"}, {"asal_kode_pos": "12"}):
        with pytest.raises(ValidationError):
            PengaturanPengirimanPatch(**salah)


@pytest.mark.asyncio
async def test_admin_settings_choose_couriers_and_pickup_address(session, monkeypatch):
    from tenants.store.modules.store.application.schemas import PengaturanPengirimanPatch

    out = services.pengaturan_out(
        await services.update_pengaturan_pengiriman(
            session,
            PengaturanPengirimanPatch(kurir_aktif=["jne", "jnt"], asal_nama="Toko X", asal_kode_pos="40115", asal_alamat="Jl. A 1"),
        )
    )
    assert out["kurir_aktif"] == ["jne", "jnt"] and out["asal_kode_pos"] == "40115"
    p = await services.pengaturan_kirim(session)
    sent = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return PRICING

    monkeypatch.setattr(bs.requests, "post", lambda url, json, headers, timeout: sent.update(json=json) or Resp())
    bs._rates_sync("40115", [ITEM], 0, p)
    assert sent["json"]["couriers"] == "jne,jnt" and sent["json"]["origin_postal_code"] == 40115
    assert bs._lokasi_asal(p)["origin_contact_name"] == "Toko X"
    # Nothing chosen: fall back to the environment / built-in values.
    monkeypatch.setenv("BITESHIP_COURIERS", "pos")
    assert bs._kurir_aktif(bs.PengaturanKirim()) == "pos"
    assert bs._lokasi_asal(None)["origin_postal_code"] == 46396


def test_webhook_secret_is_checked_only_when_configured(monkeypatch):
    monkeypatch.delenv("BITESHIP_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(bs, "aktif", lambda: True)
    assert not bs.webhook_sah({})
    monkeypatch.setattr(bs, "aktif", lambda: False)
    assert bs.webhook_sah({})
    monkeypatch.setenv("BITESHIP_WEBHOOK_SECRET", "s3cret")
    assert not bs.webhook_sah({})
    assert not bs.webhook_sah({"X-Webhook-Secret": "salah"})
    assert bs.webhook_sah({"X-Webhook-Secret": "s3cret"})
    monkeypatch.setenv("BITESHIP_WEBHOOK_KEY", "X-Biteship")
    assert not bs.webhook_sah({"X-Webhook-Secret": "s3cret"})
    assert bs.webhook_sah({"X-Biteship": "s3cret"})


@pytest.mark.asyncio
async def test_webhook_asks_biteship_and_moves_the_status(session, monkeypatch):
    _user, pesanan = await _dengan_pengiriman(session)
    monkeypatch.setattr(bs, "_request_sync", lambda m, p, b=None: ORDER_OK)
    await services.buat_order_biteship(session, pesanan.id)
    asked = []

    def fake(method, path, body=None):
        asked.append(path)
        return {"success": True, "status": "dropping_off", "waybill_id": "JNE123", "history": []}

    monkeypatch.setattr(bs, "_request_sync", fake)
    # The payload claims "delivered", but only Biteship's own answer counts.
    out = await services.sinkron_dari_webhook(session, {"event": "order.status", "order_id": "bo-1", "status": "delivered"})
    assert out == {"ok": True, "status": "dikirim"} and asked == ["/trackings/trk-1"]
    assert (await services.get_pesanan(session, pesanan.id)).status == "dikirim"


@pytest.mark.asyncio
async def test_webhook_ignores_unknown_orders_other_events_and_empty_bodies(session, monkeypatch):
    _user, pesanan = await _dengan_pengiriman(session)
    monkeypatch.setattr(bs, "_request_sync", lambda m, p, b=None: ORDER_OK)
    await services.buat_order_biteship(session, pesanan.id)

    def boom(method, path, body=None):
        raise AssertionError("must not call Biteship")

    monkeypatch.setattr(bs, "_request_sync", boom)
    for payload in ({}, {"event": "order.status"}, {"event": "order.status", "order_id": "unknown"},
                    {"event": "order.price", "order_id": "bo-1"}):
        assert (await services.sinkron_dari_webhook(session, payload))["diabaikan"] is True


def test_cod_fee_covers_the_whole_amount_the_courier_collects():
    # The real case: items 500000, shipping 225000, courier rate 4% (fee quoted on the items: 20000).
    fee = bs.biaya_cod_atas_total(20000, 500000, 225000)
    collected = 500000 + 225000 + fee
    assert fee == 30209 and abs(collected * 0.04 - fee) < 1  # what the courier will really charge on the total
    assert bs.biaya_cod_atas_total(0, 500000, 225000) == 0
    assert bs.biaya_cod_atas_total(2000, 3000, 10000) == 2000  # implausible rate: keep the quote


def test_courier_statuses_are_shown_in_indonesian_and_unknown_text_is_kept():
    assert services.catatan_indonesia("confirmed", "Courier order is confirmed. jne has been notified") == (
        "Pesanan dikonfirmasi, kurir diberi tahu untuk menjemput paket"
    )
    assert services.catatan_indonesia("delivered", "Delivered") == "Paket sudah diterima"
    assert services.catatan_indonesia("some_new_status", "Sorting at hub") == "Sorting at hub"
    assert services.catatan_indonesia("some_new_status", "") == "some_new_status"
    assert set(services.LABEL_STATUS_KURIR) >= {"picked", "dropping_off", "delivered", "returned", "cancelled"}


def test_free_text_courier_notes_are_translated_by_phrase():
    assert services.catatan_indonesia("", "Item is on the way to destination") == "Paket dalam perjalanan menuju tujuan"
    assert services.catatan_indonesia("whatever", "Shipment OUT FOR DELIVERY at hub") == "Paket dalam pengantaran ke penerima"
    assert services.catatan_indonesia("whatever", "Barang sedang disortir") == "Barang sedang disortir"  # unknown text kept


@pytest.mark.asyncio
async def test_label_data_has_waybill_recipient_sender_items_and_cod(session, monkeypatch):
    _user, pesanan = await _dengan_pengiriman(session)
    with pytest.raises(HTTPException) as exc:  # no waybill yet
        await services.label_pengiriman(session, pesanan.id)
    assert exc.value.status_code == 409
    monkeypatch.setattr(bs, "_request_sync", lambda m, p, b=None: ORDER_OK)
    await services.buat_order_biteship(session, pesanan.id)
    label = await services.label_pengiriman(session, pesanan.id)
    assert label["resi"] == "JNE123" and label["kurir"] == "jne" and label["cod"] == 0
    assert label["penerima"]["nama"] == "A" and label["penerima"]["kode_pos"] == "40115"
    assert label["pengirim"]["kode_pos"] == "46396" and label["pengirim"]["nama"]
    assert label["barang"] == [{"nama": "Rak", "qty": 2, "berat_gram": 500}] and label["berat_gram"] == 1000
    pesanan.metode_pembayaran = "cod"
    await session.flush()
    assert (await services.label_pengiriman(session, pesanan.id))["cod"] == int(pesanan.total)
