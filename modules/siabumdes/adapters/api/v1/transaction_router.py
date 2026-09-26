"""Transactions + proof uploads. Isolates pengelola to their own unit."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from modules.siabumdes.adapters.api.deps import get_current_user, require_roles
from modules.siabumdes.adapters.api.scope import (
    TX_DELETE_ROLES,
    WRITE_ROLES,
    assert_can_mutate_period,
    assert_not_readonly,
    assert_unit_active,
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
    is_oauth_configured,
    oauth_flow,
    upload_file_to_gdrive,
)
from modules.siabumdes.identity.infrastructure.models import User
from modules.siabumdes.application.services import FinanceService
from modules.siabumdes.infrastructure.models import Account, JournalEntry, Transaction
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
    # Query explicitly instead of touching tx.journal_entry: for a just-flushed
    # (now-persistent) Transaction that relationship isn't guaranteed to be
    # loaded, and a plain attribute access would trigger a lazy load outside
    # an awaited context, raising MissingGreenlet under AsyncSession.
    existing_entry = await session.scalar(
        select(JournalEntry).where(JournalEntry.transaction_id == tx.id)
    )
    if existing_entry:
        await session.delete(existing_entry)
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
    reference: Optional[str] = None,
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
    if reference:
        # Substring match, not exact: a single inventory movement can post more
        # than one transaction (e.g. stock-out posts both "stock-out:<id>" for
        # COGS and "stock-out-rev:<id>" for revenue). Filtering by the shared
        # <id> fragment surfaces every transaction tied to that movement/
        # adjustment in one request, from Inventory's "Lihat transaksi" link.
        stmt = stmt.where(Transaction.reference.contains(reference))
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
    await assert_unit_active(session, unit_id)
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
    if is_pengelola(user):
        # Pengelola yang unitnya sudah dinonaktifkan bersifat read-only --
        # tidak boleh mengedit apa pun lagi, termasuk transaksi lama di
        # unitnya sendiri.
        await assert_unit_active(session, unit_id)
    elif unit_id != tx.unit_usaha_id:
        # Untuk admin/direktur/dst: hanya cek unit aktif kalau transaksi
        # dipindah ke unit lain -- mengedit transaksi lama yang sudah ada
        # di unit tetap boleh, biar masih bisa membetulkan salah ketik di
        # transaksi historis.
        await assert_unit_active(session, unit_id)
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
    # Delete the journal entry (+ items via ORM cascade) explicitly first.
    # ORM delete of Transaction alone would try to SET NULL
    # journal_entries.transaction_id, which violates NOT NULL and 500s —
    # same class of bug already worked around in cancel_inventory_journal.
    existing_entry = await session.scalar(
        select(JournalEntry).where(JournalEntry.transaction_id == tx.id)
    )
    if existing_entry:
        await session.delete(existing_entry)
        await session.flush()
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
    if is_pengelola(user):
        await assert_unit_active(session, user.unit_usaha_id)
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
    if is_pengelola(user):
        await assert_unit_active(session, user.unit_usaha_id)
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
    oauth = is_oauth_configured()
    return {
        "connected": is_configured(),
        "mode": "oauth" if oauth else "service_account",
        "configured": is_configured(),
    }


@router.get("/admin/gdrive/connect")
async def gdrive_connect(request: Request, _: User = Depends(require_roles("admin"))):
    """Mulai alur OAuth: kembalikan link consent Google. Admin buka link itu,
    login/izinkan akses Drive, lalu Google redirect balik ke
    /admin/gdrive/oauth-callback dengan refresh token yang perlu ditempel
    manual sebagai env var GOOGLE_OAUTH_REFRESH_TOKEN (App Platform tidak
    punya API untuk backend menulis env var dirinya sendiri saat runtime)."""
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/admin/gdrive/oauth-callback"
    flow = oauth_flow(redirect_uri)
    auth_url, _state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return {"auth_url": auth_url}


@router.get("/admin/gdrive/oauth-callback")
async def gdrive_oauth_callback(request: Request, code: str):
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/admin/gdrive/oauth-callback"
    flow = oauth_flow(redirect_uri)
    flow.fetch_token(code=code)
    refresh_token = flow.credentials.refresh_token
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "Google tidak mengembalikan refresh token (biasanya karena akun ini sudah "
                "pernah kasih izin sebelumnya). Buka https://myaccount.google.com/permissions, "
                "cabut akses aplikasi ini, lalu ulangi proses connect dari awal."
            ),
        )
    return {
        "detail": (
            "Berhasil. Copy nilai refresh_token di bawah, tempel sebagai env var "
            "GOOGLE_OAUTH_REFRESH_TOKEN di App Platform (bareng GOOGLE_OAUTH_CLIENT_ID "
            "dan GOOGLE_OAUTH_CLIENT_SECRET), lalu redeploy."
        ),
        "refresh_token": refresh_token,
    }
