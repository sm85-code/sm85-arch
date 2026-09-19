"""Pydantic payloads for the madrasah HTTP API."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    no_hp: str = Field(..., min_length=8, max_length=32)
    password: str = Field(..., min_length=4)


class AbsenItem(BaseModel):
    santri_id: str
    status: Literal["hadir", "sakit", "izin", "alpa"]


class AbsenBulkRequest(BaseModel):
    tanggal: date
    guru_id: str
    items: list[AbsenItem] = Field(..., min_length=1)


class ProgresCreateRequest(BaseModel):
    tanggal: date
    santri_id: str
    tipe: str = Field(..., min_length=1, max_length=64)
    capaian: str = Field(..., min_length=1, max_length=255)
    catatan_guru: str = ""
