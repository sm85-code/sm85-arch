"""Texts of the app (menu names, page titles, tab names, row names...) that the admin may change on Kelola Aplikasi.

Each text has a fixed id chosen by the app; only the text shown for it is stored here. An id without an entry shows the app's
own default, and removing an entry brings the default back. The ids are the app's business (nothing here lists them), so
the app can add new editable texts without a change on the server.
"""
from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PengaturanUi

# Ids are made by the app and may carry a group title (spaces, &, brackets); only control characters are refused.
ID_POLA = re.compile(r"^[^\x00-\x1f\x7f]{1,96}$")
MAKS_ENTRI = 2000


def _id(id_: str) -> str:
    if not ID_POLA.fullmatch(id_ or ""):
        raise HTTPException(422, "ID teks tidak valid")
    return id_


def _teks(teks: str) -> str:
    teks = re.sub(r"\s+", " ", (teks or "").strip())
    if not 1 <= len(teks) <= 120:
        raise HTTPException(422, "Teks 1-120 karakter")
    return teks


async def daftar(session: AsyncSession) -> dict:
    rows = (await session.scalars(select(PengaturanUi).order_by(PengaturanUi.id))).all()
    return {"items": {r.id: r.teks for r in rows}}


async def simpan(session: AsyncSession, id_: str, teks: str) -> dict:
    id_, teks = _id(id_), _teks(teks)
    row = await session.get(PengaturanUi, id_)
    if row is None:
        if ((await session.scalar(select(func.count()).select_from(PengaturanUi))) or 0) >= MAKS_ENTRI:
            raise HTTPException(409, "Terlalu banyak teks yang diubah")
        session.add(PengaturanUi(id=id_, teks=teks))
    else:
        row.teks = teks
    await session.commit()
    return {"id": id_, "teks": teks}


async def kembalikan(session: AsyncSession, id_: str) -> None:
    row = await session.get(PengaturanUi, _id(id_))
    if row is not None:
        await session.delete(row)
        await session.commit()
