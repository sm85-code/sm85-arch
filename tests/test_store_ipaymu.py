"""iPaymu adapter: request signing, payment creation and webhook verification (network replaced by a fake)."""
import hashlib
import hmac
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.store.adapters.api.v1 import store_buyer_router as buyer_module
from tenants.store.modules.store.application import services
from tenants.store.modules.store.infrastructure import payment_ipaymu as ip
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import ItemPesanan, PembeliStore, PesananStore, ProdukStore


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("IPAYMU_VA", "1179000899")
    monkeypatch.setenv("IPAYMU_API_KEY", "test-key")
    monkeypatch.delenv("IPAYMU_MODE", raising=False)


def test_signature_matches_the_documented_algorithm():
    body = '{"transactionId":"78174"}'
    expected = hmac.new(
        b"test-key",
        f"POST:1179000899:{hashlib.sha256(body.encode()).hexdigest()}:test-key".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert ip.buat_signature("1179000899", "test-key", body) == expected
    assert expected == expected.lower()


def test_mode_defaults_to_sandbox_and_unknown_values_too(monkeypatch):
    assert ip._mode() == "sandbox"
    monkeypatch.setenv("IPAYMU_MODE", "production")
    assert ip._mode() == "production"
    monkeypatch.setenv("IPAYMU_MODE", "typo")
    assert ip._mode() == "sandbox"


@pytest.mark.asyncio
async def test_not_configured_is_501_and_webhook_unverifiable(monkeypatch):
    monkeypatch.delenv("IPAYMU_VA")
    with pytest.raises(HTTPException) as exc:
        await ip.create_payment(pesanan_id="p", items=[], total="1", nama_pembeli="n", email_pembeli="e@x.id",
                                notify_url="n", return_url="r", cancel_url="c")
    assert exc.value.status_code == 501
    assert await ip.verify_webhook({"trx_id": "1"}) is None


@pytest.mark.asyncio
async def test_create_payment_builds_the_request_and_reads_the_session(monkeypatch):
    seen = {}

    async def fake_post(path, body):
        seen["path"], seen["body"] = path, body
        return {"Status": 200, "Data": {"SessionID": "SID-1", "Url": "https://sandbox.ipaymu.com/payment/SID-1"}}

    monkeypatch.setattr(ip, "_post", fake_post)
    out = await ip.create_payment(
        pesanan_id="ord-1",
        items=[ip.ItemBayar("Kaos (XL)", 2, "50000.00"), ip.ItemBayar("Tas", 1, "30000")],
        total="145000.00",
        nama_pembeli="Budi", email_pembeli="b@x.id", telepon_pembeli="0812",
        notify_url="https://api.x/cb", return_url="https://x/p", cancel_url="https://x/c",
    )
    assert out.checkout_url.endswith("/SID-1") and out.gateway_ref == "SID-1"
    b = seen["body"]
    assert seen["path"] == "/payment" and b["referenceId"] == "ord-1" and b["amount"] == "145000"
    # 2 x 50.000 + 30.000 = 130.000; the remaining 15.000 is shown as its own line so the lines add up
    assert b["product"] == ["Kaos (XL)", "Tas", "Ongkos kirim"]
    assert b["qty"] == ["2", "1", "1"] and b["price"] == ["50000", "30000", "15000"]
    assert b["notifyUrl"] == "https://api.x/cb" and b["buyerPhone"] == "0812"


@pytest.mark.asyncio
async def test_create_payment_without_session_url_is_502(monkeypatch):
    async def fake_post(path, body):
        return {"Status": 200, "Data": {}}

    monkeypatch.setattr(ip, "_post", fake_post)
    with pytest.raises(HTTPException) as exc:
        await ip.create_payment(pesanan_id="p", items=[ip.ItemBayar("a", 1, "1000")], total="1000", nama_pembeli="n",
                                email_pembeli="e@x.id", notify_url="n", return_url="r", cancel_url="c")
    assert exc.value.status_code == 502


def _fake_check(data):
    async def fake_post(path, body):
        assert path == "/transaction" and body == {"transactionId": "184854"}
        return {"Status": 200, "Data": data}

    return fake_post


@pytest.mark.asyncio
async def test_webhook_is_verified_only_through_the_transaction_check(monkeypatch):
    monkeypatch.setattr(ip, "_post", _fake_check({"ReferenceId": "ord-1", "Status": 1, "SubTotal": 150000, "Total": 151050}))
    v = await ip.verify_webhook({"trx_id": "184854", "status": "berhasil", "reference_id": "SPOOFED"})
    assert v is not None and v.reference_id == "ord-1"  # the reference comes from iPaymu, not from the notify
    assert v.lunas and v.jumlah == Decimal("150000")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code,lunas", [(1, True), (6, True), (7, True), (0, False), (-2, False), (2, False)])
async def test_webhook_status_codes(monkeypatch, status_code, lunas):
    monkeypatch.setattr(ip, "_post", _fake_check({"ReferenceId": "o", "Status": status_code, "SubTotal": 1000}))
    v = await ip.verify_webhook({"trx_id": "184854"})
    assert v is not None and v.lunas is lunas


@pytest.mark.asyncio
async def test_webhook_unverifiable_cases(monkeypatch):
    assert await ip.verify_webhook({}) is None  # no transaction id
    assert await ip.verify_webhook({"trx_id": ""}) is None

    async def boom(path, body):
        raise HTTPException(status_code=502, detail="x")

    monkeypatch.setattr(ip, "_post", boom)
    assert await ip.verify_webhook({"trx_id": "184854"}) is None  # iPaymu unreachable: fail closed

    monkeypatch.setattr(ip, "_post", _fake_check({"Status": 1, "SubTotal": 1000}))  # no reference
    assert await ip.verify_webhook({"trx_id": "184854"}) is None
    monkeypatch.setattr(ip, "_post", _fake_check({"ReferenceId": "o", "Status": "abc", "SubTotal": 1000}))
    assert await ip.verify_webhook({"trx_id": "184854"}) is None


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _order(session, total="150000"):
    user = PembeliStore(nama="P", email="p@test.com", password_hash="x")
    session.add(user)
    await session.flush()
    p = PesananStore(user_id=user.id, status="menunggu_pembayaran", total=Decimal(total))
    session.add(p)
    await session.flush()
    return p


@pytest.mark.asyncio
async def test_callback_marks_the_order_paid_once_iPaymu_confirms(monkeypatch, session):
    p = await _order(session)
    monkeypatch.setattr(ip, "_post", _fake_check({"ReferenceId": p.id, "Status": 1, "SubTotal": 150000}))
    assert await buyer_module.proses_notifikasi_pembayaran({"trx_id": "184854"}, session) == {"ok": True}
    assert p.status == "dibayar"
    # iPaymu re-sends a notify until it gets 200: a second one is harmless
    assert await buyer_module.proses_notifikasi_pembayaran({"trx_id": "184854"}, session) == {"ok": True}
    assert p.status == "dibayar"


@pytest.mark.asyncio
async def test_callback_ignores_pending_and_short_payments(monkeypatch, session):
    p = await _order(session)
    monkeypatch.setattr(ip, "_post", _fake_check({"ReferenceId": p.id, "Status": 0, "SubTotal": 150000}))
    assert (await buyer_module.proses_notifikasi_pembayaran({"trx_id": "184854"}, session))["status"] == "belum_lunas"
    assert p.status == "menunggu_pembayaran"
    monkeypatch.setattr(ip, "_post", _fake_check({"ReferenceId": p.id, "Status": 1, "SubTotal": 1000}))
    await buyer_module.proses_notifikasi_pembayaran({"trx_id": "184854"}, session)
    assert p.status == "menunggu_pembayaran"  # paid less than the order total


@pytest.mark.asyncio
async def test_callback_for_unknown_order_and_forged_notify(monkeypatch, session):
    monkeypatch.setattr(ip, "_post", _fake_check({"ReferenceId": "nope", "Status": 1, "SubTotal": 1000}))
    with pytest.raises(HTTPException) as exc:
        await buyer_module.proses_notifikasi_pembayaran({"trx_id": "184854"}, session)
    assert exc.value.status_code == 404

    async def boom(path, body):
        raise HTTPException(status_code=502, detail="x")

    monkeypatch.setattr(ip, "_post", boom)
    with pytest.raises(HTTPException) as exc:
        await buyer_module.proses_notifikasi_pembayaran({"trx_id": "184854", "status": "berhasil"}, session)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_start_payment_sends_absolute_urls_and_items(monkeypatch, session):
    monkeypatch.setenv("API_PUBLIC_URL", "https://api.example.com/")
    monkeypatch.setenv("SITE_PUBLIC_URL", "https://example.com")
    p = await _order(session, total="100000")
    produk = ProdukStore(nama="Talenan", harga=Decimal("50000"), stok=5)
    session.add(produk)
    await session.flush()
    session.add(ItemPesanan(pesanan_id=p.id, produk_id=produk.id, nama_produk="Talenan", nama_varian="Besar",
                            harga_satuan=Decimal("50000"), qty=2, subtotal=Decimal("100000")))
    await session.flush()
    seen = {}

    async def fake_create(**kw):
        seen.update(kw)
        return ip.PembayaranResult(checkout_url="https://pay/x", gateway_ref="SID")

    monkeypatch.setattr(buyer_module, "ipaymu_create_payment", fake_create)
    user = await session.get(PembeliStore, p.user_id)
    pesanan = await services.get_pesanan(session, p.id)
    monkeypatch.setattr(buyer_module, "_pesanan_milik", lambda *a, **k: _async(pesanan))
    out = await buyer_module.mulai_pembayaran(p.id, session=session, user=user)
    assert out == {"checkout_url": "https://pay/x"}
    assert seen["notify_url"] == "https://api.example.com/api/store/buyer/payment/callback"
    assert seen["return_url"] == f"https://example.com/pesanan/{p.id}"
    assert seen["items"][0].nama == "Talenan (Besar)" and seen["items"][0].qty == 2
    assert (await session.get(PesananStore, p.id)).gateway_ref == "SID"


async def _async(value):
    return value


def test_payload_reader_accepts_json_and_form():
    import asyncio

    class Req:
        def __init__(self, ctype, js=None, form=None):
            self.headers = {"content-type": ctype}
            self._js, self._form = js, form

        async def json(self):
            return self._js

        async def form(self):
            return self._form

    r = asyncio.run(buyer_module._baca_payload(Req("application/json", js={"trx_id": "1"})))
    assert r == {"trx_id": "1"}
    r = asyncio.run(buyer_module._baca_payload(Req("application/x-www-form-urlencoded", form={"trx_id": "2"})))
    assert r == {"trx_id": "2"}
    assert asyncio.run(buyer_module._baca_payload(Req("application/json", js=["x"]))) == {}
