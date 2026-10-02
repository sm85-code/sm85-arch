"""Chat attachments (photo/video), shared product cards and message validation."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.store.adapters.api.v1 import store_buyer_router as buyer_module
from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import PesanChatIn, ProdukIn
from tenants.store.modules.store.infrastructure import media_storage
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import PembeliStore


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _buyer(session):
    u = PembeliStore(nama="P", email="p@test.com", password_hash="x")
    session.add(u)
    await session.flush()
    return u


def test_message_needs_text_or_product():
    with pytest.raises(ValidationError):
        PesanChatIn(isi="   ")
    assert PesanChatIn(isi="halo 😊").isi == "halo 😊"
    assert PesanChatIn(produk_id="x").isi == ""


@pytest.mark.asyncio
async def test_product_card_and_emoji_round_trip(session):
    u = await _buyer(session)
    produk = await services.create_produk(session, ProdukIn(nama="Kaos", harga="100000", stok=1))
    perc = await services.get_or_create_percakapan(session, u.id)
    await services.kirim_pesan(session, perc.id, u.id, "Ada ukuran lain? 🙏", sebagai_admin=False, produk_id=produk.id)
    out = services.percakapan_out(await services.get_percakapan(session, perc.id), dengan_pesan=True)
    pesan = out["pesan"][0]
    assert pesan["isi"] == "Ada ukuran lain? 🙏"
    assert pesan["produk"]["nama"] == "Kaos" and pesan["produk"]["slug"] == produk.slug
    assert pesan["lampiran"] is None


@pytest.mark.asyncio
async def test_unknown_product_is_rejected(session):
    u = await _buyer(session)
    perc = await services.get_or_create_percakapan(session, u.id)
    with pytest.raises(HTTPException) as exc:
        await services.kirim_pesan(session, perc.id, u.id, "x", sebagai_admin=False, produk_id="nope")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_attachment_message_is_stored_with_url(session, monkeypatch):
    monkeypatch.setenv("MEDIA_BASE_URL", "https://img.example.com")

    async def fake_upload(data, content_type):
        return "chat/x.mp4", "video"

    monkeypatch.setattr(buyer_module, "upload_chat_media", fake_upload)
    u = await _buyer(session)

    async def _read():
        return b"x"

    file = SimpleNamespace(read=_read, content_type="video/mp4")
    out = await buyer_module.kirim_lampiran_saya(file=file, isi=" lihat ", session=session, user=u)
    pesan = out["pesan"][0]
    assert pesan["lampiran"] == {"jenis": "video", "url": "https://img.example.com/chat/x.mp4"}
    assert pesan["isi"] == "lihat"


@pytest.mark.asyncio
async def test_chat_media_validation(monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "x")
    stored = {}

    def fake_put(cfg, key, body, ct):
        stored[key] = ct

    monkeypatch.setattr(media_storage, "_put_sync", fake_put)
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 20
    key, jenis = await media_storage.upload_chat_media(mp4, "video/mp4")
    assert key.startswith("chat/") and key.endswith(".mp4") and jenis == "video"
    key, jenis = await media_storage.upload_chat_media(b"\xff\xd8\xff" + b"0" * 9, "image/jpeg")
    assert jenis == "gambar"
    for data, ct in ((b"not a video", "video/mp4"), (b"abc", "application/pdf"), (b"\xff\xd8\xff", "image/png")):
        with pytest.raises(HTTPException) as exc:
            await media_storage.upload_chat_media(data, ct)
        assert exc.value.status_code == 400
    with pytest.raises(HTTPException):  # an image over its own 5 MB limit
        await media_storage.upload_chat_media(b"\xff\xd8\xff" + b"0" * (5 * 1024 * 1024), "image/jpeg")
    assert len(stored) == 2
