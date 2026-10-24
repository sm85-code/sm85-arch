import asyncio
from types import SimpleNamespace
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import tools
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_insights as insights


def test_ranking_covers_all_products_sorted(monkeypatch):
    rows = [{"id": f"k{i}", "item_id": str(1000 + i), "nama": f"P{i}", "status": "NORMAL"} for i in range(120)]
    calls = []

    async def katalog(session, **kw):
        start = (kw["halaman"] - 1) * kw["per_halaman"]
        return {"total": len(rows), "items": rows[start:start + kw["per_halaman"]]}

    async def fetch(session, akun, path, field, params=None):
        ids = params["item_id_list"].split(",")
        calls.append(len(ids))
        return {}, {"item_list": [{"item_id": int(i), "views": int(i) % 97, "sale": 1, "likes": 0, "rating_star": 5.0, "comment_count": 0} for i in ids]}

    monkeypatch.setattr(tools.services, "list_katalog_shopee", katalog)
    monkeypatch.setattr(insights, "fetch", fetch)
    out = asyncio.run(tools.product_ranking(None, SimpleNamespace(id="a"), tools.ProductRanking(urut="views", top=5)))
    assert calls == [50, 50, 20]
    assert out["cakupan"]["total_produk"] == 120 and out["cakupan"]["statistik_didapat"] == 120
    views = [i["views"] for i in out["items"]]
    assert views == sorted(views, reverse=True) and views[0] == 96 and len(views) == 5
    low = asyncio.run(tools.product_ranking(None, SimpleNamespace(id="a"), tools.ProductRanking(urut="views", top=3, views_kurang_dari=10)))
    expected = [r for r in rows if (int(r["item_id"]) % 97) < 10]
    assert low["jumlah_cocok"] == len(expected) and len(low["items"]) == 3 and low["ditampilkan"] == 3
    assert low["total_cocok"]["views"] == sum(int(r["item_id"]) % 97 for r in expected)
    assert sorted(low["item_id_cocok"]) == sorted(r["item_id"] for r in expected)


def test_ranking_available_in_tanya_for_all_bidang():
    for agent in ("auto", "operasional", "iklan"):
        names = {d["name"] for d in tools.definitions(False, agent, True)}
        assert "peringkat_produk" in names
