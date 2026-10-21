from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.push_router import router
from tenants.marketplace_erp.modules.marketplace_erp.application import push_notifications as push
from tenants.marketplace_erp.modules.marketplace_erp.application import webpush
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase, get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    AkunMarketplace,
    Pesanan,
    PushSubscription,
    StaffAkunMarketplace,
    UserMarketplaceErp,
)

FCM = "https://fcm.googleapis.com/fcm/send/abc"


def test_payload_encryption_matches_the_rfc_8291_test_vector():
    sender = ec.derive_private_key(int.from_bytes(webpush.b64d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    body = webpush.encrypt(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        salt=webpush.b64d("DGv6ra1nlYgDCS1FRnbzlw"),
        ephemeral=sender,
    )
    assert webpush.b64e(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6c"
        "VPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )
    with pytest.raises(ValueError):
        webpush.encrypt(b"x" * 4000, "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4", "BTBZMqHH6r4Tts7J_aSIgg")


def test_vapid_authorization_is_a_valid_es256_token_for_the_push_service():
    public, private = webpush.generate_keys()
    header = webpush.vapid_authorization(FCM, public, private, "mailto:admin@example.com", now=1_000_000)
    assert header.startswith("vapid t=") and header.endswith(f", k={public}")
    token = header.split("t=")[1].split(",")[0]
    head, claims, signature = token.split(".")
    import json

    assert json.loads(webpush.b64d(head)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(webpush.b64d(claims)) == {"aud": "https://fcm.googleapis.com", "exp": 1_000_000 + 12 * 3600, "sub": "mailto:admin@example.com"}
    raw = webpush.b64d(signature)
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.b64d(public))
    key.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")), f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_only_real_push_services_are_accepted_as_endpoints():
    for ok in (FCM, "https://updates.push.services.mozilla.com/wpush/v2/x", "https://web.push.apple.com/x", "https://wns2-par02p.notify.windows.com/w"):
        assert webpush.endpoint_valid(ok)
    for bad in ("http://fcm.googleapis.com/x", "https://evil.example.com/x", "https://fcm.googleapis.com.evil.com/x", "https://127.0.0.1/x", "ftp://fcm.googleapis.com/x", "not a url"):
        assert not webpush.endpoint_valid(bad)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def keys(monkeypatch):
    public, private = webpush.generate_keys()
    monkeypatch.setenv("VAPID_PUBLIC_KEY", public)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:admin@example.com")
    return public


def client_for(role, session, user_id="u1"):
    app = FastAPI()
    app.include_router(router)

    async def db():
        yield session

    async def current():
        return SimpleNamespace(id=user_id, role=role)

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = current
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


SUB = {"endpoint": FCM, "keys": {"p256dh": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4", "auth": "BTBZMqHH6r4Tts7J_aSIgg"}}


@pytest.mark.asyncio
async def test_router_without_server_keys_says_off_and_refuses_subscriptions(session, monkeypatch):
    for name in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"):
        monkeypatch.delenv(name, raising=False)
    async with client_for("admin", session) as client:
        assert (await client.get("/push/konfigurasi")).json() == {"aktif": False, "kunci_publik": None}
        assert (await client.post("/push/langganan", json=SUB)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "owner", "staff"])
async def test_every_role_can_subscribe_check_and_unsubscribe_its_own_device(session, keys, role):
    session.add(UserMarketplaceErp(id="u1", nama="U", username="u1", password_hash="x", role=role))
    await session.commit()
    async with client_for(role, session) as client:
        assert (await client.get("/push/konfigurasi")).json() == {"aktif": True, "kunci_publik": keys}
        assert (await client.post("/push/status", json={"endpoint": FCM})).json() == {"aktif": False, "pesanan": False, "chat": False}
        assert (await client.post("/push/langganan", json={**SUB, "chat": False})).json() == {"aktif": True, "pesanan": True, "chat": False}
        assert (await client.post("/push/status", json={"endpoint": FCM})).json() == {"aktif": True, "pesanan": True, "chat": False}
        assert (await client.post("/push/langganan", json={**SUB, "chat": True})).status_code == 200  # same device: updated, not duplicated
        assert len((await session.scalars(select(PushSubscription))).all()) == 1
        assert (await client.post("/push/langganan", json={**SUB, "endpoint": "https://evil.example.com/x"})).status_code == 422
        assert (await client.post("/push/berhenti", json={"endpoint": FCM})).json() == {"aktif": False}
        assert (await client.post("/push/status", json={"endpoint": FCM})).json()["aktif"] is False


@pytest.mark.asyncio
async def test_a_user_cannot_see_or_remove_another_users_device(session, keys):
    for uid in ("u1", "u2"):
        session.add(UserMarketplaceErp(id=uid, nama=uid, username=uid, password_hash="x", role="owner"))
    await session.commit()
    async with client_for("owner", session, "u1") as one:
        await one.post("/push/langganan", json=SUB)
    async with client_for("owner", session, "u2") as two:
        assert (await two.post("/push/status", json={"endpoint": FCM})).json()["aktif"] is False
        await two.post("/push/berhenti", json={"endpoint": FCM})
    assert len((await session.scalars(select(PushSubscription))).all()) == 1


async def seed(session):
    session.add_all([
        UserMarketplaceErp(id="admin", nama="A", username="admin", password_hash="x", role="admin"),
        UserMarketplaceErp(id="staf1", nama="S1", username="staf1", password_hash="x", role="staff"),
        UserMarketplaceErp(id="staf2", nama="S2", username="staf2", password_hash="x", role="staff"),
        AkunMarketplace(id="toko1", platform="shopee", nama_toko="Toko Satu", id_toko_eksternal="11", access_token="t"),
        AkunMarketplace(id="toko2", platform="shopee", nama_toko="Toko Dua", id_toko_eksternal="22", access_token="t"),
    ])
    await session.flush()
    session.add_all([
        StaffAkunMarketplace(user_id="staf1", akun_id="toko1"),
        PushSubscription(id="s-admin", user_id="admin", endpoint=FCM + "/admin", p256dh="a", auth="b"),
        PushSubscription(id="s-staf1", user_id="staf1", endpoint=FCM + "/staf1", p256dh="a", auth="b"),
        PushSubscription(id="s-staf2", user_id="staf2", endpoint=FCM + "/staf2", p256dh="a", auth="b"),
        PushSubscription(id="s-nochat", user_id="admin", endpoint=FCM + "/nochat", p256dh="a", auth="b", chat=False),
    ])
    await session.commit()


def order(oid, akun="toko1", status="to_ship", mp="READY_TO_SHIP"):
    return Pesanan(id=oid, platform="shopee", id_eksternal=f"SN-{oid}", akun_id=akun, status=status, status_marketplace=mp, total=1000)


@pytest.fixture
def sent(monkeypatch, keys):
    log = []

    async def fake(subscription, payload, **kwargs):
        log.append((subscription["endpoint"].rsplit("/", 1)[1], payload))
        return 201

    monkeypatch.setattr(webpush, "send", fake)
    return log


@pytest.mark.asyncio
async def test_orders_the_backlog_is_skipped_then_each_new_order_is_pushed_once_to_those_who_may_see_the_shop(session, sent, monkeypatch):
    await seed(session)
    session.add(order("old"))
    await session.commit()
    await push.putaran(session)
    assert sent == []  # backlog recorded, not announced
    session.add(order("new1"))
    await session.commit()
    await push.putaran(session)
    assert sorted(who for who, _ in sent) == ["admin", "nochat", "staf1"]  # staf2 has no shop; both admin devices want orders
    assert all(p["title"] == "Pesanan perlu diproses" and p["url"] == "/pesanan/new1" and "Toko Satu" in p["body"] for _, p in sent)
    sent.clear()
    await push.putaran(session)
    assert sent == []  # not announced twice


@pytest.mark.asyncio
async def test_several_new_orders_are_summarised_and_other_shops_stay_private(session, sent):
    await seed(session)
    await push.putaran(session)
    session.add_all([order("a"), order("b"), order("c", akun="toko2")])
    await session.commit()
    await push.putaran(session)
    by = {who: p for who, p in sent}
    assert by["admin"]["title"] == "3 pesanan baru perlu diproses" and by["admin"]["url"] == "/pesanan"
    assert by["staf1"]["title"] == "2 pesanan baru perlu diproses" and "Toko Dua" not in by["staf1"]["body"]
    assert "staf2" not in by


@pytest.mark.asyncio
async def test_cancel_requests_are_pushed_separately_and_the_orders_switch_is_respected(session, sent):
    await seed(session)
    await push.putaran(session)
    session.add(order("c1", status="to_ship", mp="IN_CANCEL"))
    await session.commit()
    await push.putaran(session)
    assert {p["title"] for _, p in sent} == {"Permintaan pembatalan"}
    sent.clear()
    sub = await session.get(PushSubscription, "s-staf1")
    sub.pesanan = False
    session.add(order("n2"))
    await session.commit()
    await push.putaran(session)
    assert "staf1" not in {who for who, _ in sent}


@pytest.mark.asyncio
async def test_new_buyer_chats_are_pushed_once_to_chat_subscribers_of_that_shop(session, sent, monkeypatch):
    await seed(session)
    inbox = {"toko1": [{"conversation_id": "c0", "latest_message_id": "m0", "unread_count": 1, "needs_reply": True, "to_name": "Lama"}], "toko2": []}

    async def fake_inbox(_session, akun, cursor=None, unread=False):
        return {"conversations": inbox[akun.id]}

    monkeypatch.setattr(push.erp_shopee_chat, "inbox", fake_inbox)
    await push.putaran(session)
    assert sent == []  # existing unread chats are the backlog
    inbox["toko1"].append({"conversation_id": "c1", "latest_message_id": "m1", "unread_count": 2, "needs_reply": True, "to_name": "Budi"})
    inbox["toko2"].append({"conversation_id": "c2", "latest_message_id": "m2", "unread_count": 1, "needs_reply": True, "to_name": "Sari"})
    await push.putaran(session)
    by = {}
    for who, p in sent:
        by.setdefault(who, []).append(p)
    assert sorted(by) == ["admin", "staf1"]  # the second admin device turned chat off; staf2 sees no shop
    assert [p["title"] for p in by["staf1"]] == ["Chat baru · Toko Satu"] and "Budi" in by["staf1"][0]["body"] and "toko=toko1" in by["staf1"][0]["url"]
    assert sorted(p["title"] for p in by["admin"]) == ["Chat baru · Toko Dua", "Chat baru · Toko Satu"]
    sent.clear()
    await push.putaran(session)
    assert sent == []


@pytest.mark.asyncio
async def test_a_failing_shop_does_not_stop_the_others_and_gone_devices_are_removed(session, monkeypatch, keys):
    await seed(session)

    async def fake_inbox(_session, akun, cursor=None, unread=False):
        if akun.id == "toko1":
            raise RuntimeError("Shopee down")
        return {"conversations": []}

    async def gone(subscription, payload, **kwargs):
        return 410 if subscription["endpoint"].endswith("/staf1") else 201

    monkeypatch.setattr(push.erp_shopee_chat, "inbox", fake_inbox)
    monkeypatch.setattr(webpush, "send", gone)
    await push.putaran(session)
    session.add(order("z"))
    await session.commit()
    await push.putaran(session)
    left = {s.id for s in (await session.scalars(select(PushSubscription))).all()}
    assert "s-staf1" not in left and {"s-admin", "s-staf2"} <= left


@pytest.mark.asyncio
async def test_nothing_runs_without_keys_or_subscribers(session, monkeypatch):
    for name in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"):
        monkeypatch.delenv(name, raising=False)
    await push.putaran(session)  # no keys: no-op
    assert push.interval_seconds() == 60.0
    monkeypatch.setenv("PUSH_INTERVAL_DETIK", "5")
    assert push.interval_seconds() == 30.0
    monkeypatch.setenv("PUSH_INTERVAL_DETIK", "0")
    assert push.interval_seconds() == 0.0


@pytest.mark.asyncio
async def test_recipients_follow_the_role_settings(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application import akses, push_notifications
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
        PushSubscription,
        RoleHalaman,
        UserMarketplaceErp,
    )

    await akses.pastikan_bawaan(session)
    for uid, role in (("a", "admin"), ("o", "owner"), ("s", "staff"), ("k", "staf_keuangan")):
        session.add(UserMarketplaceErp(id=uid, nama=uid, username=f"user{uid}", password_hash="x", role=role))
        session.add(PushSubscription(id=f"sub-{uid}", user_id=uid, endpoint=f"https://fcm.googleapis.com/{uid}", p256dh="p", auth="a"))
    await session.commit()
    got = {sub.user_id: (allowed, halaman) for sub, allowed, halaman in await push_notifications._recipients(session)}
    assert got["a"] == (None, {"pesanan", "chat"})
    assert got["o"] == (None, {"pesanan", "chat"})
    assert got["s"] == (set(), {"pesanan", "chat"})  # assigned to no shop yet
    assert got["k"] == (None, set())  # finance staff: every shop, but no orders and no chats
    await session.delete(await session.get(RoleHalaman, ("owner", "chat")))  # an admin unticks Chat for owner
    await session.commit()
    got = {sub.user_id: halaman for sub, _, halaman in await push_notifications._recipients(session)}
    assert got["o"] == {"pesanan"}
