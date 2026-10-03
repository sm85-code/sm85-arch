"""HTTP surface for bumi_lestari -- Tahap 1 (auth, akun kas, kategori, transaksi, transfer, kas kecil).

Mounted in main.py as prefix=/api/bumi-lestari.
Peran: owner = akses penuh; staff (pemegang kas kecil) = hanya pengeluaran & riwayat kas kecil.
"""
from __future__ import annotations

import os
import secrets as pysecrets
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

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
    TransaksiIn,
    TransaksiOut,
    TransferIn,
    TransferOut,
    UserCreateIn,
    UserOut,
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

OWNER_ONLY = ("owner",)
OWNER_OR_STAFF = ("owner", "staff")


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
    if (user.role or "").strip().lower() != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
    return user


@bumi_lestari_router.get("/seed-now")
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
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.create_user(session, payload)


# --- Akun kas ------------------------------------------------------------------


@bumi_lestari_router.get("/akun-kas", response_model=list[AkunKasOut])
async def list_akun_kas(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_OR_STAFF)),
):
    rows = await services.list_akun(session, user)
    return [AkunKasOut.model_validate(a).model_copy(update={"saldo": saldo}) for a, saldo in rows]


@bumi_lestari_router.post("/akun-kas", response_model=AkunKasOut, status_code=status.HTTP_201_CREATED)
async def create_akun_kas(
    payload: AkunKasIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    akun = await services.create_akun(session, payload)
    return AkunKasOut.model_validate(akun).model_copy(update={"saldo": akun.saldo_awal})


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
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_OR_STAFF)),
):
    return await services.list_transaksi(
        session, user, akun_id=akun_id, dari=dari, sampai=sampai, termasuk_batal=termasuk_batal
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
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.batalkan_transaksi(session, transaksi_id, payload.alasan)


# --- Transfer & kas kecil ----------------------------------------------------------


@bumi_lestari_router.post("/transfer", response_model=TransferOut, status_code=status.HTTP_201_CREATED)
async def create_transfer(
    payload: TransferIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.create_transfer(session, user, payload)


@bumi_lestari_router.post("/transfer/{transfer_id}/batal", response_model=TransferOut)
async def batalkan_transfer(
    transfer_id: str,
    payload: BatalIn,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.batalkan_transfer(session, transfer_id, payload.alasan)


@bumi_lestari_router.get("/kas-kecil/pengisian", response_model=PengisianKasKecilOut)
async def hitung_pengisian(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.hitung_pengisian_kas_kecil(session)


@bumi_lestari_router.post("/kas-kecil/pengisian", response_model=TransferOut, status_code=status.HTTP_201_CREATED)
async def catat_pengisian(
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_ONLY)),
):
    return await services.catat_pengisian_kas_kecil(session, user)
