"""Per-shop ads limits, the STOP switch, and the audit log. No Shopee calls."""
from __future__ import annotations

import json
from decimal import Decimal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models_agen import (
    AgenIklanAudit,
    AgenIklanBatas,
    AgenIklanStop,
)

GLOBAL = "global"
PERSEN_BAWAAN = 20
ANGGARAN_MAKS = Decimal("1000000000")
CPC_MAKS = Decimal("100000")


class BatasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anggaran_harian_maks: Decimal | None = None
    maks_persen_perubahan: int = Field(PERSEN_BAWAAN, ge=1, le=100)
    maks_cpc: Decimal | None = None
    min_hari_antar_perubahan: int = Field(0, ge=0, le=90)
    otonomi: bool = False
    alasan: str = Field("", max_length=500)
    request_id: str | None = Field(None, max_length=64)


class StopIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aktif: bool
    alasan: str = Field("", max_length=500)
    request_id: str | None = Field(None, max_length=64)


def _uang(nilai: Decimal | None, nama: str, batas: Decimal) -> Decimal | None:
    if nilai is None:
        return None
    if not isinstance(nilai, Decimal):
        nilai = Decimal(str(nilai))
    if not nilai.is_finite() or nilai <= 0 or nilai > batas:
        raise HTTPException(status_code=422, detail=f"{nama} harus lebih dari 0 dan tidak melebihi {batas}")
    return nilai


def _angka(nilai):
    if nilai is None:
        return None
    n = float(nilai)
    return int(n) if n == int(n) else round(n, 2)


def _muat(teks: str):
    try:
        data = json.loads(teks or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {"nilai": data}


def _simpan(data: dict) -> str:
    return json.dumps(data, default=str, ensure_ascii=False, separators=(",", ":"))


def _siapa(pengguna) -> tuple[str | None, str]:
    return getattr(pengguna, "id", None), str(getattr(pengguna, "username", None) or getattr(pengguna, "nama", "") or "")[:255]


def batas_bawaan(akun_id: str) -> dict:
    return {
        "akun_id": akun_id,
        "anggaran_harian_maks": None,
        "maks_persen_perubahan": PERSEN_BAWAAN,
        "maks_cpc": None,
        "min_hari_antar_perubahan": 0,
        "otonomi": False,
        "tersimpan": False,
    }


def _batas_keluar(row: AgenIklanBatas) -> dict:
    return {
        "akun_id": row.akun_id,
        "anggaran_harian_maks": _angka(row.anggaran_harian_maks),
        "maks_persen_perubahan": row.maks_persen_perubahan,
        "maks_cpc": _angka(row.maks_cpc),
        "min_hari_antar_perubahan": row.min_hari_antar_perubahan,
        "otonomi": bool(row.otonomi),
        "tersimpan": True,
    }


def _stop_keluar(lingkup: str, row: AgenIklanStop | None) -> dict:
    return {
        "lingkup": lingkup,
        "aktif": bool(row.aktif) if row else False,
        "alasan": row.alasan if row else "",
        "oleh": row.oleh if row else None,
    }


async def baca_batas(session: AsyncSession, akun_id: str) -> dict:
    row = await session.get(AgenIklanBatas, akun_id)
    return _batas_keluar(row) if row else batas_bawaan(akun_id)


async def _sudah(session: AsyncSession, akun_id: str | None, request_id: str | None) -> AgenIklanAudit | None:
    if not request_id:
        return None
    kondisi = AgenIklanAudit.request_id == request_id
    kondisi = kondisi & (AgenIklanAudit.akun_id.is_(None) if akun_id is None else AgenIklanAudit.akun_id == akun_id)
    return (await session.execute(select(AgenIklanAudit).where(kondisi))).scalar_one_or_none()


async def _catat(session: AsyncSession, *, akun_id: str | None, aksi: str, pengguna, sebelum: dict, sesudah: dict,
                 alasan: str, request_id: str | None, kampanye_id: str | None = None, persetujuan: str | None = None,
                 kering: bool = False, hasil: str = "tersimpan") -> AgenIklanAudit:
    pelaku_id, pelaku_nama = _siapa(pengguna)
    ada = await _sudah(session, akun_id, request_id)
    if ada is not None:
        return ada
    row = AgenIklanAudit(
        akun_id=akun_id, kampanye_id=kampanye_id, aksi=aksi, pelaku_id=pelaku_id, pelaku_nama=pelaku_nama,
        sebelum_json=_simpan(sebelum), sesudah_json=_simpan(sesudah), alasan=alasan[:2000],
        request_id=request_id or None, persetujuan=persetujuan, kering=kering, hasil=hasil,
    )
    session.add(row)
    await session.flush()
    return row


def _audit_keluar(row: AgenIklanAudit) -> dict:
    return {
        "id": row.id,
        "akun_id": row.akun_id,
        "kampanye_id": row.kampanye_id,
        "aksi": row.aksi,
        "pelaku_id": row.pelaku_id,
        "pelaku_nama": row.pelaku_nama,
        "sebelum": _muat(row.sebelum_json),
        "sesudah": _muat(row.sesudah_json),
        "alasan": row.alasan,
        "request_id": row.request_id,
        "persetujuan": row.persetujuan,
        "kering": bool(row.kering),
        "hasil": row.hasil,
        "dibuat_at": row.dibuat_at,
    }


async def simpan_batas(session: AsyncSession, akun_id: str, body: BatasIn, pengguna) -> dict:
    ada = await _sudah(session, akun_id, body.request_id)
    if ada is not None:
        return _muat(ada.sesudah_json)
    anggaran = _uang(body.anggaran_harian_maks, "Anggaran harian", ANGGARAN_MAKS)
    cpc = _uang(body.maks_cpc, "CPC maksimum", CPC_MAKS)
    sebelum = await baca_batas(session, akun_id)
    row = await session.get(AgenIklanBatas, akun_id)
    if row is None:
        row = AgenIklanBatas(akun_id=akun_id)
        session.add(row)
    row.anggaran_harian_maks = anggaran
    row.maks_persen_perubahan = body.maks_persen_perubahan
    row.maks_cpc = cpc
    row.min_hari_antar_perubahan = body.min_hari_antar_perubahan
    row.otonomi = body.otonomi
    row.updated_by = getattr(pengguna, "id", None)
    await session.flush()
    sesudah = _batas_keluar(row)
    await _catat(
        session, akun_id=akun_id, aksi="batas", pengguna=pengguna, sebelum=sebelum, sesudah=sesudah,
        alasan=body.alasan, request_id=body.request_id,
    )
    return sesudah


async def baca_stop(session: AsyncSession, akun_id: str | None) -> dict:
    kunci = GLOBAL if akun_id is None else akun_id
    return _stop_keluar("global" if akun_id is None else "toko", await session.get(AgenIklanStop, kunci))


async def simpan_stop(session: AsyncSession, akun_id: str | None, body: StopIn, pengguna) -> dict:
    ada = await _sudah(session, akun_id, body.request_id)
    if ada is not None:
        return _muat(ada.sesudah_json)
    kunci = GLOBAL if akun_id is None else akun_id
    sebelum = await baca_stop(session, akun_id)
    row = await session.get(AgenIklanStop, kunci)
    if row is None:
        row = AgenIklanStop(id=kunci)
        session.add(row)
    row.aktif = body.aktif
    row.alasan = body.alasan
    row.oleh = getattr(pengguna, "id", None)
    await session.flush()
    sesudah = _stop_keluar(sebelum["lingkup"], row)
    await _catat(
        session, akun_id=akun_id, aksi="stop", pengguna=pengguna, sebelum=sebelum, sesudah=sesudah,
        alasan=body.alasan, request_id=body.request_id, hasil="stop" if body.aktif else "lanjut",
    )
    return sesudah


async def tulisan_diblokir(session: AsyncSession, akun_id: str) -> str | None:
    """Reason writes must stop, or None. Global STOP wins over the shop switch."""
    for kunci, lingkup in ((GLOBAL, "global"), (akun_id, "toko")):
        row = await session.get(AgenIklanStop, kunci)
        if row is not None and row.aktif:
            return row.alasan or f"STOP {lingkup} aktif"
    return None


async def daftar_audit(session: AsyncSession, *, akun_id: str | None, diizinkan: list[str] | None, limit: int = 50) -> dict:
    if akun_id and diizinkan is not None and akun_id not in diizinkan:
        raise HTTPException(status_code=403, detail="Akses ditolak")
    stmt = select(AgenIklanAudit)
    if akun_id:
        stmt = stmt.where(AgenIklanAudit.akun_id == akun_id)
    elif diizinkan is not None:
        stmt = stmt.where(or_(AgenIklanAudit.akun_id.is_(None), AgenIklanAudit.akun_id.in_(diizinkan or ["-"])))
    rows = (
        await session.execute(stmt.order_by(AgenIklanAudit.dibuat_at.desc(), AgenIklanAudit.id.desc()).limit(limit))
    ).scalars().all()
    return {"items": [_audit_keluar(r) for r in rows]}
