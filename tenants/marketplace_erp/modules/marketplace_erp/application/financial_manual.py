"""Manual figures typed straight into the report sheet.

Stored per month as one document: `sel` maps "<row>|<week start>" to a typed amount, `baris` lists rows the user added
and `hapus` the built-in rows the user removed. The sheet applies it on top of the closed books and recalculates totals.
"""

import json
import re
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import LaporanKeuanganManual

# "arus_rinci" holds the breakdown of the cash-flow page (renamed lines and split rows); it never feeds the report totals.
TABEL = ("pemasukan", "pengeluaran", "arus_kas", "neraca", "arus_rinci")
KUNCI_SEL = re.compile(r"^[^|\x00-\x1f]{1,120}\|\d{4}-\d{2}-\d{2}$")  # row keys include shop names, e.g. "shop:Pemasukan Toko [RISKA]"
MAX_BARIS, MAX_SEL = 200, 5000


class Baris(BaseModel):
    id: str = Field(pattern=r"^c:[\w\-]{1,40}$")
    tabel: str
    grup: str = Field(max_length=80)
    label: str = Field(min_length=1, max_length=120)

    @field_validator("tabel")
    @classmethod
    def known_table(cls, v):
        if v not in TABEL:
            raise ValueError("Tabel tidak dikenal")
        return v


class Lembar(BaseModel):
    sel: dict[str, str] = Field(default_factory=dict)
    baris: list[Baris] = Field(default_factory=list, max_length=MAX_BARIS)
    hapus: list[str] = Field(default_factory=list, max_length=MAX_BARIS)

    @field_validator("sel")
    @classmethod
    def valid_cells(cls, cells):
        if len(cells) > MAX_SEL:
            raise ValueError("Terlalu banyak sel")
        for key, value in cells.items():
            if not KUNCI_SEL.match(key):
                raise ValueError(f"Kunci sel tidak valid: {key[:40]}")
            try:
                amount = Decimal(value)
            except InvalidOperation:
                raise ValueError(f"Angka tidak valid: {value[:20]}")
            if not amount.is_finite() or abs(amount) > Decimal("999999999999999"):
                raise ValueError("Angka di luar batas")
        return cells


class Simpan(BaseModel):
    version: int = Field(ge=0)
    lembar: Lembar


async def muat(session: AsyncSession, periode: str):
    row = await session.get(LaporanKeuanganManual, periode)
    if row is None:
        return {"version": 0, "lembar": Lembar().model_dump()}
    return {"version": row.version, "lembar": json.loads(row.data_json)}


async def simpan(session: AsyncSession, periode: str, payload: Simpan, actor):
    row = await session.scalar(select(LaporanKeuanganManual).where(LaporanKeuanganManual.periode == periode).with_for_update())
    current = row.version if row else 0
    if payload.version != current:
        raise HTTPException(409, "Lembar diubah di tempat lain; muat ulang sebelum menyimpan")
    text = payload.lembar.model_dump_json()
    if row is None:
        session.add(LaporanKeuanganManual(periode=periode, data_json=text, version=1, updated_by=actor.id))
    else:
        row.data_json, row.version, row.updated_by = text, current + 1, actor.id
    await session.commit()
    return {"version": current + 1, "lembar": payload.lembar.model_dump()}
