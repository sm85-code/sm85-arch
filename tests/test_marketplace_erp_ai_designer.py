import base64
import json
from decimal import Decimal
from types import SimpleNamespace
import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AiDrafProduk, AiGambarProduk
from tenants.marketplace_erp.modules.marketplace_erp.application import pengaturan_ui
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import designer, tools, config

JPEG = b"\xff\xd8\xff" + b"0" * 20


@pytest_asyncio.fixture
async def session(monkeypatch):
    monkeypatch.setenv("ERP_AI_ENABLED", "true")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def turn():
    return SimpleNamespace(id="t1", akun_id=None, cost_usd=Decimal(0))


USER = SimpleNamespace(id="admin")


def mock_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(designer.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_designer_tools_only_in_designer_agent():
    names = lambda agent: {d["name"] for d in tools.definitions(True, agent)}
    assert set(designer.TOOLS) <= names("desainer")
    assert "proses_pesanan" not in names("desainer")  # no order/ads writes while designing
    for agent in ("auto", "operasional", "iklan", "coding"):
        assert not set(designer.TOOLS) & names(agent)
    assert not any(tools.TOOLS[n][1] for n in designer.TOOLS)  # never a marketplace write


@pytest.mark.asyncio
async def test_research_anthropic_returns_sources_and_cost(session, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"content": [
            {"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": "https://a.example/1", "title": "Pesaing A"}]},
            {"type": "text", "text": "Harga pesaing Rp45.000", "citations": [{"url": "https://b.example/2", "title": "Pesaing B"}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 500, "server_tool_use": {"web_search_requests": 2}}})
    mock_http(monkeypatch, handler)
    t = turn()
    out = await designer.execute(session, USER, t, "riset_web", designer.Research(kueri="tas rajut wanita"))
    assert out["penyedia"] == "anthropic" and "Rp45.000" in out["ringkasan"]
    assert {s["url"] for s in out["sumber"]} == {"https://a.example/1", "https://b.example/2"}
    body = json.loads(seen[0].content)
    assert body["model"] == config.MODEL and body["tools"][0]["name"] == "web_search"
    assert "test-only" not in json.dumps(out)
    assert t.cost_usd > Decimal("0.02")


@pytest.mark.asyncio
async def test_research_follows_provider_setting_openai(session, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    await pengaturan_ui.simpan(session, designer.ID_PENYEDIA, "openai")

    def handler(request):
        assert json.loads(request.content)["tools"] == [{"type": "web_search"}]
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "Temuan", "annotations": [{"type": "url_citation", "url": "https://c.example", "title": "C"}]}]}], "usage": {"input_tokens": 10, "output_tokens": 10}})
    mock_http(monkeypatch, handler)
    out = await designer.execute(session, USER, turn(), "riset_web", designer.Research(kueri="botol minum"))
    assert out["penyedia"] == "openai" and out["sumber"] == [{"judul": "C", "url": "https://c.example"}]


@pytest.mark.asyncio
async def test_research_without_keys_is_clear(session, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        await designer.execute(session, USER, turn(), "riset_web", designer.Research(kueri="botol minum"))
    assert exc.value.status_code == 503 and "API_KEY" in exc.value.detail


@pytest.mark.asyncio
async def test_images_stored_and_draft_saved_without_publishing(session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")

    def handler(request):
        body = json.loads(request.content)
        assert body["tools"][0]["type"] == "image_generation" and body["model"] == config.MANAGER_MODEL
        return httpx.Response(200, json={"output": [{"type": "image_generation_call", "result": base64.b64encode(JPEG).decode()}]})
    mock_http(monkeypatch, handler)
    t = turn()
    photos = await designer.execute(session, USER, t, "buat_gambar_produk", designer.ImageBrief(brief="Tas rajut krem di meja kayu, cahaya pagi", jumlah=2))
    assert len(photos["gambar_ids"]) == 2 and photos["url"][0].startswith("/asisten/gambar/")
    stored = await session.get(AiGambarProduk, photos["gambar_ids"][0])
    assert stored.data == JPEG and stored.mime == "image/jpeg"
    draft = designer.DraftIn(nama="Tas Rajut Wanita Krem", deskripsi="Tas rajut handmade, muat HP dan dompet.", harga_saran=Decimal("89000"), stok=10, berat_kg=Decimal("0.3"), gambar_ids=photos["gambar_ids"], sumber=["https://a.example/1"])
    out = await designer.execute(session, USER, t, "buat_draf_produk", draft)
    assert out["status"] == "draf" and out["tautan"] == f"/publikasi?draf_ai={out['draf_id']}"
    row = await session.get(AiDrafProduk, out["draf_id"])
    assert json.loads(row.payload_json)["nama"] == "Tas Rajut Wanita Krem"


@pytest.mark.asyncio
async def test_draft_refuses_foreign_or_invented_images(session):
    draft = designer.DraftIn(nama="X", deskripsi="Deskripsi produk yang cukup panjang.", harga_saran=Decimal("1000"), berat_kg=Decimal("0.1"), gambar_ids=["karangan"])
    with pytest.raises(HTTPException) as exc:
        await designer.execute(session, USER, turn(), "buat_draf_produk", draft)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_settings_long_playbook_and_model_allowlist(session):
    long = "Baris satu\nBaris dua " + "x" * 3000
    await pengaturan_ui.simpan(session, designer.ID_PROMPT, long)
    await pengaturan_ui.simpan(session, designer.ID_MODEL_OPENAI, "model-tidak-dikenal")
    s = await designer.settings(session)
    assert s["prompt"] == long and s["model_openai"] == config.MANAGER_MODEL and s["penyedia"] == "otomatis"
    with pytest.raises(HTTPException):
        await pengaturan_ui.simpan(session, "menu:/pesanan", "x" * 300)  # ordinary texts keep the 120 limit
