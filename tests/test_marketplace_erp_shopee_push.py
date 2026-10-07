"""Shopee Push Mechanism (webhook): signature check, push log, and the order pull a valid order push starts."""
import json
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_push
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

URL = "https://api.ampelkuning.com/api/marketplace-erp/shopee/push"
KEY = "kunci-push-rahasia"
ORDER_STATUS = {"data": {"items": [], "ordersn": "220810QSK8S7BX", "status": "PROCESSED", "completed_scenario": "", "update_time": 1660123127}, "shop_id": 727720655, "code": 3, "timestamp": 1660123127}


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def _request(body: bytes, authorization: str | None, *, path="/api/marketplace-erp/shopee/push", host="api.ampelkuning.com", proto="https") -> Request:
    headers = [(b"host", host.encode()), (b"x-forwarded-proto", proto.encode())]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers, "scheme": "http", "server": (host, 80)}
    return Request(scope, receive)


def _body(obj=ORDER_STATUS) -> bytes:
    return json.dumps({**obj, "timestamp": int(datetime.now(timezone.utc).timestamp())}, separators=(",", ":")).encode()


def test_signature_is_hmac_sha256_of_url_pipe_body_and_the_diagnosis_never_holds_the_key():
    body = _body()
    sah = shopee_push.tanda_tangan(KEY, URL, body)
    ok, diag = shopee_push.verifikasi(KEY, [URL], body, sah)
    assert ok and "url|badan" in diag["cocok"] and URL in diag["cocok"]
    assert shopee_push.verifikasi(KEY, [URL], body, sah.upper())[0]  # case does not matter
    assert not shopee_push.verifikasi(KEY, [URL], body + b" ", sah)[0]  # body changed
    assert not shopee_push.verifikasi("lain", [URL], body, sah)[0]  # other key
    assert not shopee_push.verifikasi(KEY, [URL + "/x"], body, sah)[0]  # other URL including trailing slash
    ok, diag = shopee_push.verifikasi("", [URL], body, sah)
    assert not ok and diag["ada_key"] is False
    ok, diag = shopee_push.verifikasi(KEY, [URL], body, None)
    assert not ok and diag["ada_authorization"] is False
    ok, diag = shopee_push.verifikasi(KEY, [URL], body, "x" * 64)
    assert not ok and KEY not in json.dumps(diag) and len(diag["diterima"]) == 8


def test_the_diagnosis_fingerprints_the_server_key_without_revealing_it():
    ok, diag = shopee_push.verifikasi("abcdefghijklmnop", [URL], _body(), "0" * 64)
    assert not ok and diag["kunci_server"] == shopee_push.sidik_jari_kunci("abcdefghijklmnop")
    assert "cdefghijklmn" not in json.dumps(diag)
    assert "kunci_server" not in shopee_push.verifikasi("", [URL], _body(), "0" * 64)[1]


def test_non_provider_schemes_are_rejected_and_partner_key_is_only_a_fallback():
    import hashlib
    import hmac

    body = _body()
    mac = lambda key, pesan: hmac.new(key, pesan, hashlib.sha256).hexdigest()  # noqa: E731
    assert not shopee_push.verifikasi(KEY, [URL], body, shopee_push.tanda_tangan(KEY, URL + "/", body))[0]  # changed callback URL rejected
    assert not shopee_push.verifikasi(KEY, [URL], body, mac(KEY.encode(), body))[0]  # body only
    assert not shopee_push.verifikasi(KEY, [URL], body, mac(KEY.encode(), URL.encode() + body))[0]  # no pipe
    hex_key = "ab" * 16
    assert not shopee_push.verifikasi(hex_key, [URL], body, mac(bytes.fromhex(hex_key), URL.encode() + b"|" + body))[0]  # raw bytes
    # The explicit push key is authoritative; partner key only applies without it.
    sah = shopee_push.tanda_tangan("kunci-partner", URL, body)
    ok, diag = shopee_push.verifikasi(KEY, [URL], body, sah, kunci_lain={"partner_key": "kunci-partner"})
    assert not ok and "kunci-partner" not in json.dumps(diag)
    ok, diag = shopee_push.verifikasi("", [URL], body, sah, kunci_lain={"partner_key": "kunci-partner"})
    assert ok and diag["ada_key"] is True


def test_callback_url_candidates_cover_the_proxy_and_the_configured_url(monkeypatch):
    monkeypatch.delenv("SHOPEE_PUSH_URL", raising=False)
    urls = shopee_push.kandidat_url("http://internal/api/marketplace-erp/shopee/push", {"host": "api.ampelkuning.com", "x-forwarded-proto": "https"})
    assert urls[0] == URL
    monkeypatch.setenv("SHOPEE_PUSH_URL", "https://x.test/hook")
    assert shopee_push.kandidat_url("http://a/b", {"host": "h"})[0] == "https://x.test/hook"


def test_ringkas_names_the_push_kinds_and_survives_odd_bodies():
    r = shopee_push.ringkas(ORDER_STATUS)
    assert (r["kode"], r["jenis"], r["shop_id"], r["order_sn"], r["status"]) == (3, "order_status", "727720655", "220810QSK8S7BX", "PROCESSED")
    assert shopee_push.ringkas({"code": 4, "shop_id": 5, "data": {"ordersn": "A"}})["jenis"] == "order_trackingno"
    assert shopee_push.ringkas({})["kode"] is None and shopee_push.urai(b"nope") is None and shopee_push.urai(b"[1]") is None


@pytest.mark.asyncio
async def test_valid_order_push_is_logged_and_starts_the_pull(session, monkeypatch):
    monkeypatch.setenv("SHOPEE_PUSH_KEY", KEY)
    body = _body()
    latar = BackgroundTasks()
    out = await router.terima_push_shopee(_request(body, shopee_push.tanda_tangan(KEY, URL, body)), latar, session)
    assert out.status_code == 200 and out.body == b""
    assert [(t.func.__name__, t.args) for t in latar.tasks] == [("_proses_push_di_latar", ("727720655",))]
    log = await services.list_push(session)
    assert len(log) == 1 and log[0]["valid"] is True and log[0]["hasil"] == "diproses" and log[0]["order_sn"] == "220810QSK8S7BX"


@pytest.mark.asyncio
async def test_other_valid_pushes_are_only_logged(session, monkeypatch):
    monkeypatch.setenv("SHOPEE_PUSH_KEY", KEY)
    body = _body({"data": {"authorize_type": "x"}, "partner_id": 1, "code": 1, "timestamp": 1})
    latar = BackgroundTasks()
    await router.terima_push_shopee(_request(body, shopee_push.tanda_tangan(KEY, URL, body)), latar, session)
    assert latar.tasks == [] and (await services.list_push(session))[0]["hasil"] == "dicatat"


@pytest.mark.asyncio
async def test_a_wrong_signature_is_rejected_logged_with_a_diagnosis_and_starts_nothing(session, monkeypatch):
    monkeypatch.setenv("SHOPEE_PUSH_KEY", KEY)
    latar = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        await router.terima_push_shopee(_request(_body(), "0" * 64), latar, session)
    assert exc.value.status_code == 401 and latar.tasks == []
    baris = (await services.list_push(session))[0]
    assert baris["valid"] is False and baris["hasil"] == "tanda_tangan_salah" and "url_dicoba" in baris["catatan"] and KEY not in baris["catatan"]


@pytest.mark.asyncio
async def test_without_a_configured_key_nothing_is_accepted(session, monkeypatch):
    monkeypatch.delenv("SHOPEE_PUSH_KEY", raising=False)
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "")
    body = _body()
    with pytest.raises(HTTPException) as exc:
        await router.terima_push_shopee(_request(body, shopee_push.tanda_tangan(KEY, URL, body)), BackgroundTasks(), session)
    assert exc.value.status_code == 401 and (await services.list_push(session))[0]["hasil"] == "key_belum_diatur"


@pytest.mark.asyncio
async def test_a_probe_without_a_push_code_is_answered_200_logged_and_does_nothing(session, monkeypatch):
    """What the Verify button of Open Platform (or a browser) sends: reachable, but never processed."""
    monkeypatch.setenv("SHOPEE_PUSH_KEY", KEY)
    for badan in (b"", b"halo", b"[1]", b'{"hello": 1}', b'{"code": 0, "data": {}, "timestamp": 1}'):  # code 0 = Verify
        latar = BackgroundTasks()
        out = await router.terima_push_shopee(_request(badan, None), latar, session)
        assert out.status_code == 200 and out.body == b"" and latar.tasks == []
    log = await services.list_push(session)
    assert len(log) == 5 and {r["hasil"] for r in log} == {"probe"} and not any(r["valid"] for r in log)


@pytest.mark.asyncio
async def test_a_push_shaped_body_still_needs_a_valid_signature(session, monkeypatch):
    monkeypatch.setenv("SHOPEE_PUSH_KEY", KEY)
    with pytest.raises(HTTPException) as exc:
        await router.terima_push_shopee(_request(_body(), None), BackgroundTasks(), session)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_the_browser_check_says_the_receiver_is_up():
    assert (await router.cek_penerima_push_shopee())["ok"] is True


@pytest.mark.asyncio
async def test_the_push_log_is_also_bounded_by_count(session, monkeypatch):
    monkeypatch.setattr(services, "PUSH_SIMPAN_MAKS", 5)
    for _ in range(9):
        await services.catat_push(session, valid=False, ringkas={}, hasil="probe")
    assert len(await services.list_push(session)) <= 5


@pytest.mark.asyncio
async def test_the_push_log_keeps_two_weeks(session):
    from datetime import datetime, timedelta, timezone

    lama = await services.catat_push(session, valid=True, ringkas={}, hasil="dicatat")
    lama.diterima_at = datetime.now(timezone.utc) - timedelta(days=services.PUSH_SIMPAN_HARI + 1)
    await session.flush()
    await services.catat_push(session, valid=True, ringkas={}, hasil="dicatat")
    assert len(await services.list_push(session)) == 1


@pytest.mark.asyncio
async def test_push_pulls_the_shop_it_names_and_ignores_unknown_ones(session, monkeypatch):
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="T", id_toko_eksternal="727720655"))
    assert await services.sinkron_karena_push(session, "999") == "toko_tidak_dikenal"
    assert await services.sinkron_karena_push(session, "727720655") == "toko_belum_terhubung"

    akun.access_token = "at"
    dipanggil = []

    async def fake(sesi, daftar, **kw):
        dipanggil.append((daftar, kw))
        return [{"hasil": "ok"}]

    monkeypatch.setattr(services, "sinkron_semua_pesanan", fake)
    assert await services.sinkron_karena_push(session, "727720655") == "ok"
    assert dipanggil[0][0] == [akun] and dipanggil[0][1]["jeda_detik"] == 3


def test_callback_registration_and_verification_share_configured_url(monkeypatch):
    monkeypatch.setenv("SHOPEE_PUSH_URL", "https://example.test/custom-hook")
    assert shopee_push.callback_url() == shopee_push.kandidat_url("http://internal/", {})[0]
