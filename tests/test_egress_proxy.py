"""Optional fixed-IP proxy for iPaymu / Shopee calls."""

from shared.egress import proxies_for
from tenants.store.modules.store.infrastructure import payment_ipaymu as ip


def test_unset_empty_or_invalid_means_no_proxy(monkeypatch):
    monkeypatch.delenv("X_PROXY", raising=False)
    assert proxies_for("X_PROXY") is None
    for bad in ("", "   ", "localhost", "ftp://u:p@1.2.3.4:8888", "http://"):
        monkeypatch.setenv("X_PROXY", bad)
        assert proxies_for("X_PROXY") is None


def test_valid_url_applies_to_http_and_https(monkeypatch):
    monkeypatch.setenv("X_PROXY", " http://user:pw@203.0.113.7:8888 ")
    assert proxies_for("X_PROXY") == {"http": "http://user:pw@203.0.113.7:8888", "https": "http://user:pw@203.0.113.7:8888"}


class _Resp:
    status_code = 200

    def json(self):
        return {"Status": 200, "Data": {"SessionID": "S", "Url": "https://x"}}


def test_ipaymu_call_goes_through_the_proxy_only_when_configured(monkeypatch):
    monkeypatch.setenv("IPAYMU_VA", "1")
    monkeypatch.setenv("IPAYMU_API_KEY", "k")
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw)
        return _Resp()

    monkeypatch.setattr(ip.requests, "post", fake_post)
    monkeypatch.delenv("IPAYMU_PROXY_URL", raising=False)
    ip._post_sync("/payment", {"a": 1})
    assert seen["proxies"] is None
    monkeypatch.setenv("IPAYMU_PROXY_URL", "http://u:p@203.0.113.7:8888")
    ip._post_sync("/payment", {"a": 1})
    assert seen["proxies"]["https"] == "http://u:p@203.0.113.7:8888"
