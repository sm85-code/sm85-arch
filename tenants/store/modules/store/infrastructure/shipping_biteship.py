"""Biteship shipping adapter -- PLACEHOLDER (fails closed).

Same situation as payment_ipaymu.py: no verified account/key yet, so every
call raises HTTP 503 instead of fabricating rates or tracking numbers.

TODO with a BITESHIP_API_KEY: implement cek_ongkir() (POST
/v1/rates/couriers) and buat_pengiriman() (POST /v1/orders), plus a
verified webhook mapping order id -> pesanan -- check field names against
the current Biteship docs first.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status


class BiteshipNotReady(HTTPException):
    def __init__(self, detail: str = "Layanan pengiriman (Biteship) belum aktif. Menunggu akun terverifikasi dan integrasi API."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


@dataclass
class OngkirOption:
    kurir: str
    layanan: str
    ongkir: str
    estimasi_hari: str


async def cek_ongkir(
    *,
    kode_pos_asal: str,
    kode_pos_tujuan: str,
    berat_gram: int,
    nilai_barang: str,
) -> list[OngkirOption]:
    raise BiteshipNotReady()
