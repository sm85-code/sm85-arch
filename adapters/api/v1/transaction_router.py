"""Transactions + proof uploads. Isolates pengelola to their own unit."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from adapters.api.deps import get_current_user, require_roles
from adapters.api.scope import (
    TX_DELETE_ROLES,
    WRITE_ROLES,
    assert_can_mutate_period,
    assert_not_readonly,
    can_access_unit,
    is_pengelola,
    parse_date,
    scoped_unit_id,
    unit_code_for,
)
from adapters.external.gdrive_adapter import (
    delete_file_from_gdrive,
    gdrive_file_exists,
    is_configured,
    upload_file_to_gdrive,
)
from modules.identity.infrastructure.models import User
from modules.siabumdes.application.services import FinanceService
from modules.siabumdes.infrastructure.models import Account, Transaction, UnitUsaha
from shared.config import public_role
from shared.database import get_db

router = APIRouter(prefix="/api", tags=["transactions"])
ALLOWED_PROOF_EXT = {"pdf", "jpg", "jpeg", "png"}
MAX_PROOF_BYTES = 1 * 1024 * 1024
MAX_PROOFS = 3


class TransactionIn(BaseModel):
    date: date
    unit_usaha_id: Optional[str] = None
    transaction_type: str
    description: str = ""
    amount: Decimal = Field(gt=0)
    debit_account_code: str
    credit_account_code: str
    reference: str = ""
    mitra_id: Optional[str] = None


def _tx_out(row: Transaction) -> dict:
    proofs = list(row.proofs or [])
    return {
        "id": row.id,
        "date": row.date.isoformat(),
        "unit_usaha_id": row.unit_usaha_id,
        "transaction_type": row.transaction_type,
        "description": row.description,
        "amount": float(row.amount),
        "debit_account_code": row.debit_account_code,
        "credit_account_code": row.credit_account_code,
        "reference": row.reference,
        "mitra_id": row.mitra_id,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "is_closing": row.is_closing,
        "proofs": proofs,
    }


async def _account(session: AsyncSession, code: str, group: str) -> Account:
    row = (
        await session.execute(select(Account).where(Account.code == code, Account.group_code == group))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=400, detail=f"Akun {code} tidak ada di kelompok {group}")
    return row


async def _sync_journal(session: AsyncSession, tx: Transaction, group: str) -> None:
    debit = await _account(session, tx.debit_account_code, group)
    credit = await _account(session, tx.credit_account_code, group)
    if tx.journal_entry:
        await session.delete(tx.journal_entry)
        await session.flush()
    await FinanceService(session).create_journal_entry(
        transaction_id=tx.id,
        entry_date=tx.date,
        memo=tx.description,
        debit_account_id=debit.id,
        credit_account_id=credit.id,
        amount=tx.amount,
    )


@router.get("/transactions")
async def list_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    unit_usaha_id: Optional[str] = None,
    limit: int = 500,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(Transaction).order_by(Transaction.date.desc(), Transaction.created_at.desc()).limit(min(limit, 2000))
    start = parse_date(start_date)
    end = parse_date(end_date)
    if start:
        stmt = stmt.where(Transaction.date >= start)
    if end:
        stmt = stmt.where(Transaction.date <= end)
    if is_pengelola(user):
        stmt = stmt.where(Transaction.unit_usaha_id == user.unit_usaha_id)
    elif unit_usaha_id:
        stmt = stmt.where(Transaction.unit_usaha_id == unit_usaha_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [_tx_out(row) for row in rows]


@router.post("/transactions")
async def create_transaction(
    payload: TransactionIn,
    user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_db),
):
    assert_not_readonly(user)
    unit_id = await scoped_unit_id(session, user, payload.unit_usaha_id)
    await assert_can_mutate_period(session, user, payload.date, unit_id)
    group = await unit_code_for(session, unit_id)
    tx = Transaction(
        date=payload.date,
        unit_usaha_id=unit_id,
        transaction_type=payload.transaction_type,
        description=payload.description,
        amount=payload.amount,
        debit_account_code=payload.debit_account_code,
        credit_account_code=payload.credit_account_code,
        reference=payload.reference or "",
        mitra_id=payload.mitra_id,
        created_by=user.id,
        proofs=[],
    )
    session.add(tx)
    await session.flush()
    await _sync_journal(session, tx, group)
    return _tx_out(tx)


@router.put("/transactions/{tx_id}")
async def update_transaction(
    tx_id: str,
    payload: TransactionIn,
    user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_db),
):
    assert_not_readonly(user)
    tx = (
        await session.execute(
            select(Transaction).options(selectinload(Transaction.journal_entry)).where(Transaction.id == tx_id)
        )
    ).scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan")
    if not can_access_unit(user, tx.unit_usaha_id):
        raise HTTPException(status_code=403, detail="Hanya bisa mengedit transaksi unit Anda")
    unit_id = await scoped_unit_id(session, user, payload.unit_usaha_id if not is_pengelola(user) else tx.unit_usaha_id)
    await assert_can_mutate_period(session, user, tx.date, tx.unit_usaha_id)
    await assert_can_mutate_period(session, user, payload.date, unit_id)
    tx.date = payload.date
    tx.unit_usaha_id = unit_id
    tx.transaction_type = payload.transaction_type
    tx.description = payload.description
    tx.amount = payload.amount
    tx.debit_account_code = payload.debit_account_code
    tx.credit_account_code = payload.credit_account_code
    tx.reference = payload.reference or ""
    tx.mitra_id = payload.mitra_id
    await session.flush()
    group = await unit_code_for(session, unit_id)
    await _sync_journal(session, tx, group)
    return _tx_out(tx)


@router.delete("/transactions/{tx_id}")
async def delete_transaction(
    tx_id: str,
    user: User = Depends(require_roles(*TX_DELETE_ROLES)),
    session: AsyncSession = Depends(get_db),
):
    tx = await session.get(Transaction, tx_id)
    if not tx:
        return {"deleted": 0}
    await assert_can_mutate_period(session, user, tx.date, tx.unit_usaha_id)
    for proof in list(tx.proofs or []):
        fid = proof.get("file_id")
        if fid and is_configured():
            try:
                await delete_file_from_gdrive(fid)
            except Exception:
                pass
    await session.delete(tx)
    return {"deleted": 1}


@router.post("/transactions/{tx_id}/proof")
async def upload_proof(
    tx_id: str,
    file: UploadFile = File(...),
    user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_db),
):
    tx = await session.get(Transaction, tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan")
    if not can_access_unit(user, tx.unit_usaha_id):
        raise HTTPException(status_code=403, detail="Bukan transaksi unit Anda")
    await assert_can_mutate_period(session, user, tx.date, tx.unit_usaha_id)
    proofs = list(tx.proofs or [])
    if len(proofs) >= MAX_PROOFS:
        raise HTTPException(status_code=400, detail="Maksimal 3 file bukti per transaksi.")
    fname = (file.filename or "bukti").lower()
    ext = fname.rsplit(".", 1)[-1] if "." in fname else ""
    if ext not in ALLOWED_PROOF_EXT:
        raise HTTPException(status_code=400, detail="Format harus PDF/JPG/JPEG/PNG")
    data = await file.read()
    if len(data) > MAX_PROOF_BYTES:
        raise HTTPException(status_code=400, detail="Ukuran file maksimal 1 MB")
    if not is_configured():
        raise HTTPException(status_code=503, detail="Google Drive service account belum dikonfigurasi")

    group_code = await unit_code_for(session, tx.unit_usaha_id)
    ddmmyyyy = tx.date.strftime("%d%m%Y")
    same_q = select(Transaction).where(Transaction.date == tx.date)
    if tx.unit_usaha_id:
        same_q = same_q.where(Transaction.unit_usaha_id == tx.unit_usaha_id)
    else:
        same_q = same_q.where(Transaction.unit_usaha_id.is_(None))
    max_urut = 0
    for other in (await session.execute(same_q)).scalars():
        for item in other.proofs or []:
            max_urut = max(max_urut, int(item.get("urut") or 0))
    urut = max_urut + 1
    new_name = f"{group_code}_{ddmmyyyy}_{urut}.{ext}"
    meta = await upload_file_to_gdrive(data, new_name, file.content_type or "application/octet-stream")
    proof = {
        "file_id": meta["id"],
        "file_name": meta["name"],
        "url": meta.get("webViewLink"),
        "urut": urut,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "uploaded_by": user.id,
        "size": len(data),
    }
    proofs.append(proof)
    tx.proofs = proofs
    return {"ok": True, "proofs": proofs}


@router.delete("/transactions/{tx_id}/proofs/{file_id}")
async def delete_proof(
    tx_id: str,
    file_id: str,
    user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_db),
):
    tx = await session.get(Transaction, tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan")
    if not can_access_unit(user, tx.unit_usaha_id):
        raise HTTPException(status_code=403, detail="Bukan transaksi unit Anda")
    await assert_can_mutate_period(session, user, tx.date, tx.unit_usaha_id)
    proofs = list(tx.proofs or [])
    if not any(p.get("file_id") == file_id for p in proofs):
        raise HTTPException(status_code=404, detail="File bukti tidak ditemukan")
    if is_configured():
        try:
            await delete_file_from_gdrive(file_id)
        except Exception:
            pass
    tx.proofs = [p for p in proofs if p.get("file_id") != file_id]
    return {"ok": True, "proofs": tx.proofs}


@router.post("/transactions/verify-proofs")
async def verify_proofs(
    user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_db),
):
    if not is_configured():
        return {"ok": True, "checked": 0, "removed": 0, "note": "Drive belum dikonfigurasi"}
    stmt = select(Transaction)
    if is_pengelola(user):
        stmt = stmt.where(Transaction.unit_usaha_id == user.unit_usaha_id)
    checked = removed = 0
    for tx in (await session.execute(stmt)).scalars():
        arr = list(tx.proofs or [])
        if not arr:
            continue
        kept = []
        changed = False
        for item in arr:
            fid = item.get("file_id")
            if not fid:
                continue
            checked += 1
            if await gdrive_file_exists(fid):
                kept.append(item)
            else:
                removed += 1
                changed = True
        if changed:
            tx.proofs = kept
    return {"ok": True, "checked": checked, "removed": removed}


@router.get("/admin/gdrive/status")
async def gdrive_status(_: User = Depends(get_current_user)):
    """Compatibility stub: SPA treats connected=true as 'no need to OAuth'."""
    return {
        "connected": True,
        "mode": "service_account",
        "configured": is_configured(),
        "email": "service-account",
    }


@router.get("/admin/gdrive/connect")
async def gdrive_connect(request: Request, _: User = Depends(require_roles("admin"))):
    base = str(request.base_url).rstrip("/")
    return {
        "connected": True,
        "auth_url": f"{base}/api/admin/gdrive/already-connected",
        "detail": "Google Drive memakai service account. OAuth admin tidak diperlukan.",
    }


@router.get("/admin/gdrive/already-connected")
async def gdrive_already_connected():
    return {
        "connected": True,
        "detail": "Google Drive terhubung melalui service account.",
    }
