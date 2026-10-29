"""Create the ads-agent login. No password is stored in source, and hermes-ai is never touched."""
from __future__ import annotations

import os

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import akses, services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import UserCreateIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import StaffAkunMarketplace

DILARANG = "hermes-ai"


class PenggunaAgenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nama: str = Field(default="Agen Iklan", max_length=255)
    username: str
    password: str
    akun_ids: list[str] = Field(default_factory=list)


def tolak_hermes(username: str) -> None:
    if username.strip().lower() == DILARANG:
        raise HTTPException(status_code=409, detail="Pengguna hermes-ai tidak diubah oleh perintah ini")


async def buat(session: AsyncSession, body: PenggunaAgenIn) -> dict:
    tolak_hermes(body.username)
    await akses.pastikan_bawaan(session)
    user = await services.create_user(session, UserCreateIn(nama=body.nama, username=body.username, password=body.password, role="agen_iklan"))
    for akun_id in dict.fromkeys(body.akun_ids):
        await services.get_akun_marketplace(session, akun_id)
        sudah = (await session.execute(
            select(StaffAkunMarketplace).where(StaffAkunMarketplace.user_id == user.id, StaffAkunMarketplace.akun_id == akun_id)
        )).scalar_one_or_none()
        if sudah is None:
            session.add(StaffAkunMarketplace(user_id=user.id, akun_id=akun_id))
    await session.flush()
    return {"id": user.id, "username": user.username, "role": user.role, "akun_ids": list(dict.fromkeys(body.akun_ids))}


def periksa_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Read the command's inputs. Missing password or the hermes-ai name stops before any database call."""
    sumber = os.environ if env is None else env
    username = sumber.get("AGEN_IKLAN_USERNAME", "").strip()
    password = sumber.get("AGEN_IKLAN_PASSWORD", "")
    if username.lower() == DILARANG:
        raise HTTPException(status_code=409, detail="Pengguna hermes-ai tidak diubah oleh perintah ini")
    if not username or not password:
        raise HTTPException(status_code=422, detail="Isi AGEN_IKLAN_USERNAME dan AGEN_IKLAN_PASSWORD")
    akun = list(dict.fromkeys(a.strip() for a in sumber.get("AGEN_IKLAN_AKUN", "").split(",") if a.strip()))
    return {"username": username, "password": password, "akun_ids": akun}
