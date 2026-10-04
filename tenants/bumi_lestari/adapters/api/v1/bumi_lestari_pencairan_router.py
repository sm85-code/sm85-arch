"""HTTP: format file penghasilan (Data master, admin) & pencairan marketplace/iPaymu (spesifikasi 8.3, 10.6)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import pencairan_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import BatalIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pencairan import (
    BacaHeaderOut,
    FormatPenghasilanIn,
    FormatPenghasilanOut,
    HubungkanIn,
    PencairanBarisOut,
    PencairanDetailOut,
    PencairanManualIn,
    PencairanUnggahanOut,
    PratinjauOut,
    UjiFormatOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

pencairan_router = APIRouter()
OWNER_UP = ("admin", "owner")
BATAS_FILE = 10 * 1024 * 1024  # 10 MB


def _guard(*roles: str):
    return Depends(require_roles_bumi_lestari(*(roles or OWNER_UP)))


def _db():
    return Depends(get_db_bumi_lestari)


async def _isi(file: UploadFile) -> tuple[bytes, str]:
    isi = await file.read(BATAS_FILE + 1)
    if len(isi) > BATAS_FILE:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="File terlalu besar (maks. 10 MB)")
    if not isi:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File kosong")
    return isi, file.filename or "file"


# ---- format file penghasilan (khusus admin; baca: admin & owner) ----


@pencairan_router.get("/format-penghasilan", response_model=list[FormatPenghasilanOut])
async def list_format(saluran_id: str | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_format(session, saluran_id)


@pencairan_router.get("/format-penghasilan/{format_id}", response_model=FormatPenghasilanOut)
async def get_format(format_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.format_out(session, await svc.get_format(session, format_id))


@pencairan_router.post("/format-penghasilan/baca-header", response_model=BacaHeaderOut)
async def baca_header(
    file: UploadFile = File(...), nama_sheet: str | None = Form(None), baris_header: int | None = Form(None),
    _: BlUser = _guard("admin"),
):
    isi, nama = await _isi(file)
    return svc.baca_header(isi, nama, nama_sheet or None, baris_header)


@pencairan_router.post("/format-penghasilan", response_model=FormatPenghasilanOut, status_code=status.HTTP_201_CREATED)
async def create_format(payload: FormatPenghasilanIn, session: AsyncSession = _db(), user: BlUser = _guard("admin")):
    return await svc.format_out(session, await svc.create_format(session, user, payload))


@pencairan_router.put("/format-penghasilan/{format_id}", response_model=FormatPenghasilanOut)
async def update_format(format_id: str, payload: FormatPenghasilanIn, session: AsyncSession = _db(), _: BlUser = _guard("admin")):
    return await svc.format_out(session, await svc.update_format(session, format_id, payload))


@pencairan_router.post("/format-penghasilan/{format_id}/versi-baru", response_model=FormatPenghasilanOut)
async def versi_baru(format_id: str, session: AsyncSession = _db(), user: BlUser = _guard("admin")):
    return await svc.format_out(session, await svc.versi_baru(session, user, format_id))


@pencairan_router.post("/format-penghasilan/{format_id}/uji", response_model=UjiFormatOut)
async def uji_format(format_id: str, file: UploadFile = File(...), session: AsyncSession = _db(), _: BlUser = _guard("admin")):
    isi, nama = await _isi(file)
    return await svc.uji_format(session, format_id, isi, nama)


@pencairan_router.post("/format-penghasilan/{format_id}/aktifkan", response_model=FormatPenghasilanOut)
async def aktifkan(format_id: str, session: AsyncSession = _db(), user: BlUser = _guard("admin")):
    return await svc.format_out(session, await svc.aktifkan_format(session, user, format_id))


# ---- pencairan ----


@pencairan_router.post("/pencairan/pratinjau", response_model=PratinjauOut)
async def pratinjau(
    saluran_id: str = Form(...), format_id: str | None = Form(None), file: UploadFile = File(...),
    session: AsyncSession = _db(), _: BlUser = _guard(),
):
    isi, nama = await _isi(file)
    return await svc.pratinjau(session, saluran_id, isi, nama, format_id or None)


@pencairan_router.post("/pencairan", response_model=PencairanUnggahanOut, status_code=status.HTTP_201_CREATED)
async def simpan(
    saluran_id: str = Form(...), format_id: str | None = Form(None), file: UploadFile = File(...),
    ganti_manual: bool = Form(False), session: AsyncSession = _db(), user: BlUser = _guard(),
):
    isi, nama = await _isi(file)
    return await svc.simpan(session, user, saluran_id, isi, nama, format_id or None, ganti_manual=ganti_manual)


@pencairan_router.post("/pencairan/manual", response_model=PencairanUnggahanOut, status_code=status.HTTP_201_CREATED)
async def manual(payload: PencairanManualIn, session: AsyncSession = _db(), user: BlUser = _guard()):
    return await svc.entri_manual(session, user, payload)


@pencairan_router.get("/pencairan", response_model=list[PencairanUnggahanOut])
async def list_pencairan(saluran_id: str | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_unggahan(session, saluran_id)


@pencairan_router.get("/pencairan/{unggahan_id}", response_model=PencairanDetailOut)
async def detail(unggahan_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    out = PencairanDetailOut.model_validate(await svc.get_unggahan(session, unggahan_id))
    out.baris = [PencairanBarisOut.model_validate(r) for r in await svc.baris_unggahan(session, unggahan_id)]
    return out


@pencairan_router.post("/pencairan/{unggahan_id}/batal", response_model=PencairanUnggahanOut)
async def batal(unggahan_id: str, payload: BatalIn, session: AsyncSession = _db(), user: BlUser = _guard()):
    return await svc.batal_unggahan(session, user, unggahan_id, payload.alasan)


@pencairan_router.post("/pencairan/baris/{baris_id}/hubungkan", response_model=PencairanBarisOut)
async def hubungkan(baris_id: str, payload: HubungkanIn, session: AsyncSession = _db(), user: BlUser = _guard()):
    return await svc.hubungkan(session, user, baris_id, payload.order_id)


@pencairan_router.get("/pencairan/erp/toko")
async def toko_erp(_: BlUser = _guard()):
    """Toko Shopee di ERP, plus saran mana yang masuk Bumi Lestari."""
    from tenants.bumi_lestari.modules.bumi_lestari.application import erp_pencairan
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import SessionLocal

    async with SessionLocal() as erp:
        return await erp_pencairan.daftar_toko(erp)


@pencairan_router.post("/pencairan/erp/pasang")
async def pasang_erp(payload: dict = Body(), session: AsyncSession = _db(), _: BlUser = _guard()):
    from tenants.bumi_lestari.modules.bumi_lestari.application import erp_pencairan

    saluran = await erp_pencairan.pasangkan(session, payload["saluran_id"], payload.get("akun_erp_id"))
    return {"saluran_id": saluran.id, "nama": saluran.nama, "akun_erp_id": saluran.akun_erp_id}


@pencairan_router.post("/pencairan/erp/tarik")
async def tarik_erp(hari: int = 15, session: AsyncSession = _db(), user: BlUser = _guard()):
    """Salin pencairan toko yang sudah dipasangkan menjadi draf. Tidak mengirim ke laporan."""
    from tenants.bumi_lestari.modules.bumi_lestari.application import erp_pencairan
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import SessionLocal

    async with SessionLocal() as erp:
        return await erp_pencairan.tarik(session, erp, user, hari=hari)
