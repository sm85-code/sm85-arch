"""Public business identity, independent of courier pickup configuration."""
import json
import re
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import select

from ..infrastructure.models import ProfilTokoStore

DEFAULT_PROFILE = {
    "nama": "AmpelKuning", "email": "ampelkuningdotcom@gmail.com", "telepon": "0813-1351-1101",
    "whatsapp": "6281313511101", "jalan": "Jl. Ampelkuning, Dusun Padasuka RT. 003 RW. 019",
    "desa": "Desa Wonoharjo", "kecamatan": "Kec. Pangandaran", "kabupaten": "Kab. Pangandaran",
    "provinsi": "Jawa Barat", "kodePos": "46396", "jam": "Senin – Sabtu, pukul 08.00 – 16.00 WIB",
    "instagram": "https://www.instagram.com/ampelkuningdotcom", "instagramNama": "@ampelkuningdotcom",
    "tiktok": "https://www.tiktok.com/@ampelkuningdotcom", "tiktokNama": "@ampelkuningdotcom",
}


class ProfilTokoIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    nama: str = Field(min_length=1, max_length=128)
    email: EmailStr
    telepon: str = Field(min_length=6, max_length=32)
    whatsapp: str = Field(min_length=8, max_length=15)
    jalan: str = Field(min_length=1, max_length=500)
    desa: str = Field(min_length=1, max_length=128)
    kecamatan: str = Field(min_length=1, max_length=128)
    kabupaten: str = Field(min_length=1, max_length=128)
    provinsi: str = Field(min_length=1, max_length=128)
    kodePos: str = Field(pattern=r"^\d{5}$")
    jam: str = Field(min_length=1, max_length=255)
    instagram: str = Field(default="", max_length=500)
    instagramNama: str = Field(default="", max_length=128)
    tiktok: str = Field(default="", max_length=500)
    tiktokNama: str = Field(default="", max_length=128)

    @field_validator("whatsapp")
    @classmethod
    def phone_digits(cls, value):
        if not re.fullmatch(r"[1-9][0-9]{7,14}", value):
            raise ValueError("WhatsApp harus angka dengan kode negara, misalnya 6281313511101")
        return value

    @field_validator("telepon")
    @classmethod
    def phone_display(cls, value):
        if not re.fullmatch(r"[+0-9 ()-]+", value) or len(re.sub(r"\D", "", value)) < 6:
            raise ValueError("Nomor telepon tidak valid")
        return value

    @field_validator("instagram", "tiktok")
    @classmethod
    def safe_social_url(cls, value, info):
        if not value:
            return value
        url = urlsplit(value)
        host = "instagram.com" if info.field_name == "instagram" else "tiktok.com"
        if url.scheme != "https" or url.hostname not in {host, f"www.{host}"} or url.username or url.password or url.port not in {None, 443}:
            raise ValueError(f"Gunakan URL HTTPS {host}")
        return value


async def get_profile(session):
    row = (await session.execute(select(ProfilTokoStore).where(ProfilTokoStore.id == "global"))).scalar_one_or_none()
    return {**DEFAULT_PROFILE, **json.loads(row.data)} if row else dict(DEFAULT_PROFILE)


async def save_profile(session, payload: ProfilTokoIn):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise HTTPException(501, "Database profil toko tidak didukung")
    values = payload.model_dump(mode="json")
    stmt = insert(ProfilTokoStore).values(id="global", data=json.dumps(values))
    await session.execute(stmt.on_conflict_do_update(index_elements=[ProfilTokoStore.id], set_={"data": stmt.excluded.data}))
    await session.flush()
    return values
