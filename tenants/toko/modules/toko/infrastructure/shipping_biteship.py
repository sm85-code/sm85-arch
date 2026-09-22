"""Biteship shipping adapter -- PLACEHOLDER.

STATUS: not wired to the real Biteship API yet, same reasoning as
payment_ipaymu.py -- the account was just registered and business
verification is not done, so there is no key to build/test against.
Every call fails loudly (HTTP 503) rather than fabricating rates or
tracking numbers.

TODO once you have a Biteship API key:
  1. Fill BITESHIP_API_KEY in your .env (see .env.example).
  2. Confirm the exact request/response shape against the current
     Biteship API docs (https://biteship.com/id/docs -- unreachable from
     this session's network). This module assumes, UNVERIFIED:
       - auth header: Authorization: <api_key> (no "Bearer " prefix, per
         Biteship's documented convention as of this module's authoring)
       - POST {base_url}/v1/rates/couriers for a rate check, with
         origin_postal_code / destination_postal_code / couriers /
         items (weight, value) in the body
       - POST {base_url}/v1/orders to create a shipment, returning an
         order id + courier waybill/tracking number once assigned
     Verify every field name against the real docs/Postman collection.
  3. Confirm the webhook payload Biteship POSTs on status change (order
     id, status, waybill_id) and adjust parse_webhook() accordingly.
  4. Remove the NotConfigured guards once verified end-to-end.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import HTTPException, status

BITESHIP_API_KEY = os.getenv("BITESHIP_API_KEY")
BITESHIP_BASE_URL = "https://api.biteship.com"


class BiteshipNotConfigured(HTTPException):
    def __init__(self, detail: str = "Layanan pengiriman (Biteship) belum dikonfigurasi. Isi BITESHIP_API_KEY setelah akun terverifikasi."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


@dataclass
class OngkirOption:
    kurir: str
    layanan: str
    ongkir: str
    estimasi_hari: str


def _is_configured() -> bool:
    return bool(BITESHIP_API_KEY)


async def cek_ongkir(
    *,
    kode_pos_asal: str,
    kode_pos_tujuan: str,
    berat_gram: int,
    nilai_barang: str,
) -> list[OngkirOption]:
    """Rate check across couriers. NOT IMPLEMENTED against the real API
    yet -- raises BiteshipNotConfigured until BITESHIP_API_KEY is set."""
    if not _is_configured():
        raise BiteshipNotConfigured()

    raise NotImplementedError(
        "Biteship cek_ongkir() is not wired up yet -- verify the request shape "
        "(POST /v1/rates/couriers) against current docs first."
    )


async def buat_pengiriman(
    *,
    pesanan_id: str,
    kurir: str,
    layanan: str,
    penerima: dict,
    items: list[dict],
) -> dict:
    """Create a shipment order. NOT IMPLEMENTED against the real API yet."""
    if not _is_configured():
        raise BiteshipNotConfigured()

    raise NotImplementedError(
        "Biteship buat_pengiriman() is not wired up yet -- verify the request shape "
        "(POST /v1/orders) against current docs first."
    )


def parse_webhook(payload: dict) -> dict:
    """Best-effort mapping of commonly documented Biteship webhook fields.
    UNVERIFIED -- confirm against a real test shipment before relying on
    this."""
    return {
        "order_id": payload.get("order_id") or payload.get("id"),
        "status_mentah": payload.get("status"),
        "tracking_id": payload.get("waybill_id") or payload.get("courier_waybill_id"),
    }
