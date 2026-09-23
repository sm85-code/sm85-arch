from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tenants.toko.modules.erp.application.schemas import PercakapanERPIn, ProdukERPIn, ProdukERPPatch
from tenants.toko.modules.erp.infrastructure.models import (
    PLATFORM_ERP,
    STATUS_PESANAN_ERP,
    PercakapanERP,
    PesanChatERP,
    PesananERP,
    ProdukERP,
)
from tenants.toko.modules.toko.infrastructure.models import ProdukToko

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


# --- Produk ERP -------------------------------------------------------------


def produk_erp_out(produk: ProdukERP) -> dict:
    return {
        "id": produk.id,
        "platform": produk.platform,
        "id_eksternal": produk.id_eksternal,
        "nama": produk.nama,
        "deskripsi": produk.deskripsi,
        "harga": str(produk.harga),
        "stok": produk.stok,
        "foto_url": produk.foto_url,
    }


async def list_produk_erp(session: AsyncSession, *, platform: str | None = None) -> list[ProdukERP]:
    stmt = select(ProdukERP).order_by(ProdukERP.created_at.desc())
    if platform:
        stmt = stmt.where(ProdukERP.platform == _validate_platform(platform))
    return list((await session.execute(stmt)).scalars().all())


async def get_produk_erp(session: AsyncSession, produk_id: str) -> ProdukERP:
    produk = await session.get(ProdukERP, produk_id)
    if not produk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk ERP tidak ditemukan")
    return produk


async def create_produk_erp(session: AsyncSession, payload: ProdukERPIn) -> ProdukERP:
    platform = _validate_platform(payload.platform)
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
    produk_toko = ProdukToko(
        nama=produk_erp.nama,
        deskripsi=produk_erp.deskripsi,
        harga=produk_erp.harga,
        stok=produk_erp.stok,
        foto_url=produk_erp.foto_url,
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
        "id_eksternal": pesanan.id_eksternal,
        "status": pesanan.status,
        "nama_pembeli": pesanan.nama_pembeli,
        "total": str(pesanan.total),
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


async def list_pesanan_erp(session: AsyncSession, *, platform: str | None = None) -> list[PesananERP]:
    stmt = select(PesananERP).options(selectinload(PesananERP.items)).order_by(PesananERP.created_at.desc())
    if platform:
        stmt = stmt.where(PesananERP.platform == _validate_platform(platform))
    return list((await session.execute(stmt)).scalars().all())


async def get_pesanan_erp(session: AsyncSession, pesanan_id: str) -> PesananERP:
    stmt = select(PesananERP).where(PesananERP.id == pesanan_id).options(selectinload(PesananERP.items))
    pesanan = (await session.execute(stmt)).scalar_one_or_none()
    if not pesanan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan ERP tidak ditemukan")
    return pesanan


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
        "id_eksternal_pembeli": percakapan.id_eksternal_pembeli,
        "nama_pembeli": percakapan.nama_pembeli,
        "unread_admin": percakapan.unread_admin,
        "updated_at": percakapan.updated_at.isoformat(),
    }
    if dengan_pesan:
        out["pesan"] = [pesan_erp_out(p) for p in percakapan.pesan]
    return out


async def list_percakapan_erp(session: AsyncSession, *, platform: str | None = None) -> list[PercakapanERP]:
    """Inbox gabungan: satu daftar berisi thread dari ketiga platform
    sekaligus, sort terbaru dulu, opsional difilter ke satu platform saja."""
    stmt = select(PercakapanERP).order_by(PercakapanERP.updated_at.desc())
    if platform:
        stmt = stmt.where(PercakapanERP.platform == _validate_platform(platform))
    return list((await session.execute(stmt)).scalars().all())


async def get_or_create_percakapan_erp(session: AsyncSession, payload: PercakapanERPIn) -> PercakapanERP:
    platform = _validate_platform(payload.platform)
    stmt = select(PercakapanERP).where(
        PercakapanERP.platform == platform, PercakapanERP.id_eksternal_pembeli == payload.id_eksternal_pembeli
    )
    percakapan = (await session.execute(stmt)).scalar_one_or_none()
    if not percakapan:
        percakapan = PercakapanERP(
            platform=platform,
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
