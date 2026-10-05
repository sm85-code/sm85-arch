"""Break-even ROAS of an ad from the seller's own cost price (modal) of the advertised products.

margin = 1 - modal share of the price - Shopee fees share; an ad pays for itself when GMV * margin >= ad cost, i.e. when
ROAS >= 1 / margin. Only the fees Shopee takes (commission, service, transaction) are counted; shipping, vouchers and
packing are not, so the break-even is a floor, not a promise of profit.
"""
from __future__ import annotations

from typing import Any


def _angka(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def harga_acuan(harga_min: Any, harga_max: Any) -> float | None:
    """Price used to turn a Rp cost into a share: the middle of the variants' range (one number for a whole item)."""
    a, b = _angka(harga_min), _angka(harga_max)
    if a and b:
        return (a + b) / 2
    return a or b or None


def porsi_modal(modal_rp: Any, modal_persen: Any, harga: float | None) -> float | None:
    """Share of the selling price that is cost (0..1), from a percentage or from Rp over the price; None if unknown."""
    persen = _angka(modal_persen)
    if persen is not None:
        return persen / 100
    rp = _angka(modal_rp)
    if rp is not None and harga:
        return rp / harga
    return None


def hitung(produk: list[dict], biaya_shopee_persen: float | None) -> dict:
    """``produk``: dicts with harga (price), modal_rp, modal_persen. Averages the modal share over the products that have
    one; ``terisi`` / ``total`` tell how complete that is. margin_persen / roas_impas are None when nothing is filled or
    the margin is not positive (the ad cannot pay for itself at any ROAS)."""
    porsi = [p for p in (porsi_modal(x.get("modal_rp"), x.get("modal_persen"), x.get("harga")) for x in produk) if p is not None]
    hasil: dict = {"terisi": len(porsi), "total": len(produk), "margin_persen": None, "roas_impas": None, "biaya_shopee_diketahui": biaya_shopee_persen is not None}
    if not porsi:
        return hasil
    margin = 1 - sum(porsi) / len(porsi) - (biaya_shopee_persen or 0)
    hasil["margin_persen"] = round(margin * 100, 1)
    hasil["roas_impas"] = round(1 / margin, 2) if margin > 0 else None
    return hasil
