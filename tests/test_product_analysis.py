from decimal import Decimal
import pytest
from tenants.marketplace_erp.modules.marketplace_erp.application.product_analysis import project, catalogue_rows, public_link

def row(**kw):
    r = dict(account="a", shop_name="Toko A", shop_id="1", item="2", model="3",
        product=None, family=None, family_name=None, master_name=None, master_sku="",
        name="Rak", variant="60 cm", item_sku="RAK", model_sku="R60", line="x", units=3, sales=Decimal("300"))
    return dict(r, **kw)

def test_units_rank_before_sales_and_pagination_totals():
    d = project([row(), row(item="4", units=1, sales=Decimal("9999"))], page=2, size=1)
    assert d["rows"][0]["units"] == 1
    assert d["total"] == 2
    assert d["totals"] == dict(units=4, sales=Decimal("10299"))

def test_family_variant_shops_conservation():
    d = project([row(family="f", family_name="Rak master", product="p"),
                 row(account="b", shop_id="9", family="f", family_name="Rak master", product="p", units=4)])
    g = d["rows"][0]
    assert g["units"] == 7 == sum(v["units"] for v in g["variants"])
    assert g["variants"][0]["units"] == sum(s["units"] for s in g["variants"][0]["shops"])
    assert len(g["variants"][0]["shops"]) == 2

def test_no_variant_and_similar_names_not_merged():
    d = project([row(model="0", variant=""), row(account="b", model="0", variant="")])
    assert len(d["rows"]) == 2
    assert d["rows"][0]["variants"] == []
    assert len(d["rows"][0]["shops"]) == 1

def test_multiple_variants_search_and_totals():
    rows = [row(), row(model="4", variant="90 cm", units=10)]
    assert len(project(rows)["rows"][0]["variants"]) == 2
    assert project(rows, search="r60")["totals"]["units"] == 13
    assert project(rows, search="90 cm")["totals"]["units"] == 10
    assert project(rows, search="missing")["totals"]["units"] == 0

def test_missing_identity_never_joins():
    assert project([row(item=None, line="1"), row(item=None, line="2")])["total"] == 2

def test_historical_name_changes_consolidate_listing():
    g = project([row(), row(name="Rak baru", line="y")])["rows"][0]
    assert len(g["variants"][0]["shops"]) == 1
    assert g["variants"][0]["shops"][0]["units"] == 6

@pytest.mark.parametrize("shop,item", [(None,"2"),("1",None),("0","2"),("abc","3"),("1","2?model=3"),("-1","2")])
def test_invalid_link(shop,item):
    assert public_link(shop,item) is None

def entry(**kw):
    return dict(dict(account="a", shop_name="Toko A", shop_id="1", item="2", name="Rak",
        sku="SKU", variants='[{"model_id":"3","nama":"60 cm","sku":"R60","stok":2}]',
        stock=5, status="NORMAL", as_of="2026-11-17"), **kw)

def test_link_message_variant_without_model_deep_link():
    r = catalogue_rows([entry()], "r60")[0]
    assert r["url"] == "https://shopee.co.id/product/1/2"
    assert "pilih varian 60 cm" in r["message"]
    assert r["stock"] == 2

def test_catalogue_no_variant_and_incomplete_link():
    r = catalogue_rows([entry(variants="[]", item=None)])[0]
    assert r["variant"] == ""
    assert r["message"] is None and r["reason"]
