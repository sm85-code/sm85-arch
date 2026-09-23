from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import requests
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tenants.toko.modules.erp.application.schemas import (
    AkunMarketplaceIn,
    AkunMarketplacePatch,
    PercakapanERPIn,
    ProdukERPIn,
    ProdukERPPatch,
)
from tenants.toko.modules.erp.infrastructure.models import (
    PLATFORM_ERP,
    STATUS_PESANAN_ERP,
    AkunMarketplace,
    PercakapanERP,
    PesanChatERP,
    PesananERP,
    ProdukERP,
)
from tenants.toko.modules.toko.infrastructure.image_upload import (
    _ALLOWED_CONTENT_TYPES as _FOTO_ALLOWED_CONTENT_TYPES,
)
from tenants.toko.modules.toko.infrastructure.image_upload import (
    _MAX_BYTES as _FOTO_MAX_BYTES,
)
from tenants.toko.modules.toko.infrastructure.image_upload import upload_produk_photo
from tenants.toko.modules.toko.infrastructure.models import ProdukToko

# Platform -> adapter module dispatch for the one-way local->marketplace
# push on order status change (see ubah_status_pesanan_erp). Imported
# lazily-by-name here (not per-call) so a missing/broken adapter module
# fails at import time, same as any other module-level wiring.
from tenants.toko.modules.erp.infrastructure import erp_blibli, erp_lazada, erp_shopee

_ADAPTER_MODULES = {"shopee": erp_shopee, "lazada": erp_lazada, "blibli": erp_blibli}

# Same timeout convention as adapters/external/gdrive_adapter.py's
# _REQUEST_TIMEOUT_SECONDS -- one-off download of a marketplace photo, not
# high-throughput, so a sync requests.get() wrapped in asyncio.to_thread is
# enough (no new async HTTP dependency needed).
_FOTO_DOWNLOAD_TIMEOUT_SECONDS = 25

_EXT_TO_CONTENT_TYPE = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Transisi status pesanan marketplace yang diizinkan -- linear, tanpa
# jalur retur/refund (di luar scope modul ini, sama seperti
# tenants/toko/modules/toko/application/services.py::_TRANSISI_STATUS).
_TRANSISI_STATUS_ERP = {
    "unpaid": {"to_ship", "cancelled"},
    "to_ship": {"shipped", "cancelled"},
    "shipped": {"completed"},
    "completed": set(),
    "cancelled": set(),
}


def _validate_platform(platform: str) -> str:
    platform = (platform or "").strip().lower()
    if platform not in PLATFORM_ERP:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Platform tidak dikenal")
    return platform


# --- Akun Marketplace --------------------------------------------------------


def akun_out(akun: AkunMarketplace) -> dict:
    """Ringkasan akun untuk list/detail -- SENGAJA TIDAK menyertakan
    access_token/refresh_token (lihat models.py::AkunMarketplace
    docstring). token_kedaluwarsa & status boleh muncul karena itu bukan
    rahasia, cuma metadata."""
    return {
        "id": akun.id,
        "platform": akun.platform,
        "nama_toko": akun.nama_toko,
        "id_toko_eksternal": akun.id_toko_eksternal,
        "status": akun.status,
        "catatan": akun.catatan,
        "token_kedaluwarsa": akun.token_kedaluwarsa.isoformat() if akun.token_kedaluwarsa else None,
        "sudah_terautentikasi": bool(akun.access_token),
        "created_at": akun.created_at.isoformat(),
        "updated_at": akun.updated_at.isoformat(),
    }


async def list_akun_marketplace(session: AsyncSession, *, platform: str | None = None) -> list[AkunMarketplace]:
    stmt = select(AkunMarketplace).order_by(AkunMarketplace.created_at.desc())
    if platform:
        stmt = stmt.where(AkunMarketplace.platform == _validate_platform(platform))
    return list((await session.execute(stmt)).scalars().all())


async def get_akun_marketplace(session: AsyncSession, akun_id: str) -> AkunMarketplace:
    akun = await session.get(AkunMarketplace, akun_id)
    if not akun:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Akun marketplace tidak ditemukan")
    return akun


async def _cek_duplikat_id_toko_eksternal(
    session: AsyncSession, *, platform: str, id_toko_eksternal: str | None, exclude_id: str | None = None
) -> None:
    """Partial-unique-constraint pengganti di service layer: (platform,
    id_toko_eksternal) harus unik HANYA saat id_toko_eksternal terisi.
    Dilakukan di sini (bukan lewat DB constraint) supaya perilakunya sama
    persis di SQLite (dipakai tests) maupun Postgres -- lihat catatan di
    models.py::AkunMarketplace."""
    if not id_toko_eksternal:
        return
    stmt = select(AkunMarketplace).where(
        AkunMarketplace.platform == platform, AkunMarketplace.id_toko_eksternal == id_toko_eksternal
    )
    if exclude_id:
        stmt = stmt.where(AkunMarketplace.id != exclude_id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Toko dengan id_toko_eksternal ini sudah terdaftar untuk platform tsb",
        )


async def create_akun_marketplace(session: AsyncSession, payload: AkunMarketplaceIn) -> AkunMarketplace:
    platform = _validate_platform(payload.platform)
    await _cek_duplikat_id_toko_eksternal(session, platform=platform, id_toko_eksternal=payload.id_toko_eksternal)
    akun = AkunMarketplace(
        platform=platform,
        nama_toko=payload.nama_toko,
        id_toko_eksternal=payload.id_toko_eksternal,
        catatan=payload.catatan,
    )
    session.add(akun)
    await session.flush()
    return akun


async def update_akun_marketplace(session: AsyncSession, akun_id: str, payload: AkunMarketplacePatch) -> AkunMarketplace:
    akun = await get_akun_marketplace(session, akun_id)
    data = payload.model_dump(exclude_unset=True)
    if "id_toko_eksternal" in data:
        await _cek_duplikat_id_toko_eksternal(
            session, platform=akun.platform, id_toko_eksternal=data["id_toko_eksternal"], exclude_id=akun.id
        )
    for field, value in data.items():
        setattr(akun, field, value)
    await session.flush()
    return akun


async def delete_akun_marketplace(session: AsyncSession, akun_id: str) -> None:
    akun = await get_akun_marketplace(session, akun_id)
    await session.delete(akun)
    await session.flush()


async def _validate_akun_untuk_platform(session: AsyncSession, akun_id: str, platform: str) -> AkunMarketplace:
    """Dipakai oleh create produk/pesanan/percakapan ERP: akun_id harus
    menunjuk ke AkunMarketplace yang benar-benar ada DAN platform-nya
    sama dengan platform baris yang mau dibuat (mencegah salah tempel,
    mis. akun Shopee dipakai untuk baris berplatform lazada)."""
    akun = await get_akun_marketplace(session, akun_id)
    if akun.platform != platform:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"akun_id ini adalah akun platform '{akun.platform}', tidak cocok dengan platform '{platform}'",
        )
    return akun


# --- Produk ERP -------------------------------------------------------------


def produk_erp_out(produk: ProdukERP) -> dict:
    return {
        "id": produk.id,
        "platform": produk.platform,
        "akun_id": produk.akun_id,
        "id_eksternal": produk.id_eksternal,
        "nama": produk.nama,
        "deskripsi": produk.deskripsi,
        "harga": str(produk.harga),
        "stok": produk.stok,
        "foto_url": produk.foto_url,
    }


async def list_produk_erp(
    session: AsyncSession, *, platform: str | None = None, akun_id: str | None = None
) -> list[ProdukERP]:
    stmt = select(ProdukERP).order_by(ProdukERP.created_at.desc())
    if platform:
        stmt = stmt.where(ProdukERP.platform == _validate_platform(platform))
    if akun_id:
        stmt = stmt.where(ProdukERP.akun_id == akun_id)
    return list((await session.execute(stmt)).scalars().all())


async def get_produk_erp(session: AsyncSession, produk_id: str) -> ProdukERP:
    produk = await session.get(ProdukERP, produk_id)
    if not produk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk ERP tidak ditemukan")
    return produk


async def create_produk_erp(session: AsyncSession, payload: ProdukERPIn) -> ProdukERP:
    platform = _validate_platform(payload.platform)
    await _validate_akun_untuk_platform(session, payload.akun_id, platform)
    existing = (
        await session.execute(
            select(ProdukERP).where(ProdukERP.platform == platform, ProdukERP.id_eksternal == payload.id_eksternal)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Produk dengan id_eksternal ini sudah ada untuk platform tsb")
    data = payload.model_dump()
    data["platform"] = platform
    produk = ProdukERP(**data)
    session.add(produk)
    await session.flush()
    return produk


async def update_produk_erp(session: AsyncSession, produk_id: str, payload: ProdukERPPatch) -> ProdukERP:
    produk = await get_produk_erp(session, produk_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(produk, field, value)
    await session.flush()
    return produk


async def delete_produk_erp(session: AsyncSession, produk_id: str) -> None:
    produk = await get_produk_erp(session, produk_id)
    await session.delete(produk)


def _guess_content_type(url: str, header_content_type: str | None) -> str | None:
    """Cek Content-Type dari response header dulu, fallback ke ekstensi
    di URL kalau header kosong/tidak dikenali."""
    header_content_type = (header_content_type or "").split(";")[0].strip().lower()
    if header_content_type in _FOTO_ALLOWED_CONTENT_TYPES:
        return header_content_type
    path = urlsplit(url).path
    for ext, content_type in _EXT_TO_CONTENT_TYPE.items():
        if path.lower().endswith(ext):
            return content_type
    return None


def _download_foto_sync(url: str) -> tuple[bytes, str | None]:
    resp = requests.get(url, timeout=_FOTO_DOWNLOAD_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type")


async def _rehost_foto_erp_ke_drive(foto_url: str) -> str:
    """Download foto dari foto_url ERP (link marketplace/manual) dan
    upload ulang ke folder Drive kita sendiri (GDRIVE_FOLDER_ID_TOKO)
    lewat upload_produk_photo yang sudah ada, supaya foto produk web
    benar-benar kita miliki, bukan cuma referensi ke URL luar yang bisa
    berubah/hilang kapan saja. Raise kalau gagal di langkah manapun --
    pemanggil (copy_produk_ke_web) yang soft-fail-kan ini."""
    file_bytes, header_content_type = await asyncio.to_thread(_download_foto_sync, foto_url)

    if len(file_bytes) > _FOTO_MAX_BYTES:
        raise ValueError("Foto dari foto_url ERP melebihi ukuran maksimal")

    content_type = _guess_content_type(foto_url, header_content_type)
    if content_type not in _FOTO_ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Content-Type foto dari foto_url ERP tidak didukung: {header_content_type!r}")

    file_name = urlsplit(foto_url).path.rsplit("/", 1)[-1] or "produk.jpg"
    return await upload_produk_photo(file_bytes, file_name, content_type)


async def copy_produk_ke_web(session: AsyncSession, produk_erp_id: str) -> ProdukToko:
    """Salin satu ProdukERP ke katalog toko web sebagai baris ProdukToko
    BARU dan independen -- snapshot nama/deskripsi/harga/foto/stok saat ini
    juga, bukan referensi live. Perubahan berikutnya di ProdukERP (mis. dari
    sync marketplace) TIDAK akan ikut mengubah baris ProdukToko ini lagi;
    admin yang mau menyamakan perlu copy ulang atau edit manual. Kolom
    provenance (sumber_erp_produk_id, platform_asal) hanya dipakai untuk
    menampilkan "produk ini berasal dari mana", bukan untuk sinkron
    lanjutan -- tidak ada jalur sebaliknya (web -> ERP)."""
    produk_erp = await get_produk_erp(session, produk_erp_id)

    foto_url = produk_erp.foto_url
    if foto_url:
        try:
            foto_url = await _rehost_foto_erp_ke_drive(foto_url)
        except Exception:  # noqa: BLE001 -- deliberate soft-fail, see docstring
            # Rehosting gagal (network error, format tak didukung, ukuran
            # kelebihan, Drive belum dikonfigurasi, dll) -- jangan gagalkan
            # seluruh proses copy karenanya, fallback ke foto_url asli
            # persis seperti perilaku sebelum perbaikan ini.
            foto_url = produk_erp.foto_url

    produk_toko = ProdukToko(
        nama=produk_erp.nama,
        deskripsi=produk_erp.deskripsi,
        harga=produk_erp.harga,
        stok=produk_erp.stok,
        foto_url=foto_url,
        aktif=True,
        sumber_erp_produk_id=produk_erp.id,
        platform_asal=produk_erp.platform,
    )
    session.add(produk_toko)
    await session.flush()
    return produk_toko


# --- Pesanan ERP -------------------------------------------------------------


def pesanan_erp_out(pesanan: PesananERP) -> dict:
    return {
        "id": pesanan.id,
        "platform": pesanan.platform,
        "akun_id": pesanan.akun_id,
        "id_eksternal": pesanan.id_eksternal,
        "status": pesanan.status,
        "nama_pembeli": pesanan.nama_pembeli,
        "total": str(pesanan.total),
        "tersinkron_marketplace": pesanan.tersinkron_marketplace,
        "catatan_sinkron": pesanan.catatan_sinkron,
        "created_at": pesanan.created_at.isoformat(),
        "items": [
            {
                "produk_erp_id": it.produk_erp_id,
                "nama_produk": it.nama_produk,
                "harga_satuan": str(it.harga_satuan),
                "qty": it.qty,
                "subtotal": str(it.subtotal),
            }
            for it in pesanan.items
        ],
    }


async def list_pesanan_erp(
    session: AsyncSession, *, platform: str | None = None, akun_id: str | None = None
) -> list[PesananERP]:
    stmt = select(PesananERP).options(selectinload(PesananERP.items)).order_by(PesananERP.created_at.desc())
    if platform:
        stmt = stmt.where(PesananERP.platform == _validate_platform(platform))
    if akun_id:
        stmt = stmt.where(PesananERP.akun_id == akun_id)
    return list((await session.execute(stmt)).scalars().all())


async def terima_pesanan_erp(
    session: AsyncSession, *, platform: str, akun_id: str, id_eksternal: str, nama_pembeli: str = "", total=None
) -> PesananERP:
    """Terima pesanan baru dari hasil sync marketplace (atau input manual
    admin) -- dipakai adapter masa depan (lihat infrastructure/
    erp_<platform>.py) sebagai satu-satunya jalur pembuatan PesananERP,
    supaya akun_id selalu tervalidasi cocok dengan platform-nya, sama
    seperti create_produk_erp."""
    platform = _validate_platform(platform)
    await _validate_akun_untuk_platform(session, akun_id, platform)
    from decimal import Decimal as _Decimal

    pesanan = PesananERP(
        platform=platform,
        akun_id=akun_id,
        id_eksternal=id_eksternal,
        nama_pembeli=nama_pembeli,
        total=total if total is not None else _Decimal("0"),
    )
    session.add(pesanan)
    await session.flush()
    return pesanan


async def get_pesanan_erp(session: AsyncSession, pesanan_id: str) -> PesananERP:
    stmt = (
        select(PesananERP)
        .where(PesananERP.id == pesanan_id)
        .options(selectinload(PesananERP.items), selectinload(PesananERP.akun))
    )
    pesanan = (await session.execute(stmt)).scalar_one_or_none()
    if not pesanan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan ERP tidak ditemukan")
    return pesanan


async def _dorong_proses_pesanan_ke_marketplace(session: AsyncSession, pesanan: PesananERP) -> None:
    """Attempt the one-way local->marketplace push for the "to_ship"
    ("Proses Pesanan") transition -- soft-fail: whatever the platform
    adapter raises (NotConfigured 503, NotImplementedError, or anything
    else) is caught here and recorded on the row via
    tersinkron_marketplace/catatan_sinkron. The caller's local status
    change is never rolled back because of this, by design (see PR
    description / product decision A)."""
    adapter = _ADAPTER_MODULES.get(pesanan.platform)
    akun = pesanan.akun if pesanan.akun_id else None
    if adapter is None or akun is None:
        pesanan.tersinkron_marketplace = False
        pesanan.catatan_sinkron = "Tidak bisa disinkronkan: akun marketplace untuk pesanan ini tidak ditemukan"
        return
    try:
        await adapter.proses_pesanan(akun, pesanan)
    except HTTPException as exc:
        pesanan.tersinkron_marketplace = False
        pesanan.catatan_sinkron = str(exc.detail)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        pesanan.tersinkron_marketplace = False
        pesanan.catatan_sinkron = str(exc)
    else:
        pesanan.tersinkron_marketplace = True
        pesanan.catatan_sinkron = f"Berhasil disinkronkan ke {pesanan.platform.capitalize()}"


async def ubah_status_pesanan_erp(session: AsyncSession, pesanan_id: str, status_baru: str) -> PesananERP:
    if status_baru not in STATUS_PESANAN_ERP:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status tidak dikenal")
    pesanan = await get_pesanan_erp(session, pesanan_id)
    if status_baru not in _TRANSISI_STATUS_ERP.get(pesanan.status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tidak bisa ubah status dari '{pesanan.status}' ke '{status_baru}'",
        )
    pesanan.status = status_baru
    if status_baru == "to_ship":
        # "Proses Pesanan": attempt to push this to the marketplace --
        # "shipped" ("Kirim Pesanan") deliberately does NOT call any
        # adapter, the platform's own logistics system handles
        # pickup/drop-off automatically once "to_ship" was acknowledged.
        await _dorong_proses_pesanan_ke_marketplace(session, pesanan)
    await session.flush()
    return pesanan


# --- Chat ERP (inbox gabungan lintas platform) ------------------------------


def pesan_erp_out(pesan: PesanChatERP) -> dict:
    return {
        "id": pesan.id,
        "pengirim_admin": pesan.pengirim_admin,
        "isi": pesan.isi,
        "created_at": pesan.created_at.isoformat(),
    }


def percakapan_erp_out(percakapan: PercakapanERP, *, dengan_pesan: bool = False) -> dict:
    out = {
        "id": percakapan.id,
        "platform": percakapan.platform,
        "akun_id": percakapan.akun_id,
        "id_eksternal_pembeli": percakapan.id_eksternal_pembeli,
        "nama_pembeli": percakapan.nama_pembeli,
        "unread_admin": percakapan.unread_admin,
        "updated_at": percakapan.updated_at.isoformat(),
    }
    if dengan_pesan:
        out["pesan"] = [pesan_erp_out(p) for p in percakapan.pesan]
    return out


async def list_percakapan_erp(
    session: AsyncSession, *, platform: str | None = None, akun_id: str | None = None
) -> list[PercakapanERP]:
    """Inbox gabungan: satu daftar berisi thread dari ketiga platform
    sekaligus, sort terbaru dulu, opsional difilter ke satu platform dan/
    atau satu akun saja."""
    stmt = select(PercakapanERP).order_by(PercakapanERP.updated_at.desc())
    if platform:
        stmt = stmt.where(PercakapanERP.platform == _validate_platform(platform))
    if akun_id:
        stmt = stmt.where(PercakapanERP.akun_id == akun_id)
    return list((await session.execute(stmt)).scalars().all())


async def get_or_create_percakapan_erp(session: AsyncSession, payload: PercakapanERPIn) -> PercakapanERP:
    platform = _validate_platform(payload.platform)
    await _validate_akun_untuk_platform(session, payload.akun_id, platform)
    stmt = select(PercakapanERP).where(
        PercakapanERP.platform == platform, PercakapanERP.id_eksternal_pembeli == payload.id_eksternal_pembeli
    )
    percakapan = (await session.execute(stmt)).scalar_one_or_none()
    if not percakapan:
        percakapan = PercakapanERP(
            platform=platform,
            akun_id=payload.akun_id,
            id_eksternal_pembeli=payload.id_eksternal_pembeli,
            nama_pembeli=payload.nama_pembeli,
        )
        session.add(percakapan)
        await session.flush()
    return percakapan


async def get_percakapan_erp(session: AsyncSession, percakapan_id: str) -> PercakapanERP:
    stmt = select(PercakapanERP).where(PercakapanERP.id == percakapan_id).options(selectinload(PercakapanERP.pesan))
    percakapan = (await session.execute(stmt)).scalar_one_or_none()
    if not percakapan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Percakapan tidak ditemukan")
    return percakapan


async def kirim_pesan_erp(session: AsyncSession, percakapan_id: str, *, isi: str, pengirim_admin: bool) -> PesanChatERP:
    percakapan = await session.get(PercakapanERP, percakapan_id)
    if not percakapan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Percakapan tidak ditemukan")
    pesan = PesanChatERP(percakapan_id=percakapan_id, pengirim_admin=pengirim_admin, isi=isi)
    session.add(pesan)
    percakapan.unread_admin = not pengirim_admin
    await session.flush()
    session.expire(percakapan, ["pesan"])
    return pesan
