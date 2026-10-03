"""Biteship shipping adapter (rates).

Environment:
  BITESHIP_API_KEY        API key from the Biteship dashboard -- a secret: set it only in the environment.
                          A testing key gives test data, a production key gives real rates.
  BITESHIP_ORIGIN_POSTAL  postal code the parcels leave from (default: the shop's, Pangandaran 46396)
  BITESHIP_COURIERS       comma separated Biteship courier codes to quote (default: the common ones)
  BITESHIP_DEFAULT_WEIGHT_GRAM  weight used for a product that has none recorded (default 500)

Until BITESHIP_API_KEY is set every entry point answers HTTP 501 (never 503: DigitalOcean App Platform replaces
an application 503 with its own HTML 504 page) instead of inventing a price.

The shipping price is never taken from the browser: the weight and value come from the order lines on the
server, and the price of the chosen courier service is looked up again here when the order's shipping is saved.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import requests
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.biteship.com/v1"
_TIMEOUT = 20
_DEFAULT_ORIGIN_POSTAL = "46396"
_DEFAULT_COURIERS = "jne,jnt,sicepat,anteraja,idexpress,ninja,lion,tiki,pos,wahana"


class BiteshipNotReady(HTTPException):
    def __init__(self, detail: str = "Layanan pengiriman (Biteship) belum aktif. Menunggu akun terverifikasi dan integrasi API."):
        super().__init__(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=detail)


@dataclass
class ItemKirim:
    nama: str
    nilai: int  # unit price in rupiah
    qty: int
    berat_gram: int
    panjang_cm: int = 0
    lebar_cm: int = 0
    tinggi_cm: int = 0


@dataclass
class OngkirOption:
    kurir: str  # courier code, e.g. "jne"
    kurir_nama: str
    layanan: str  # service code, e.g. "reg"
    layanan_nama: str
    ongkir: str  # rupiah, whole number
    estimasi: str


def _api_key() -> str:
    return os.getenv("BITESHIP_API_KEY", "").strip()


def aktif() -> bool:
    return bool(_api_key())


def berat_default() -> int:
    try:
        return max(1, int(os.getenv("BITESHIP_DEFAULT_WEIGHT_GRAM", "500")))
    except ValueError:
        return 500


def _item_payload(item: ItemKirim) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": item.nama[:100] or "Produk",
        "value": max(int(item.nilai), 0),
        "quantity": max(int(item.qty), 1),
        "weight": max(int(item.berat_gram), 1),
    }
    for key, value in (("length", item.panjang_cm), ("width", item.lebar_cm), ("height", item.tinggi_cm)):
        if value > 0:
            data[key] = int(value)
    return data


def _rates_sync(kode_pos_tujuan: str, items: list[ItemKirim]) -> dict[str, Any]:
    body = {
        "origin_postal_code": int(os.getenv("BITESHIP_ORIGIN_POSTAL", _DEFAULT_ORIGIN_POSTAL).strip() or _DEFAULT_ORIGIN_POSTAL),
        "destination_postal_code": int(kode_pos_tujuan),
        "couriers": os.getenv("BITESHIP_COURIERS", _DEFAULT_COURIERS).strip() or _DEFAULT_COURIERS,
        "items": [_item_payload(i) for i in items],
    }
    try:
        resp = requests.post(
            f"{_BASE_URL}/rates/couriers",
            json=body,
            headers={"Authorization": _api_key()},  # Biteship takes the bare key, no "Bearer"
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Biteship rates request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY, detail="Layanan ongkir sedang tidak bisa dihubungi. Coba lagi sebentar."
        ) from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or not data.get("success", True):
        logger.warning("Biteship rates answered %s: %s", resp.status_code, str(data)[:300])
        pesan = str(data.get("error") or "").strip() if isinstance(data, dict) else ""
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ongkir tidak bisa dihitung untuk alamat ini. {pesan}".strip(),
        )
    return data


async def cek_ongkir(*, kode_pos_tujuan: str, items: list[ItemKirim]) -> list[OngkirOption]:
    if not aktif():
        raise BiteshipNotReady()
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tidak ada barang untuk dikirim")
    if not kode_pos_tujuan.isdigit() or len(kode_pos_tujuan) != 5:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kode pos alamat tujuan harus 5 angka")
    data = await asyncio.to_thread(_rates_sync, kode_pos_tujuan, items)

    options: list[OngkirOption] = []
    for row in data.get("pricing") or []:
        try:
            harga = int(round(float(row["price"])))
            kurir = str(row["courier_code"]).lower()
            layanan = str(row["courier_service_code"])
        except (KeyError, TypeError, ValueError):
            continue
        options.append(
            OngkirOption(
                kurir=kurir,
                kurir_nama=str(row.get("courier_name") or kurir.upper()),
                layanan=layanan,
                layanan_nama=str(row.get("courier_service_name") or layanan),
                ongkir=str(harga),
                estimasi=str(row.get("duration") or row.get("shipment_duration_range") or "").strip(),
            )
        )
    if not options:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Belum ada kurir yang melayani alamat ini")
    options.sort(key=lambda o: int(o.ongkir))
    return options
