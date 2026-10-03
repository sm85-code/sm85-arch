"""HTTP surface for bumi_lestari -- Tahap 1 (auth, akun kas, kategori, transaksi, transfer, kas kecil).

Mounted in main.py as prefix=/api/bumi-lestari.
Peran: admin (di atas owner) = akses owner + boleh membuat akun admin/owner; owner = akses penuh keuangan; staff (pemegang kas kecil) = hanya pengeluaran & riwayat kas kecil.
"""
from __future__ import annotations

import os
import secrets as pysecrets
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.adapters.api.v1.bumi_lestari_dokumen_router import dokumen_router
from tenants.bumi_lestari.adapters.api.v1.bumi_lestari_kiriman_router import kiriman_router
from tenants.bumi_lestari.adapters.api.v1.bumi_lestari_laporan_router import laporan_router
from tenants.bumi_lestari.adapters.api.v1.bumi_lestari_order_router import order_router
from tenants.bumi_lestari.adapters.api.v1.bumi_lestari_pembayaran_router import pembayaran_router
from tenants.bumi_lestari.adapters.api.v1.bumi_lestari_pencairan_router import pencairan_router
from tenants.bumi_lestari.adapters.api.v1.bumi_lestari_tutup_buku_router import tutup_buku_router
from tenants.bumi_lestari.modules.bumi_lestari.application import services
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import (
    AkunKasIn,
    AkunKasOut,
    BatalIn,
    ChangePasswordIn,
    KategoriIn,
    KategoriOut,
    LoginIn,
    PengisianKasKecilOut,
    ProfilIn,
    ProfilOut,
    ResetPasswordIn,
    ProporsiIn,
    ProporsiItemOut,
    TransaksiIn,
    TransaksiOut,
    TransferIn,
    TransferOut,
    UserCreateIn,
    UserOut,
    UserPatchIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import (
    clear_bumi_lestari_cookie,
    get_current_user_bumi_lestari,
    issue_bumi_lestari_token,
    require_roles_bumi_lestari,
    set_bumi_lestari_cookie,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.seeder import seed_bumi_lestari

bumi_lestari_router = APIRouter()
bumi_lestari_router.include_router(order_router)
bumi_lestari_router.include_router(pembayaran_router)
bumi_lestari_router.include_router(dokumen_router)
bumi_lestari_router.include_router(laporan_router)
bumi_lestari_router.include_router(kiriman_router)
bumi_lestari_router.include_router(tutup_buku_router)
bumi_lestari_router.include_router(pencairan_router)

OWNER_ONLY = ("admin", "owner")  # admin berada di atas owner: semua akses owner + kelola akun owner
OWNER_OR_STAFF = ("admin", "owner", "staff")


# --- Seed (owner login OR secret header; never anonymous in production) -----------


def _seed_secret_ok(request: Request) -> bool:
    expected = (os.getenv("BUMI_LESTARI_SEED_SECRET") or "").strip()
    provided = (
        request.headers.get("X-Bumi-Lestari-Seed-Secret") or request.headers.get("X-Seed-Secret") or ""
    ).strip()
    return bool(expected and provided and pysecrets.compare_digest(provided, expected))


async def authorize_bumi_lestari_seed(
    request: Request, session: AsyncSession = Depends(get_db_bumi_lestari)
) -> BlUser | None:
    if _seed_secret_ok(request):
        return None
    try:
        user = await get_current_user_bumi_lestari(request, session)
    except HTTPException:
        if (os.getenv("APP_ENV") or "").strip().lower() == "production":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="seed-now dinonaktifkan untuk publik di production. Login sebagai owner atau pakai seed secret.",
            )
        raise
    if (user.role or "").strip().lower() not in OWNER_ONLY:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
    return user


@bumi_lestari_router.post("/seed-now")
async def seed_now(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser | None = Depends(authorize_bumi_lestari_seed),
):
    try:
        return await seed_bumi_lestari(session)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc


# --- Auth ----------------------------------------------------------------------


@bumi_lestari_router.post("/auth/login", response_model=UserOut)
async def login(payload: LoginIn, response: Response, session: AsyncSession = Depends(get_db_bumi_lestari)):
    user = await services.authenticate_user(session, payload)
    set_bumi_lestari_cookie(response, issue_bumi_lestari_token(user))
    return user


@bumi_lestari_router.post("/auth/logout")
async def logout(response: Response):
    clear_bumi_lestari_cookie(response)
    return {"ok": True}


@bumi_lestari_router.post("/auth/logout-semua")
async def logout_semua(
    response: Response,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(get_current_user_bumi_lestari),
):
    """Keluar dari semua perangkat: semua token pengguna ini dicabut."""
    services.naikkan_versi_sesi(user)
    await session.flush()
    clear_bumi_lestari_cookie(response)
    return {"ok": True}


@bumi_lestari_router.get("/auth/me", response_model=UserOut)
async def me(user: BlUser = Depends(get_current_user_bumi_lestari)):
    return user


@bumi_lestari_router.post("/auth/change-password", response_model=UserOut)
async def change_password(
    payload: ChangePasswordIn,
    response: Response,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(get_current_user_bumi_lestari),
):
    user = await services.change_password(session, user, payload)
    set_bumi_lestari_cookie(response, issue_bumi_lestari_token(user))
    return user


@bumi_lestari_router.get("/users", response_model=list[UserOut])
async def list_users(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.list_users(session)


@bumi_lestari_router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    actor: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.create_user(session, actor, payload)


@bumi_lestari_router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    payload: UserPatchIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    actor: BlUser = Depends(require_roles_bumi_lestari("admin")),
):
    return await services.update_user(session, actor, user_id, payload)


@bumi_lestari_router.post("/users/{user_id}/reset-password", response_model=UserOut)
async def reset_user_password(
    user_id: str,
    payload: ResetPasswordIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari("admin")),
):
    return await services.reset_password(session, user_id, payload)


# --- Akun kas ------------------------------------------------------------------


def _akun_out(akun, saldo, saldo_setelah_draf=None) -> AkunKasOut:
    return AkunKasOut(
        id=akun.id, kode=akun.kode, nama=akun.nama, jenis=akun.jenis, saldo_awal=akun.saldo_awal,
        plafon=akun.plafon, aktif=akun.aktif, saldo=saldo,
        saldo_setelah_draf=saldo if saldo_setelah_draf is None else saldo_setelah_draf,
    )


@bumi_lestari_router.get("/akun-kas", response_model=list[AkunKasOut])
async def list_akun_kas(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_OR_STAFF)),
):
    rows = await services.list_akun(session, user)
    return [_akun_out(a, saldo, fisik) for a, saldo, fisik in rows]


@bumi_lestari_router.post("/akun-kas", response_model=AkunKasOut, status_code=status.HTTP_201_CREATED)
async def create_akun_kas(
    payload: AkunKasIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    akun = await services.create_akun(session, payload)
    return _akun_out(akun, akun.saldo_awal)


# --- Kategori ------------------------------------------------------------------


@bumi_lestari_router.get("/kategori", response_model=list[KategoriOut])
async def list_kategori(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_OR_STAFF)),
):
    return await services.list_kategori(session)


@bumi_lestari_router.post("/kategori", response_model=KategoriOut, status_code=status.HTTP_201_CREATED)
async def create_kategori(
    payload: KategoriIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.create_kategori(session, payload)


# --- Transaksi -----------------------------------------------------------------


@bumi_lestari_router.get("/transaksi", response_model=list[TransaksiOut])
async def list_transaksi(
    akun_id: str | None = None,
    dari: date | None = None,
    sampai: date | None = None,
    termasuk_batal: bool = False,
    termasuk_draf: bool = False,
    hanya_draf: bool = False,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_OR_STAFF)),
):
    return await services.list_transaksi(
        session, user, akun_id=akun_id, dari=dari, sampai=sampai, termasuk_batal=termasuk_batal,
        termasuk_draf=termasuk_draf, hanya_draf=hanya_draf,
    )


@bumi_lestari_router.post("/transaksi", response_model=TransaksiOut, status_code=status.HTTP_201_CREATED)
async def create_transaksi(
    payload: TransaksiIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_OR_STAFF)),
):
    return await services.create_transaksi(session, user, payload)


@bumi_lestari_router.post("/transaksi/{transaksi_id}/batal", response_model=TransaksiOut)
async def batalkan_transaksi(
    transaksi_id: str,
    payload: BatalIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),  # staf tidak bisa membatalkan
):
    return await services.batalkan_transaksi(session, transaksi_id, payload.alasan, user)


# --- Transfer & kas kecil ----------------------------------------------------------


@bumi_lestari_router.post("/transfer", response_model=TransferOut, status_code=status.HTTP_201_CREATED)
async def create_transfer(
    payload: TransferIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.create_transfer(session, user, payload)


@bumi_lestari_router.get("/transfer", response_model=list[TransferOut])
async def list_transfer(
    dari: date | None = None,
    sampai: date | None = None,
    termasuk_batal: bool = False,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.list_transfer(session, user, dari=dari, sampai=sampai, termasuk_batal=termasuk_batal)


@bumi_lestari_router.post("/transfer/{transfer_id}/batal", response_model=TransferOut)
async def batalkan_transfer(
    transfer_id: str,
    payload: BatalIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.batalkan_transfer(session, transfer_id, payload.alasan, user)


@bumi_lestari_router.get("/kas-kecil/pengisian", response_model=PengisianKasKecilOut)
async def hitung_pengisian(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.hitung_pengisian_kas_kecil(session)


@bumi_lestari_router.post("/kas-kecil/pengisian", response_model=TransferOut, status_code=status.HTTP_201_CREATED)
async def catat_pengisian(
    tanggal: date | None = None,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.catat_pengisian_kas_kecil(session, user, tanggal)


@bumi_lestari_router.get("/kas-iklan/pengisian", response_model=PengisianKasKecilOut)
async def hitung_pengisian_iklan(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari("admin")),  # kas iklan: hanya admin
):
    return await services.hitung_pengisian(session, "kas_iklan")


@bumi_lestari_router.post("/kas-iklan/pengisian", response_model=TransferOut, status_code=status.HTTP_201_CREATED)
async def catat_pengisian_iklan(
    tanggal: date | None = None,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari("admin")),  # kas iklan: hanya admin
):
    return await services.catat_pengisian(session, user, "kas_iklan", tanggal)


# --- Profil UMKM & proporsi bagi hasil -------------------------------------------------


async def _profil_out(session: AsyncSession) -> ProfilOut:
    profil = await services.get_profil(session)
    proporsi = [ProporsiItemOut.model_validate(p) for p in await services.get_proporsi(session)]
    return ProfilOut(
        nama_usaha=profil.nama_usaha, alamat=profil.alamat, telepon=profil.telepon,
        email=profil.email, catatan=profil.catatan, biaya_proses_order=profil.biaya_proses_order,
        info_pembayaran=profil.info_pembayaran,
        nama_usaha_lama=profil.nama_usaha_lama, nama_usaha_berlaku_mulai=profil.nama_usaha_berlaku_mulai, proporsi_bagi_hasil=proporsi,
    )


@bumi_lestari_router.get("/profil", response_model=ProfilOut)
async def get_profil(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await _profil_out(session)


@bumi_lestari_router.put("/profil", response_model=ProfilOut)
async def update_profil(
    payload: ProfilIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari("admin")),
):
    await services.update_profil(session, payload)
    return await _profil_out(session)


@bumi_lestari_router.put("/profil/proporsi-bagi-hasil", response_model=ProfilOut)
async def set_proporsi_bagi_hasil(
    payload: ProporsiIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari("admin")),
):
    await services.set_proporsi(session, payload)
    return await _profil_out(session)
