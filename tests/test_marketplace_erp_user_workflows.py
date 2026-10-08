"""Regressions for user-visible data, scoped read queues and uncertain Chat sends."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import sync_router, chat_router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn, PesananOut
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Pesanan
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee, erp_shopee_chat


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def test_buyer_message_internal_note_and_zero_are_separate():
    row = erp_shopee.normalisasi_pesanan(
        {
            "order_sn": "A",
            "order_status": "PROCESSED",
            "message_to_seller": "Bungkus aman",
            "note": "Internal",
            "actual_shipping_fee": 0,
            "estimated_shipping_fee": 5000,
            "actual_shipping_fee_confirmed": False,
            "item_list": [
                {"item_id": 1, "model_discounted_price": 0, "model_original_price": 5000, "model_quantity_purchased": 1}
            ],
        }
    )
    assert row["items"][0]["harga_satuan"] == 0
    now = datetime.now(timezone.utc)
    orm = SimpleNamespace(
        id="1",
        platform="shopee",
        id_eksternal="A",
        akun_id="a",
        status="to_ship",
        nama_pembeli="",
        total=0,
        tersinkron_marketplace=True,
        catatan_sinkron=None,
        created_at=now,
        updated_at=now,
        detail_json=json.dumps(row["detail"]),
    )
    out = PesananOut.model_validate(orm)
    assert out.message_to_seller == "Bungkus aman" and out.note == "Internal"
    assert out.actual_shipping_fee == 0 and out.actual_shipping_fee_confirmed is False


def test_standardised_variant_keeps_options_and_free_price():
    row = erp_shopee.normalisasi_katalog(
        {"item_id": 1, "has_model": True},
        {
            "standardise_tier_variation": [
                {
                    "variation_name": "Warna",
                    "variation_option_list": [
                        {"variation_option_name": "Merah", "image_url": "https://example.com/red.jpg"}
                    ],
                }
            ],
            "model": [{"model_id": 2, "tier_index": [0], "price_info": [{"current_price": 0}]}],
        },
    )
    variant = row["varian"][0]
    assert variant["opsi"] == [{"tier": "Warna", "opsi": "Merah"}]
    assert variant["harga"] == "0" and row["harga_min"] == 0
    assert row["stok_shopee"] is None


async def setup(session):
    akun = await services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="shopee", nama_toko="A", id_toko_eksternal="123")
    )
    user = SimpleNamespace(id="user", role="admin")
    return akun, user


@pytest.mark.asyncio
async def test_targeted_order_queue_does_not_list_other_orders(session, monkeypatch):
    akun, user = await setup(session)
    order = Pesanan(platform="shopee", id_eksternal="ONLY", akun_id=akun.id, status="unpaid", nama_pembeli="", total=0)
    session.add(order)
    await session.flush()
    calls = []

    async def fresh(*args):
        pass

    async def request(*args, **kwargs):
        calls.append((args[2], kwargs["params"]))
        return {
            "response": {
                "order_list": [{"order_sn": "ONLY", "order_status": "UNPAID", "message_to_seller": "Pesan baru"}]
            }
        }

    monkeypatch.setattr(erp_shopee, "pastikan_token_segar", fresh)
    monkeypatch.setattr(erp_shopee, "signed_shop_request", request)
    job = await sync_router.create(sync_router.SyncIn(jenis="pesanan", ids=[order.id]), session, user)
    same = await sync_router.create(sync_router.SyncIn(jenis="pesanan", ids=[order.id]), session, user)
    assert same["id"] == job["id"]
    done = await sync_router.step(job["id"], session, user)
    assert done["status"] == "selesai" and done["tersisa"] == 0
    assert calls == [
        (
            erp_shopee._PATH_ORDER_DETAIL,
            {"order_sn_list": "ONLY", "response_optional_fields": erp_shopee._ORDER_DETAIL_FIELDS},
        )
    ]
    assert PesananOut.model_validate(await services.get_pesanan(session, order.id)).message_to_seller == "Pesan baru"


@pytest.mark.asyncio
async def test_failed_unit_is_recorded_and_retried_without_successful_units(session, monkeypatch):
    akun, user = await setup(session)

    async def fresh(*args):
        pass

    async def fail(*args, **kwargs):
        raise HTTPException(502, "Provider timeout")

    monkeypatch.setattr(erp_shopee, "pastikan_token_segar", fresh)
    monkeypatch.setattr(erp_shopee, "signed_shop_request", fail)
    job = await sync_router.create(sync_router.SyncIn(jenis="pesanan", akun_id=akun.id), session, user)
    done = await sync_router.step(job["id"], session, user)
    assert done["status"] == "sebagian" and len(done["gagal"]) == 1 and done["tersisa"] == 0
    resumed = await sync_router.retry(job["id"], session, user)
    assert resumed["tersisa"] == 1 and not resumed["gagal"]
    with pytest.raises(HTTPException) as exc:
        await sync_router.get(job["id"], session, SimpleNamespace(id="other"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_chat_uncertain_send_is_never_replayed(session, monkeypatch):
    akun, user = await setup(session)
    calls = []

    async def conversation(*args):
        return {"conversation_id": "c1", "to_id": 77, "shop_id": 123}

    async def send(*args):
        calls.append(args)
        raise HTTPException(502, "Timeout")

    monkeypatch.setattr(erp_shopee_chat, "conversation", conversation)
    monkeypatch.setattr(erp_shopee_chat, "send", send)
    body = chat_router.SendIn(operation_id=uuid4(), text="Halo")
    first = await chat_router.send(akun.id, "c1", body, session, user)
    replay = await chat_router.send(akun.id, "c1", body, session, user)
    assert first["status"] == replay["status"] == "belum_pasti" and len(calls) == 1
    with pytest.raises(HTTPException) as exc:
        await chat_router.send(akun.id, "c2", body, session, user)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_chat_rejects_cross_conversation_history(monkeypatch):
    async def request(*args, **kwargs):
        return {"response": {"messages": [{"message_id": "m", "conversation_id": "other"}]}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", request)
    with pytest.raises(HTTPException) as exc:
        await erp_shopee_chat.messages(None, None, "requested")
    assert exc.value.status_code == 424


@pytest.mark.asyncio
async def test_reordered_items_refresh_metadata_by_identity(session):
    akun, _ = await setup(session)

    def source(items):
        return erp_shopee.normalisasi_pesanan({"order_sn": "SORT", "order_status": "UNPAID", "item_list": items})

    first = [
        {"item_id": 1, "model_id": 2, "item_name": "Produk A", "model_name": "Merah", "model_quantity_purchased": 1},
        {"item_id": 3, "model_id": 4, "item_name": "Produk B", "model_name": "Biru", "model_quantity_purchased": 2},
    ]
    await services.impor_pesanan_marketplace(session, akun, [source(first)])
    reversed_items = [{**first[1], "item_name": "Produk B terbaru"}, {**first[0], "item_name": "Produk A terbaru"}]
    await services.impor_pesanan_marketplace(session, akun, [source(reversed_items)])
    order = (await services.list_pesanan(session, platform="shopee"))[0]
    assert {(i.item_id_eksternal, i.nama_produk, i.qty) for i in order.items} == {
        ("1", "Produk A terbaru", 1),
        ("3", "Produk B terbaru", 2),
    }


@pytest.mark.asyncio
async def test_chat_context_never_links_a_product_from_another_shop(session, monkeypatch):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee

    akun, user = await setup(session)
    other = await services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="shopee", nama_toko="B", id_toko_eksternal="456")
    )
    own = KatalogShopee(akun_id=akun.id, item_id="11", nama="Produk A")
    foreign = KatalogShopee(akun_id=other.id, item_id="22", nama="Produk B")
    session.add_all([own, foreign])
    await session.flush()

    async def conversation(*args):
        return {"conversation_id": "c1", "to_id": 99}

    async def messages(*args):
        return {
            "messages": [
                {"message_id": "m1", "content": {"item_id": 11}},
                {"message_id": "m2", "content": {"item_id": 22}},
            ]
        }

    monkeypatch.setattr(erp_shopee_chat, "conversation", conversation)
    monkeypatch.setattr(erp_shopee_chat, "messages", messages)
    result = await chat_router.messages(akun.id, "c1", None, session, user)
    assert result["messages"][0]["context"]["katalog_id"] == own.id
    assert result["messages"][1]["context"]["katalog_id"] is None


@pytest.mark.asyncio
async def test_chat_cards_are_scoped_to_shop_and_buyer(session):
    from tenants.marketplace_erp.adapters.api.v1 import chat_context
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee, ItemPesanan

    akun, _ = await setup(session)
    other = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="B"))
    order = Pesanan(
        platform="shopee",
        akun_id=akun.id,
        id_eksternal="OWN",
        status="to_ship",
        nama_pembeli="buyer",
        total=100,
        detail_json=json.dumps({"buyer_user_id": 77, "kota": "Kabupaten Bandung"}),
    )
    wrong_buyer = Pesanan(
        platform="shopee",
        akun_id=akun.id,
        id_eksternal="OTHER",
        status="to_ship",
        nama_pembeli="buyer",
        total=100,
        detail_json=json.dumps({"buyer_user_id": 88, "kota": "Jakarta"}),
    )
    own = KatalogShopee(akun_id=akun.id, item_id="11", nama="Produk A", foto_json='["https://example.com/a.jpg"]')
    foreign = KatalogShopee(akun_id=other.id, item_id="22", nama="Produk B")
    session.add_all([order, wrong_buyer, own, foreign])
    await session.flush()
    session.add(
        ItemPesanan(
            pesanan_id=order.id,
            nama_produk="Produk A",
            model_name="Merah",
            qty=1,
            harga_satuan=100,
            subtotal=100,
            foto_url="https://example.com/a.jpg",
        )
    )
    await session.flush()
    target = {"to_id": 77, "to_name": "buyer_renamed"}
    result = await chat_context.context(session, akun, target)
    assert result["kota"] == "Kabupaten Bandung"
    assert [p["id"] for p in result["produk"]] == [own.id]
    assert [p["id"] for p in result["pesanan"]] == [order.id]
    assert result["pesanan"][0]["items"][0]["varian"] == "Merah"
    assert await chat_context.attachment(session, akun, target, "item", own.id) == {"item_id": 11}
    assert await chat_context.attachment(session, akun, target, "order", order.id) == {"order_sn": "OWN"}
    for kind, row_id in [("item", foreign.id), ("order", wrong_buyer.id)]:
        with pytest.raises(HTTPException) as exc:
            await chat_context.attachment(session, akun, target, kind, row_id)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_chat_card_send_is_durable_and_uses_native_payload(session, monkeypatch):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee

    akun, user = await setup(session)
    product = KatalogShopee(akun_id=akun.id, item_id="11", nama="Produk A")
    session.add(product)
    await session.flush()
    calls = []

    async def conversation(*args):
        return {"conversation_id": "c1", "to_id": 77}

    async def request(*args, **kwargs):
        calls.append(kwargs["body"])
        return {"response": {"message_id": "m1", "to_id": 77, "conversation_id": "c1"}}

    monkeypatch.setattr(erp_shopee_chat, "conversation", conversation)
    monkeypatch.setattr(erp_shopee, "signed_shop_request", request)
    body = chat_router.SendIn(operation_id=uuid4(), message_type="item", attachment_id=product.id)
    first = await chat_router.send(akun.id, "c1", body, session, user)
    replay = await chat_router.send(akun.id, "c1", body, session, user)
    assert first["status"] == replay["status"] == "terkirim"
    assert calls == [{"to_id": 77, "message_type": "item", "content": {"item_id": 11}}]


@pytest.mark.asyncio
async def test_contact_order_recipient_comes_from_exact_shopee_order(monkeypatch):
    from tenants.marketplace_erp.adapters.api.v1 import chat_context

    row = SimpleNamespace(id="o1", id_eksternal="OWN", nama_pembeli="buyer", detail_json="{}")

    async def request(*args, **kwargs):
        assert kwargs["params"]["order_sn_list"] == "OWN"
        return {"response": {"order_list": [{"order_sn": "FOREIGN", "buyer_user_id": 99}]}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", request)
    with pytest.raises(HTTPException) as exc:
        await chat_context.order_target(None, None, row)
    assert exc.value.status_code == 424
    row.detail_json = '{"buyer_user_id":77}'
    assert (await chat_context.order_target(None, None, row))["to_id"] == 77


@pytest.mark.asyncio
async def test_contact_and_context_reject_unassigned_staff_before_shopee(session, monkeypatch):
    akun, _ = await setup(session)
    row = Pesanan(
        platform="shopee", akun_id=akun.id, id_eksternal="STAFF", status="to_ship", nama_pembeli="buyer", total=100
    )
    session.add(row)
    await session.flush()
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Provider must not be contacted")

    monkeypatch.setattr(erp_shopee, "signed_shop_request", forbidden)
    user = SimpleNamespace(id="unassigned", role="staff")
    for call in (
        chat_router.order_chat(row.id, "", 0, session, user),
        chat_router.conversation_context(akun.id, "c1", "", 0, session, user),
        chat_router.start_order_chat(row.id, chat_router.SendIn(operation_id=uuid4(), text="Halo"), session, user),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 403
    assert not calls


@pytest.mark.asyncio
async def test_chat_latest_page_anchors_at_now_and_tracks_sender(monkeypatch):
    calls = []
    monkeypatch.setattr(erp_shopee_chat.time, "time_ns", lambda: 1791446400000000000)

    async def request(*args, **kwargs):
        calls.append(kwargs["params"])
        return {
            "response": {
                "conversations": [
                    {"shop_id": 123, "to_id": 77, "latest_message_from_id": 77, "unread_count": 0},
                    {"shop_id": 123, "to_id": 88, "latest_message_from_id": 123, "unread_count": 1},
                    {"shop_id": 123, "to_id": 99},
                    {"shop_id": 456, "to_id": 123, "latest_message_from_id": 456},
                ],
                "page_result": {"more": True},
            }
        }

    monkeypatch.setattr(erp_shopee, "signed_shop_request", request)
    result = await erp_shopee_chat.inbox(None, SimpleNamespace(id_toko_eksternal="123"))
    assert calls[0] == {
        "direction": "older",
        "type": "all",
        "page_size": 20,
        "next_timestamp_nano": "1791446400000000000",
    }
    assert [c["needs_reply"] for c in result["conversations"]] == [True, False, None]
    await erp_shopee_chat.inbox(None, SimpleNamespace(id_toko_eksternal="123"), "1730787900123456789", True)
    assert calls[1]["next_timestamp_nano"] == "1730787900123456789"
    assert calls[1]["direction"] == "older" and calls[1]["type"] == "unread"


@pytest.mark.asyncio
async def test_chat_rejects_buyer_side_and_unknown_shop_conversations(monkeypatch):
    response = {"conversation_id": "c1", "to_id": 77, "shop_id": 456}

    async def request(*args, **kwargs):
        return {"response": response}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", request)
    akun = SimpleNamespace(id_toko_eksternal="123")
    with pytest.raises(HTTPException) as exc:
        await erp_shopee_chat.conversation(None, akun, "c1")
    assert exc.value.status_code == 403
    response.pop("shop_id")
    with pytest.raises(HTTPException) as exc:
        await erp_shopee_chat.conversation(None, akun, "c1")
    assert exc.value.status_code == 424
    response = {"conversations": [{"to_id": 77}]}
    with pytest.raises(HTTPException) as exc:
        await erp_shopee_chat.inbox(None, akun)
    assert exc.value.status_code == 424


@pytest.mark.asyncio
async def test_chat_keeps_history_with_dual_shop_or_missing_role_fields(monkeypatch):
    rows = [
        {
            "message_id": "dual",
            "conversation_id": "c1",
            "from_shop_id": 456,
            "to_shop_id": 123,
            "content": {"text": "Pesan sah"},
        },
        {"message_id": "missing", "conversation_id": "c1", "content": {"text": "Tetap terbaca"}},
    ]

    async def request(*args, **kwargs):
        return {"response": {"messages": rows}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", request)
    result = await erp_shopee_chat.messages(None, SimpleNamespace(id_toko_eksternal="123"), "c1")
    assert result["messages"] == rows
