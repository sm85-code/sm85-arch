import base64
import json
import pytest
from integrations.easyboss_label_read import read_label, PATH


@pytest.mark.parametrize("platform,state", [("tiktok", "AWAITING_COLLECTION"), ("lazada", "ready_to_ship")])
def test_verified_label_only_reads(platform, state):
    calls = []

    def post(path, data):
        calls.append(path)
        if path == PATH:
            return {
                "errors": [],
                "opPackageIdAndWaybillPdfDataBase64Map": {"p": base64.b64encode(b"%PDF-fixture").decode()},
            }
        return {
            "total": 1,
            "packageList": [
                {
                    "opOrderPackageId": "p",
                    "isSplit": 0,
                    "orderInfo": {
                        "platform": platform,
                        "shopId": "s",
                        "platformOrderSn": "o",
                        "platformOrderStatus": state,
                    },
                }
            ],
        }

    result = json.loads(read_label(post, platform, "s", "o", "p"))
    assert result["package_id"] == "p" and result["order_id"] == "o"
    assert calls == ["/api/order/package/searchOrderPackageList", PATH]


@pytest.mark.parametrize(
    "field,value",
    [
        ("platformOrderStatus", "AWAITING_SHIPMENT"),
        ("platformOrderStatus", "CANCELLED"),
        ("shopId", "wrong"),
        ("platformOrderSn", "wrong"),
    ],
)
def test_unready_or_changed_identity_never_requests_pdf(field, value):
    def post(path, data):
        assert path != PATH
        o = {
            "platform": "tiktok",
            "shopId": "s",
            "platformOrderSn": "o",
            "platformOrderStatus": "AWAITING_COLLECTION",
            field: value,
        }
        return {"total": 1, "packageList": [{"opOrderPackageId": "p", "isSplit": 0, "orderInfo": o}]}

    assert "error" in json.loads(read_label(post, "tiktok", "s", "o", "p"))
