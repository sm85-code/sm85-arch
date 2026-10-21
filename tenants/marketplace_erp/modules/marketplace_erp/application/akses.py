"""Roles defined by access: which shops, which pages, and read or write per page.

A role (mpe_role) has `akses_toko` ("semua" or "ditugaskan") and a level ("baca" or "tulis") for each page it may open
(mpe_role_halaman). `admin` is built in and always has everything; `owner` and `staff` are built in too but an admin
can edit what they may do. Every page follows these settings (see peta_akses.py for which routes belong to which page).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable

from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    Role,
    RoleHalaman,
    UserMarketplaceErp,
)

TINGKAT = ("baca", "tulis")
AKSES_TOKO = ("semua", "ditugaskan")
ADMIN = "admin"

# (key, label, group, levels). levels: which of baca/tulis exist for the page (a page that is only read, or only an
# action, has a single level).
HALAMAN: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("dashboard", "Dashboard", "Harian", ("baca",)),
    ("pesanan", "Pesanan", "Harian", ("baca", "tulis")),
    ("pesanan_manual", "Buat dan hapus pesanan manual", "Harian", ("tulis",)),
    ("chat", "Chat", "Harian", ("baca", "tulis")),
    ("retur", "Retur & Refund", "Harian", ("baca", "tulis")),
    ("katalog", "Katalog Shopee", "Produk", ("baca", "tulis")),
    ("katalog_lanjutan", "Katalog: pengaturan lanjutan Shopee", "Produk", ("baca", "tulis")),
    ("produk", "Produk (SKU)", "Produk", ("baca", "tulis")),
    ("listing", "Pemetaan Produk Toko", "Produk", ("baca", "tulis")),
    ("gudang", "Gudang & Stok", "Produk", ("baca", "tulis")),
    ("promosi", "Promosi Diskon", "Produk", ("baca", "tulis")),
    ("publikasi", "Buat / Salin Produk", "Produk", ("baca", "tulis")),
    ("sinkron_shopee", "Sinkronisasi dan kirim stok/harga ke Shopee", "Produk", ("tulis",)),
    ("laporan_ringkas", "Ringkasan laporan (stok kritis)", "Produk", ("baca",)),
    ("processing", "Pemetaan Order dan Data Produksi", "Processing", ("baca", "tulis")),
    ("operasional_umum", "Operasional Umum", "Keuangan", ("baca", "tulis")),
    ("operasional_packing", "Operasional Packing", "Keuangan", ("baca", "tulis")),
    ("arus_kas", "Arus Kas (tulis = centang)", "Keuangan", ("baca", "tulis")),
    ("laporan_keuangan", "Laporan Keuangan", "Keuangan", ("baca", "tulis")),
    ("settlement", "Dana & Saldo Shopee", "Keuangan", ("baca", "tulis")),
    ("info_toko", "Performa dan Pengaturan Toko", "Info Toko", ("baca", "tulis")),
    ("otomasi", "Otomasi produk dan pesanan", "Info Toko", ("baca", "tulis")),
    ("iklan", "Iklan", "Iklan", ("baca", "tulis")),
    ("kelola_toko", "Kelola Toko", "Pengaturan", ("baca", "tulis")),
    ("push_shopee", "Pengaturan push Shopee", "Pengaturan", ("baca", "tulis")),
    ("asisten", "Asisten AI", "Pengaturan", ("baca", "tulis")),
    ("kelola_pengguna", "Kelola Pengguna dan Role", "Pengaturan", ("baca", "tulis")),
]
TINGKAT_HALAMAN = {k: lv for k, _, _, lv in HALAMAN}

# What the built-in roles could reach before roles were editable; seeded as the starting checkmarks. The test
# tests/test_marketplace_erp_peta_akses.py checks these against a snapshot of the old fixed rules.
_OWNER = {
    "dashboard": "baca", "pesanan": "tulis", "pesanan_manual": "tulis", "chat": "tulis", "retur": "tulis", "katalog": "tulis",
    "produk": "tulis", "listing": "tulis", "gudang": "tulis", "promosi": "tulis", "publikasi": "tulis",
    "sinkron_shopee": "tulis", "laporan_ringkas": "baca", "push_shopee": "tulis",
    "operasional_umum": "baca", "operasional_packing": "baca",
}
_STAFF = {"dashboard": "baca", "pesanan": "tulis", "chat": "tulis", "retur": "tulis", "katalog": "baca", "arus_kas": "tulis"}

# Built-in roles and the ones every installation starts with. Seeded only when missing, never overwritten.
BAWAAN: list[dict] = [
    {"id": "admin", "nama": "Admin", "keterangan": "Semua akses; tidak bisa diubah", "akses_toko": "semua", "sistem": True, "halaman": {}},
    {"id": "owner", "nama": "Owner", "keterangan": "Pemilik usaha", "akses_toko": "semua", "sistem": True, "halaman": _OWNER},
    {"id": "staff", "nama": "Staff", "keterangan": "Staf toko", "akses_toko": "ditugaskan", "sistem": True, "halaman": _STAFF},
    {"id": "staf_keuangan", "nama": "Staf Keuangan", "keterangan": "Mencatat dan memeriksa transaksi keuangan", "akses_toko": "semua", "sistem": False,
     "halaman": {"operasional_umum": "tulis", "operasional_packing": "tulis", "arus_kas": "tulis"}},
    {"id": "staf_operasional", "nama": "Staf Operasional", "keterangan": "Mencatat operasional umum", "akses_toko": "semua", "sistem": False,
     "halaman": {"operasional_umum": "tulis"}},
    {"id": "staf_produksi", "nama": "Staf Produksi", "keterangan": "Mencatat operasional packing", "akses_toko": "semua", "sistem": False,
     "halaman": {"operasional_packing": "tulis"}},
]


def _norm(role: str | None) -> str:
    return (role or "").strip().lower()


async def pastikan_bawaan(session: AsyncSession) -> None:
    """Create what is missing: the three built-in roles always, the starter roles (Staf Keuangan, ...) only on a fresh
    installation, so a starter role an admin deleted does not come back. Existing roles and their edits stay as they are."""
    ada = set((await session.scalars(select(Role.id))).all())
    for r in BAWAAN:
        if r["id"] in ada or (not r["sistem"] and ada):
            continue
        session.add(Role(id=r["id"], nama=r["nama"], keterangan=r["keterangan"], akses_toko=r["akses_toko"], sistem=r["sistem"]))
        for halaman, tingkat in r["halaman"].items():
            session.add(RoleHalaman(role_id=r["id"], halaman=halaman, tingkat=tingkat))
    await session.flush()


async def _role(session: AsyncSession, role_id: str) -> Role | None:
    role = await session.get(Role, role_id)
    if role is None and role_id in ("admin", "owner", "staff"):
        await pastikan_bawaan(session)  # a database from before roles existed
        role = await session.get(Role, role_id)
    return role


async def tingkat_halaman(session: AsyncSession, role_id: str | None, halaman: str) -> str | None:
    """"tulis", "baca" or None for this role on this page. Admin always has "tulis"."""
    role_id = _norm(role_id)
    if role_id == ADMIN:
        return "tulis"
    if await _role(session, role_id) is None:
        return None
    row = await session.get(RoleHalaman, (role_id, halaman))
    return row.tingkat if row else None


async def akses_toko(session: AsyncSession, role_id: str | None) -> str | None:
    """"semua", "ditugaskan", or None for a role that does not exist."""
    role_id = _norm(role_id)
    if role_id == ADMIN:
        return "semua"
    role = await _role(session, role_id)
    return role.akses_toko if role else None


def butuh_halaman(halaman: str, perlu: str) -> Callable:
    """FastAPI dependency: the signed-in user's role must reach `halaman` at level `perlu` ("baca" or "tulis")."""
    assert halaman in TINGKAT_HALAMAN and perlu in TINGKAT

    async def _inner(
        user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
        session: AsyncSession = Depends(get_db_marketplace_erp),
    ) -> UserMarketplaceErp:
        punya = await tingkat_halaman(session, user.role, halaman)
        if punya is None or (perlu == "tulis" and punya != "tulis"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Akses ditolak")
        return user

    return _inner


# --- role management (admin only; see roles_router) --------------------------------------------------------------


def _slug(nama: str) -> str:
    teks = unicodedata.normalize("NFKD", nama).encode("ascii", "ignore").decode().lower()
    teks = re.sub(r"[^a-z0-9]+", "_", teks).strip("_")
    return (teks if teks[:1].isalpha() else f"role_{teks}")[:48].rstrip("_")


def _bersih_halaman(halaman: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, tingkat in (halaman or {}).items():
        if key not in TINGKAT_HALAMAN:
            raise HTTPException(422, f"Halaman '{key}' tidak dikenal")
        if tingkat == "tidak":
            continue
        if tingkat not in TINGKAT_HALAMAN[key]:
            raise HTTPException(422, f"Halaman '{key}' hanya punya tingkat: {', '.join(TINGKAT_HALAMAN[key])}")
        out[key] = tingkat
    return out


async def _jumlah_pengguna(session: AsyncSession, role_id: str) -> int:
    return (await session.execute(select(func.count()).select_from(UserMarketplaceErp).where(UserMarketplaceErp.role == role_id))).scalar_one()


async def daftar(session: AsyncSession) -> dict:
    await pastikan_bawaan(session)
    roles = (await session.scalars(select(Role).order_by(Role.sistem.desc(), Role.nama))).all()
    izin: dict[str, dict[str, str]] = {r.id: {} for r in roles}
    for h in (await session.scalars(select(RoleHalaman))).all():
        izin.setdefault(h.role_id, {})[h.halaman] = h.tingkat
    jumlah = dict(
        (await session.execute(select(UserMarketplaceErp.role, func.count()).group_by(UserMarketplaceErp.role))).all()
    )
    return {
        "roles": [
            {
                "id": r.id, "nama": r.nama, "keterangan": r.keterangan, "akses_toko": r.akses_toko, "sistem": r.sistem,
                "terkunci": r.id == ADMIN, "jumlah_pengguna": jumlah.get(r.id, 0),
                "halaman": {k: lv[-1] for k, lv in TINGKAT_HALAMAN.items()} if r.id == ADMIN else izin.get(r.id, {}),
            }
            for r in roles
        ],
        "halaman": [{"id": k, "label": lbl, "grup": g, "tingkat": list(lv)} for k, lbl, g, lv in HALAMAN],
    }


async def akses_pengguna(session: AsyncSession, role_id: str | None) -> dict:
    """What the signed-in user's role may do, for the app to show or hide menus and buttons."""
    role_id = _norm(role_id)
    role = await _role(session, role_id) if role_id != ADMIN else None
    if role_id == ADMIN:
        return {"role": ADMIN, "nama": "Admin", "akses_toko": "semua", "halaman": {k: lv[-1] for k, lv in TINGKAT_HALAMAN.items()}}
    if role is None:
        return {"role": role_id, "nama": role_id, "akses_toko": "ditugaskan", "halaman": {}}
    izin = {h.halaman: h.tingkat for h in (await session.scalars(select(RoleHalaman).where(RoleHalaman.role_id == role.id))).all()}
    return {"role": role.id, "nama": role.nama, "akses_toko": role.akses_toko, "halaman": izin}


async def buat(session: AsyncSession, nama: str, keterangan: str, akses: str, halaman: dict[str, str]) -> str:
    nama = (nama or "").strip()
    if not 2 <= len(nama) <= 80:
        raise HTTPException(422, "Nama role 2-80 karakter")
    if akses not in AKSES_TOKO:
        raise HTTPException(422, "Akses toko harus semua atau ditugaskan")
    role_id = _slug(nama)
    if len(role_id) < 2:
        raise HTTPException(422, "Nama role tidak valid")
    if await session.get(Role, role_id) or (await session.scalar(select(Role.id).where(func.lower(Role.nama) == nama.lower()))):
        raise HTTPException(409, "Nama role sudah dipakai")
    bersih = _bersih_halaman(halaman)
    session.add(Role(id=role_id, nama=nama, keterangan=(keterangan or "").strip()[:255], akses_toko=akses, sistem=False))
    for k, t in bersih.items():
        session.add(RoleHalaman(role_id=role_id, halaman=k, tingkat=t))
    await session.commit()
    return role_id


async def ubah(session: AsyncSession, role_id: str, nama: str, keterangan: str, akses: str, halaman: dict[str, str]) -> None:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(404, "Role tidak ditemukan")
    if role.id == ADMIN:
        raise HTTPException(409, "Role Admin tidak bisa diubah")
    if akses not in AKSES_TOKO:
        raise HTTPException(422, "Akses toko harus semua atau ditugaskan")
    bersih = _bersih_halaman(halaman)
    if not role.sistem:
        nama = (nama or "").strip()
        if not 2 <= len(nama) <= 80:
            raise HTTPException(422, "Nama role 2-80 karakter")
        bentrok = await session.scalar(select(Role.id).where(func.lower(Role.nama) == nama.lower(), Role.id != role.id))
        if bentrok:
            raise HTTPException(409, "Nama role sudah dipakai")
        role.nama = nama
    role.keterangan = (keterangan or "").strip()[:255]
    role.akses_toko = akses
    lama = {h.halaman: h for h in (await session.scalars(select(RoleHalaman).where(RoleHalaman.role_id == role.id))).all()}
    for k, h in lama.items():
        if k not in bersih:
            await session.delete(h)
    for k, t in bersih.items():
        if k in lama:
            lama[k].tingkat = t
        else:
            session.add(RoleHalaman(role_id=role.id, halaman=k, tingkat=t))
    await session.commit()


async def hapus(session: AsyncSession, role_id: str) -> None:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(404, "Role tidak ditemukan")
    if role.sistem:
        raise HTTPException(409, "Role bawaan tidak bisa dihapus")
    if await _jumlah_pengguna(session, role.id):
        raise HTTPException(409, "Role masih dipakai pengguna; pindahkan penggunanya dulu")
    for h in (await session.scalars(select(RoleHalaman).where(RoleHalaman.role_id == role.id))).all():
        await session.delete(h)
    await session.delete(role)
    await session.commit()


async def pastikan_role_ada(session: AsyncSession, role_id: str) -> None:
    if await _role(session, _norm(role_id)) is None:
        raise HTTPException(422, "Role tidak ditemukan")
