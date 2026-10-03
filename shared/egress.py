"""Optional outbound proxy for integrations whose provider whitelists our server IP (iPaymu, Shopee).

DigitalOcean App Platform has no fixed outbound address, so these calls can be sent through a small server
with a fixed IP. Set the integration's own variable (a URL such as ``http://user:password@203.0.113.7:8888``;
it contains a secret, so keep it in the environment only):

  IPAYMU_PROXY_URL   iPaymu calls
  SHOPEE_PROXY_URL   Shopee calls

Unset or empty = no proxy (calls go out directly, as before). Only these integrations use it; the database,
photo storage and everything else stay direct.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse


def proxies_for(env_name: str) -> dict[str, str] | None:
    """The ``proxies=`` argument for ``requests``, or None. An invalid value is ignored rather than breaking payments."""
    url = (os.getenv(env_name) or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return {"http": url, "https": url}
