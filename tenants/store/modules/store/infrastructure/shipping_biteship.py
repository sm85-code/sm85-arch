"""Biteship shipping adapter (rates).

Environment:
  BITESHIP_API_KEY        API key from the Biteship dashboard -- a secret: set it only in the environment.
                          A testing key gives test data, a production key gives real rates.
  BITESHIP_ORIGIN_POSTAL  postal code the parcels leave from (default: the shop's, Pangandaran 46396)
  BITESHIP_COURIERS       comma separated Biteship courier codes to quote (default: the common ones)
  BITESHIP_DEFAULT_WEIGHT_GRAM  weight used for a product that has none recorded (default 500)
  BITESHIP_ORIGIN_NAME / _PHONE / _ADDRESS  the pickup contact and address printed on the shipment
  BITESHIP_COD_TYPE       when the courier pays out the COD money: 7_days (cheapest, default), 5_days or 3_days
  BITESHIP_WEBHOOK_KEY / BITESHIP_WEBHOOK_SECRET  header name (default X-Webhook-Secret) and value that Biteship sends
                          with every webhook ("Headers Signature Key / Secret" in the dashboard). When the secret is set,
                          webhooks without it are refused. A webhook is never trusted anyway: it only tells us which
                          shipment to ask Biteship about.

Until BITESHIP_API_KEY is set every entry point answers HTTP 501 (never 503: DigitalOcean App Platform replaces
an application 503 with its own HTML 504 page) instead of inventing a price.

The shipping price is never taken from the browser: the weight and value come from the order lines on the
server, and the price of the chosen courier service is looked up again here when the order's shipping is saved.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import math
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
class PengaturanKirim:
    """Settings chosen in the admin; an empty value means "use the environment / built-in default"."""

    kurir: str = ""  # comma separated Biteship courier codes
    asal_nama: str = ""
    asal_telepon: str = ""
    asal_alamat: str = ""
    asal_kode_pos: str = ""


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
    ongkir: str  # rupiah, whole number (the courier's shipping fee, without any COD fee)
    estimasi: str
    biaya_cod: str = "0"  # rupiah; only set when the rates were asked for a COD amount


@dataclass
class OrderBiteship:
    order_id: str
    tracking_id: str
    waybill_id: str


@dataclass
class RiwayatLacak:
    status: str
    catatan: str
    waktu: str


@dataclass
class HasilLacak:
    status: str  # Biteship's own status, e.g. "picking_up", "delivered"
    waybill_id: str
    riwayat: list[RiwayatLacak]


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


def _cod_type() -> str:
    nilai = os.getenv("BITESHIP_COD_TYPE", "7_days").strip()
    return nilai if nilai in ("3_days", "5_days", "7_days") else "7_days"


def _kurir_aktif(p: PengaturanKirim | None) -> str:
    return (p.kurir if p and p.kurir else "") or os.getenv("BITESHIP_COURIERS", "").strip() or _DEFAULT_COURIERS


def _kode_pos_asal(p: PengaturanKirim | None) -> int:
    nilai = (p.asal_kode_pos if p and p.asal_kode_pos else "") or os.getenv("BITESHIP_ORIGIN_POSTAL", "").strip()
    return int(nilai or _DEFAULT_ORIGIN_POSTAL)


def _rates_sync(
    kode_pos_tujuan: str, items: list[ItemKirim], cod_nilai: int = 0, pengaturan: PengaturanKirim | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "origin_postal_code": _kode_pos_asal(pengaturan),
        "destination_postal_code": int(kode_pos_tujuan),
        "couriers": _kurir_aktif(pengaturan),
        "items": [_item_payload(i) for i in items],
    }
    if cod_nilai > 0:
        body["destination_cash_on_delivery"] = int(cod_nilai)
        body["destination_cash_on_delivery_type"] = _cod_type()
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


def biaya_cod_atas_total(fee_barang: int, nilai_barang: int, ongkir: int) -> int:
    """The courier takes its COD percentage of everything it collects (items + shipping + the fee itself), but the
    rates were asked for the items only. Work out the fee that covers the whole amount, so that the buyer really
    pays it: with rate r = fee / items, collected = (items + shipping) / (1 - r) and fee = collected - items - shipping.
    Rounded up, so the seller never pays the difference."""
    if fee_barang <= 0 or nilai_barang <= 0:
        return max(fee_barang, 0)
    r = fee_barang / nilai_barang
    if r >= 0.5:  # not a plausible percentage (e.g. a flat minimum fee on a tiny order): keep what the courier quoted
        return fee_barang
    dasar = nilai_barang + ongkir
    return max(math.ceil(r * dasar / (1 - r)), fee_barang)


async def cek_ongkir(
    *,
    kode_pos_tujuan: str,
    items: list[ItemKirim],
    cod_nilai: int = 0,
    pengaturan: PengaturanKirim | None = None,
    cod_dari_barang: bool = True,
) -> list[OngkirOption]:
    """Rates for the items. With ``cod_nilai`` only the couriers that can collect that amount on delivery are
    returned, and each option carries the COD fee (``biaya_cod``) apart from the shipping fee (``ongkir``).
    ``cod_dari_barang`` says ``cod_nilai`` is the items' value (the COD fee is then raised to cover the shipping and
    the fee too); pass False when ``cod_nilai`` is already the whole amount to be collected."""
    if not aktif():
        raise BiteshipNotReady()
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tidak ada barang untuk dikirim")
    if not kode_pos_tujuan.isdigit() or len(kode_pos_tujuan) != 5:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kode pos alamat tujuan harus 5 angka")
    data = await asyncio.to_thread(_rates_sync, kode_pos_tujuan, items, cod_nilai, pengaturan)

    options: list[OngkirOption] = []
    for row in data.get("pricing") or []:
        try:
            harga = int(round(float(row["price"])))
            kurir = str(row["courier_code"]).lower()
            layanan = str(row["courier_service_code"])
            fee_cod = int(round(float(row.get("cash_on_delivery_fee") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
        if cod_nilai > 0:
            if not row.get("available_for_cash_on_delivery"):
                continue
            try:
                harga = int(round(float(row["shipping_fee"])))
            except (KeyError, TypeError, ValueError):
                harga = max(harga - fee_cod, 0)
            if cod_dari_barang:
                fee_cod = biaya_cod_atas_total(fee_cod, cod_nilai, harga)
        options.append(
            OngkirOption(
                kurir=kurir,
                kurir_nama=str(row.get("courier_name") or kurir.upper()),
                layanan=layanan,
                layanan_nama=str(row.get("courier_service_name") or layanan),
                ongkir=str(harga),
                estimasi=str(row.get("duration") or row.get("shipment_duration_range") or "").strip(),
                biaya_cod=str(fee_cod if cod_nilai > 0 else 0),
            )
        )
    if not options:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Belum ada kurir yang melayani alamat ini")
    options.sort(key=lambda o: int(o.ongkir))
    return options


def _headers() -> dict[str, str]:
    return {"Authorization": _api_key()}  # Biteship takes the bare key, no "Bearer"


def _request_sync(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        resp = requests.request(method, f"{_BASE_URL}{path}", json=body, headers=_headers(), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Biteship %s %s failed: %s", method, path, exc)
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY, detail="Layanan pengiriman sedang tidak bisa dihubungi. Coba lagi sebentar."
        ) from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code not in (200, 201) or data.get("success") is False:
        logger.warning("Biteship %s %s answered %s: %s", method, path, resp.status_code, str(data)[:300])
        pesan = str(data.get("error") or "").strip()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Biteship menolak permintaan. {pesan}".strip()
        )
    return data


def asal_kirim(p: PengaturanKirim | None = None) -> dict[str, str]:
    """The pickup contact, address and postal code in use (settings, else environment, else built-in)."""
    lokasi = _lokasi_asal(p)
    return {
        "nama": str(lokasi["origin_contact_name"]),
        "telepon": str(lokasi["origin_contact_phone"]),
        "alamat": str(lokasi["origin_address"]),
        "kode_pos": str(lokasi["origin_postal_code"]),
    }


def _lokasi_asal(p: PengaturanKirim | None = None) -> dict[str, Any]:
    def pilih(nilai: str | None, env: str, bawaan: str) -> str:
        return (nilai or "").strip() or os.getenv(env, "").strip() or bawaan

    return {
        "origin_contact_name": pilih(p.asal_nama if p else None, "BITESHIP_ORIGIN_NAME", "AmpelKuning"),
        "origin_contact_phone": pilih(p.asal_telepon if p else None, "BITESHIP_ORIGIN_PHONE", "081313511101"),
        "origin_address": pilih(
            p.asal_alamat if p else None,
            "BITESHIP_ORIGIN_ADDRESS",
            "Jl. Ampelkuning, Dusun Padasuka RT 003 RW 019, Desa Wonoharjo, Kec. Pangandaran, Kab. Pangandaran, Jawa Barat",
        ),
        "origin_postal_code": _kode_pos_asal(p),
    }


async def buat_order(
    *,
    kurir: str,
    layanan: str,
    nama_penerima: str,
    telepon_penerima: str,
    alamat_tujuan: str,
    kode_pos_tujuan: str,
    items: list[ItemKirim],
    catatan: str = "",
    cod_nilai: int = 0,
    pengaturan: PengaturanKirim | None = None,
) -> OrderBiteship:
    """Book the parcel with the courier (a testing key only simulates it: no courier comes)."""
    if not aktif():
        raise BiteshipNotReady()
    if not kode_pos_tujuan.isdigit() or len(kode_pos_tujuan) != 5:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kode pos alamat tujuan harus 5 angka")
    body: dict[str, Any] = {
        **_lokasi_asal(pengaturan),
        "destination_contact_name": nama_penerima,
        "destination_contact_phone": telepon_penerima,
        "destination_address": alamat_tujuan,
        "destination_postal_code": int(kode_pos_tujuan),
        "courier_company": kurir,
        "courier_type": layanan,
        "delivery_type": "now",
        "order_note": catatan[:200],
        "items": [_item_payload(i) for i in items],
    }
    if cod_nilai > 0:
        body["destination_cash_on_delivery"] = int(cod_nilai)
        body["destination_cash_on_delivery_type"] = _cod_type()
    data = await asyncio.to_thread(_request_sync, "POST", "/orders", body)
    kurir_data = data.get("courier") if isinstance(data.get("courier"), dict) else {}
    order_id = str(data.get("id") or "")
    if not order_id:
        raise HTTPException(status_code=status.HTTP_424_FAILED_DEPENDENCY, detail="Biteship tidak mengembalikan nomor pesanan")
    return OrderBiteship(
        order_id=order_id,
        tracking_id=str(kurir_data.get("tracking_id") or ""),
        waybill_id=str(kurir_data.get("waybill_id") or ""),
    )


async def lacak(tracking_id: str) -> HasilLacak:
    if not aktif():
        raise BiteshipNotReady()
    data = await asyncio.to_thread(_request_sync, "GET", f"/trackings/{tracking_id}")
    riwayat = [
        RiwayatLacak(
            status=str(h.get("status") or ""),
            catatan=str(h.get("note") or ""),
            waktu=str(h.get("updated_at") or ""),
        )
        for h in (data.get("history") or [])
        if isinstance(h, dict)
    ]
    riwayat.sort(key=lambda r: r.waktu, reverse=True)
    return HasilLacak(status=str(data.get("status") or ""), waybill_id=str(data.get("waybill_id") or ""), riwayat=riwayat)


def webhook_sah(headers: Any) -> bool:
    """True when no webhook secret is configured, or the request carries the configured header with the right value."""
    rahasia = os.getenv("BITESHIP_WEBHOOK_SECRET", "").strip()
    if not rahasia:
        return True
    nama = os.getenv("BITESHIP_WEBHOOK_KEY", "").strip() or "X-Webhook-Secret"
    diterima = str(headers.get(nama) or "")
    return hmac.compare_digest(diterima.encode(), rahasia.encode())
