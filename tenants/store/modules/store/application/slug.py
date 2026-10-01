"""URL slugs for products (SEO-friendly addresses such as /produk/partisi-ruangan-kayu)."""
from __future__ import annotations

import re
import unicodedata

_MAX_LEN = 80


def slugify(text: str, max_len: int = _MAX_LEN) -> str:
    """Lower-case ASCII words joined by single dashes; never empty."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_len].strip("-") or "produk"


def with_suffix(base: str, n: int) -> str:
    """base, base-2, base-3 ... keeping the whole slug within the column length."""
    if n <= 1:
        return base
    suffix = f"-{n}"
    return f"{base[: _MAX_LEN - len(suffix)].rstrip('-')}{suffix}"
