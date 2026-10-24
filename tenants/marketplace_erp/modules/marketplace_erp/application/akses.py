"""Roles defined by access: which shops, which pages, and read or write per page.

A role (mpe_role) has `akses_toko` ("semua" or "ditugaskan") and a level ("baca" or "tulis") for each page it may open
(mpe_role_halaman). `admin` is built in and always has everything; `owner` and `staff` are built in too but an admin
can edit what they may do. Every page follows these settings (see peta_akses.py for which routes belong to which page).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable, Iterable

from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    PengaturanUi,
    PeranTambahan,
    Role,
    RoleHalaman,
    UserMarketplaceErp,
)

PENANDA_TAB = "migrasi:pemetaan_per_tab"
PENANDA_MENU = "migrasi:data_produksi_per_menu"
PENANDA_SUPPLIER = "migrasi:belanja_supplier"
PENANDA_TUKANG = "migrasi:belanja_tukang"
PENANDA_PISAH = "migrasi:data_produksi"  # a row of Kelola Aplikasi's store used as a one-time marker; never shown
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
    ("processing_tukang", "Pemetaan Order: tab Tukang Kayu", "Processing", ("baca", "tulis")),
    ("processing_supplier", "Pemetaan Order: tab Supplier", "Processing", ("baca", "tulis")),
    ("processing_pengecatan", "Pemetaan Order: tab Pengecatan", "Processing", ("baca", "tulis")),
    ("processing_packing", "Pemetaan Order: tab Packing", "Processing", ("baca", "tulis")),
    ("data_tukang", "Tukang Kayu", "Data Produksi", ("baca", "tulis")),
    ("data_supplier", "Supplier", "Data Produksi", ("baca", "tulis")),
    ("data_produk_kayu", "Katalog Produk Kayu", "Data Produksi", ("baca", "tulis")),
    ("data_katalog_otomatis", "Katalog Produk Non Kayu: tab Katalog Otomatis", "Data Produksi", ("baca", "tulis")),
    ("data_katalog_manual", "Katalog Produk Non Kayu: tab Katalog Manual", "Data Produksi", ("baca", "tulis")),
    ("data_tarif_pengecatan", "Tarif Pengecatan", "Data Produksi", ("baca", "tulis")),
    ("data_tarif_packing", "Tarif Packing", "Data Produksi", ("baca", "tulis")),
    ("data_template", "Template", "Data Produksi", ("baca", "tulis")),
    ("operasional_umum", "Kas Utama", "Keuangan", ("baca", "tulis")),
    ("operasional_packing", "Kas Kecil", "Keuangan", ("baca", "tulis")),
    ("belanja_supplier", "Belanja Supplier", "Keuangan", ("baca", "tulis")),
    ("belanja_tukang", "Belanja Tukang", "Keuangan", ("baca", "tulis")),
    ("arus_kas", "Arus Kas (tulis = centang)", "Keuangan", ("baca", "tulis")),
    ("kategori_transaksi", "Kategori Transaksi", "Keuangan", ("baca", "tulis")),
    ("laporan_keuangan", "Laporan Keuangan", "Keuangan", ("baca", "tulis")),
    ("settlement", "Dana & Saldo Shopee", "Keuangan", ("baca", "tulis")),
    ("info_toko", "Performa dan Pengaturan Toko", "Info Toko", ("baca", "tulis")),
    ("otomasi", "Otomasi produk dan pesanan", "Info Toko", ("baca", "tulis")),
    ("iklan", "Iklan", "Iklan", ("baca", "tulis")),
    ("kelola_toko", "Kelola Toko", "Pengaturan", ("baca", "tulis")),
    ("push_shopee", "Pengaturan push Shopee", "Pengaturan", ("baca", "tulis")),
    ("asisten", "Asisten AI", "Pengaturan", ("baca", "tulis")),
    ("kelola_pengguna", "Kelola Pengguna dan Role", "Pengaturan", ("baca", "tulis")),
    ("kelola_aplikasi", "Kelola Aplikasi (teks menu dan halaman)", "Pengaturan", ("baca", "tulis")),
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
    {"id": "staf_produksi", "nama": "Staf Produksi", "keterangan": "Mencatat operasional eceran", "akses_toko": "semua", "sistem": False,
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
    await _pisah_data_produksi(session)
    await _pisah_pemetaan(session)
    await _beri_belanja_supplier(session)
    await _beri_dari_operasional_umum(session, "belanja_tukang", PENANDA_TUKANG)


async def _beri_belanja_supplier(session: AsyncSession) -> None:
    """Once: Belanja Supplier is new. Every role that has Operasional Umum gets it at the same level; nobody loses
    anything, and the marker keeps a later removal by the admin from being given back."""
    if await session.get(PengaturanUi, PENANDA_SUPPLIER):
        return
    for baris in (await session.scalars(select(RoleHalaman).where(RoleHalaman.halaman == "operasional_umum"))).all():
        if await session.get(RoleHalaman, (baris.role_id, "belanja_supplier")) is None:
            session.add(RoleHalaman(role_id=baris.role_id, halaman="belanja_supplier", tingkat=baris.tingkat))
    session.add(PengaturanUi(id=PENANDA_SUPPLIER, teks="1"))
    await session.flush()



async def _beri_dari_operasional_umum(session: AsyncSession, halaman: str, penanda: str) -> None:
    """Once per new page: every role that has Operasional Umum gets it at the same level (same rule as Belanja Supplier)."""
    if await session.get(PengaturanUi, penanda):
        return
    for baris in (await session.scalars(select(RoleHalaman).where(RoleHalaman.halaman == "operasional_umum"))).all():
        if await session.get(RoleHalaman, (baris.role_id, halaman)) is None:
            session.add(RoleHalaman(role_id=baris.role_id, halaman=halaman, tingkat=baris.tingkat))
    session.add(PengaturanUi(id=penanda, teks="1"))
    await session.flush()


async def _pisah_pemetaan(session: AsyncSession) -> None:
    """Once: Pemetaan Order was one page. A role that had it keeps all four tabs (at the same level); the admin can then
    take any of them away. The marker keeps a later removal from being given back."""
    if await session.get(PengaturanUi, PENANDA_TAB):
        return
    tab = [k for k, _, g, _ in HALAMAN if k.startswith("processing_")]
    for baris in (await session.scalars(select(RoleHalaman).where(RoleHalaman.halaman == "processing"))).all():
        for h in tab:
            if await session.get(RoleHalaman, (baris.role_id, h)) is None:
                session.add(RoleHalaman(role_id=baris.role_id, halaman=h, tingkat=baris.tingkat))
        await session.delete(baris)
    session.add(PengaturanUi(id=PENANDA_TAB, teks="1"))
    await session.flush()


async def _pisah_data_produksi(session: AsyncSession) -> None:
    """Once: Data Produksi used to be part of Pemetaan Order. A role that had that page keeps every Data Produksi menu, so
    nobody loses a screen; the admin can then take any of them away. The marker keeps a later removal from being given back."""
    if await session.get(PengaturanUi, PENANDA_MENU):
        return
    data = [k for k, _, g, _ in HALAMAN if g == "Data Produksi"]
    for baris in (await session.scalars(select(RoleHalaman).where(RoleHalaman.halaman.in_(["processing", "data_produksi"])))).all():
        if baris.halaman == "data_produksi" or not await session.get(PengaturanUi, PENANDA_PISAH):
            for h in data:
                if await session.get(RoleHalaman, (baris.role_id, h)) is None:
                    session.add(RoleHalaman(role_id=baris.role_id, halaman=h, tingkat=baris.tingkat))
        if baris.halaman == "data_produksi":
            await session.delete(baris)  # the short-lived single page, replaced by one per menu
    for marker in (PENANDA_PISAH, PENANDA_MENU):
        if await session.get(PengaturanUi, marker) is None:
            session.add(PengaturanUi(id=marker, teks="1"))
    await session.flush()


async def _role(session: AsyncSession, role_id: str) -> Role | None:
    role = await session.get(Role, role_id)
    if role is None and role_id in ("admin", "owner", "staff"):
        await pastikan_bawaan(session)  # a database from before roles existed
        role = await session.get(Role, role_id)
    return role


def _daftar_role(role_id: str | Iterable[str] | None) -> list[str]:
    """One role id or several (an account may hold more than one), normalised and without repeats."""
    ids = [role_id] if isinstance(role_id, str) or role_id is None else list(role_id)
    return list(dict.fromkeys(r for r in (_norm(x) for x in ids) if r))


async def peran_pengguna(session: AsyncSession, user: UserMarketplaceErp) -> list[str]:
    """The main role first, then the extra ones the account holds."""
    extra = (await session.scalars(select(PeranTambahan.role_id).where(PeranTambahan.user_id == user.id).order_by(PeranTambahan.role_id))).all()
    return _daftar_role([user.role, *extra])


async def tingkat_halaman(session: AsyncSession, role_id: str | Iterable[str] | None, halaman: str) -> str | None:
    """"tulis", "baca" or None on this page. With several roles the most generous one counts. Admin always has "tulis"."""
    if "|" in halaman:  # a screen that either page may open
        tingkat = [await tingkat_halaman(session, role_id, h) for h in halaman.split("|")]
        return "tulis" if "tulis" in tingkat else ("baca" if "baca" in tingkat else None)
    terbaik: str | None = None
    for rid in _daftar_role(role_id):
        if rid == ADMIN:
            return "tulis"
        if await _role(session, rid) is None:
            continue
        row = await session.get(RoleHalaman, (rid, halaman))
        if row and (terbaik is None or row.tingkat == "tulis"):
            terbaik = row.tingkat
    return terbaik


async def akses_toko(session: AsyncSession, role_id: str | Iterable[str] | None) -> str | None:
    """"semua", "ditugaskan", or None when no role exists. With several roles "semua" wins over "ditugaskan"."""
    hasil: str | None = None
    for rid in _daftar_role(role_id):
        if rid == ADMIN:
            return "semua"
        role = await _role(session, rid)
        if role is None:
            continue
        if role.akses_toko == "semua":
            return "semua"
        hasil = role.akses_toko
    return hasil


def butuh_halaman(halaman: str, perlu: str) -> Callable:
    """FastAPI dependency: the signed-in user's role must reach `halaman` at level `perlu` ("baca" or "tulis")."""
    assert halaman in TINGKAT_HALAMAN and perlu in TINGKAT

    async def _inner(
        user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
        session: AsyncSession = Depends(get_db_marketplace_erp),
    ) -> UserMarketplaceErp:
        punya = await tingkat_halaman(session, await peran_pengguna(session, user), halaman)
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
    utama = (await session.execute(select(func.count()).select_from(UserMarketplaceErp).where(UserMarketplaceErp.role == role_id))).scalar_one()
    tambahan = (await session.execute(select(func.count()).select_from(PeranTambahan).where(PeranTambahan.role_id == role_id))).scalar_one()
    return utama + tambahan


async def daftar(session: AsyncSession) -> dict:
    await pastikan_bawaan(session)
    roles = (await session.scalars(select(Role).order_by(Role.sistem.desc(), Role.nama))).all()
    izin: dict[str, dict[str, str]] = {r.id: {} for r in roles}
    for h in (await session.scalars(select(RoleHalaman))).all():
        izin.setdefault(h.role_id, {})[h.halaman] = h.tingkat
    jumlah = dict(
        (await session.execute(select(UserMarketplaceErp.role, func.count()).group_by(UserMarketplaceErp.role))).all()
    )
    for rid, n in (await session.execute(select(PeranTambahan.role_id, func.count()).group_by(PeranTambahan.role_id))).all():
        jumlah[rid] = jumlah.get(rid, 0) + n
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


async def akses_pengguna(session: AsyncSession, role_id: str | Iterable[str] | None) -> dict:
    """What the signed-in user's role(s) may do, for the app to show or hide menus and buttons. With several roles the
    pages and levels are merged (write beats read), "semua" shops beats "ditugaskan"; `role` is the main one."""
    ids = _daftar_role(role_id)
    utama = ids[0] if ids else ""
    if ADMIN in ids:
        return {"role": ADMIN, "roles": ids, "nama": "Admin", "akses_toko": "semua", "halaman": {k: lv[-1] for k, lv in TINGKAT_HALAMAN.items()}}
    roles = [r for r in [await _role(session, rid) for rid in ids] if r is not None]
    if not roles:
        return {"role": utama, "roles": ids, "nama": utama, "akses_toko": "ditugaskan", "halaman": {}}
    izin: dict[str, str] = {}
    for h in (await session.scalars(select(RoleHalaman).where(RoleHalaman.role_id.in_([r.id for r in roles])))).all():
        if izin.get(h.halaman) != "tulis":
            izin[h.halaman] = h.tingkat
    return {
        "role": roles[0].id, "roles": [r.id for r in roles], "nama": " + ".join(r.nama for r in roles),
        "akses_toko": "semua" if any(r.akses_toko == "semua" for r in roles) else roles[0].akses_toko, "halaman": izin,
    }


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


async def atur_peran_tambahan(session: AsyncSession, user: UserMarketplaceErp, role_ids: list[str]) -> list[str]:
    """Replace the extra roles of an account. The main role is not repeated, and Admin is never an extra (it is all or nothing)."""
    ids = [r for r in _daftar_role(role_ids) if r != _norm(user.role)]
    for rid in ids:
        if rid == ADMIN:
            raise HTTPException(422, "Admin tidak bisa dirangkap; jadikan role utama")
        await pastikan_role_ada(session, rid)
    if _norm(user.role) == ADMIN and ids:
        raise HTTPException(422, "Admin sudah punya semua akses; tidak perlu role tambahan")
    lama = {p.role_id: p for p in (await session.scalars(select(PeranTambahan).where(PeranTambahan.user_id == user.id))).all()}
    for rid, p in lama.items():
        if rid not in ids:
            await session.delete(p)
    for rid in ids:
        if rid not in lama:
            session.add(PeranTambahan(user_id=user.id, role_id=rid))
    await session.flush()
    return ids


async def muat_peran_tambahan(session: AsyncSession, users: list[UserMarketplaceErp]) -> None:
    """Attach `peran_tambahan` (list of role ids) to each account, for the response."""
    peta: dict[str, list[str]] = {}
    if users:
        for uid, rid in (await session.execute(select(PeranTambahan.user_id, PeranTambahan.role_id).where(PeranTambahan.user_id.in_([u.id for u in users])).order_by(PeranTambahan.role_id))).all():
            peta.setdefault(uid, []).append(rid)
    for u in users:
        u.peran_tambahan = peta.get(u.id, [])  # type: ignore[attr-defined]
