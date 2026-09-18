"""Modular routes: crud"""
from typing import List, Optional

from router_dependencies import (
    ACCESS_TOKEN_EXPIRE_HOURS,
    ADMIN_LEVEL,
    APIRouter,
    API_PREFIX,
    APP_TITLE,
    Account,
    AccountCreate,
    DatabaseClient,
    CHART_OF_ACCOUNTS,
    COOKIE_NAME,
    CORSMiddleware,
    ChangePasswordRequest,
    Depends,
    FastAPI,
    File,
    HTMLResponse,
    HTTPException,
    List,
    Mitra,
    MitraCreate,
    Optional,
    P,
    Paragraph,
    PasswordResetRequest,
    Path,
    ProfileUpdateRequest,
    Query,
    READONLY_ROLES,
    READ_LEVEL,
    REPORT_READ_LEVEL,
    ROOT_DIR,
    Request,
    Response,
    RevenueShare,
    RevenueShareCreate,
    Spacer,
    StreamingResponse,
    Table,
    TableStyle,
    Transaction,
    TransactionCreate,
    UNIT_USAHA_SEED,
    UnitUsaha,
    UnitUsahaCreate,
    UploadFile,
    User,
    UserCreate,
    UserLogin,
    UserOut,
    UserRole,
    WRITE_LEVEL,
    _CELL_BOLD,
    _CELL_BOLD_RIGHT,
    _CELL_RIGHT,
    _CELL_STYLE,
    _STYLES,
    _arus_kas,
    _calc_balances,
    _calc_balances_before,
    _get_accounts_map,
    _group_from_unit,
    _laba_rugi,
    _ledger_data,
    _neraca,
    _pdf_response,
    _per_unit_report,
    _perubahan_ekuitas,
    _check_period_not_blocked,
    _check_period_not_closed,
    _section_row,
    _sig_flow,
    _table_style,
    app,
    client,
    close_database,
    cm,
    colors,
    create_access_token,
    datetime,
    db,
    fmt_rp,
    get_current_user_payload,
    hash_password,
    io,
    load_dotenv,
    logging,
    now_utc,
    os,
    pdf_header,
    require_not_readonly,
    require_password_ready,
    require_roles,
    router,
    scope_unit_for_pengelola,
    seed_database,
    timezone,
    user_from_payload,
    uuid,
    verify_password
)

router = APIRouter(prefix=API_PREFIX)

@router.get("/transactions")
async def list_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    unit_usaha_id: Optional[str] = None,
    limit: int = 500,
    payload: dict = Depends(get_current_user_payload),
):
    user = await user_from_payload(payload)
    q: dict = {}
    if start_date and end_date:
        q["date"] = {"$gte": start_date, "$lte": end_date}
    elif start_date:
        q["date"] = {"$gte": start_date}
    elif end_date:
        q["date"] = {"$lte": end_date}
    if unit_usaha_id:
        q["unit_usaha_id"] = unit_usaha_id
    # Pengelola only sees own unit
    if user.role == UserRole.PENGELOLA and user.unit_usaha_id:
        q["unit_usaha_id"] = user.unit_usaha_id
    docs = await db.transactions.select(q, {"_id": 0}).order("date", -1).all(limit)
    # Auto-migrate on read: `proof` (single, lama) → `proofs` (array)
    for d in docs:
        if not d.get("proofs") and d.get("proof"):
            d["proofs"] = [d["proof"]]
        d.pop("proof", None)
    return docs

@router.post("/transactions")
async def create_transaction(payload: TransactionCreate, dep: dict = Depends(require_not_readonly())):
    user = await user_from_payload(dep)
    await _check_period_not_blocked(dep, payload.date)
    # Pengelola force unit
    unit_id = payload.unit_usaha_id
    if user.role == UserRole.PENGELOLA:
        unit_id = user.unit_usaha_id
    # Normalise: "" → None (BUMDES scope)
    if not unit_id:
        unit_id = None
    await _check_period_not_closed(unit_id, payload.date)
    tx = Transaction(**{**payload.model_dump(), "unit_usaha_id": unit_id, "created_by": user.id})
    await db.transactions.create(tx.model_dump(mode="json"))
    return tx

@router.put("/transactions/{tx_id}")
async def update_transaction(tx_id: str, payload: TransactionCreate, dep: dict = Depends(require_not_readonly())):
    user = await user_from_payload(dep)
    existing = await db.transactions.select_one({"id": tx_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan")
    # Block on either old or new date's period
    await _check_period_not_blocked(dep, existing.get("date", ""))
    await _check_period_not_blocked(dep, payload.date)
    # Pengelola: can only edit transactions of own unit, and can't change unit
    if user.role == UserRole.PENGELOLA:
        if existing.get("unit_usaha_id") != user.unit_usaha_id:
            raise HTTPException(status_code=403, detail="Hanya bisa mengedit transaksi unit Anda")
        payload_data = payload.model_dump(mode="json")
        payload_data["unit_usaha_id"] = user.unit_usaha_id
    else:
        payload_data = payload.model_dump(mode="json")
    # Normalise: "" → None (BUMDES scope)
    if not payload_data.get("unit_usaha_id"):
        payload_data["unit_usaha_id"] = None
    payload_data["created_by"] = existing.get("created_by")
    payload_data["id"] = tx_id
    payload_data["created_at"] = existing.get("created_at")
    await db.transactions.modify_one({"id": tx_id}, {"set": payload_data})
    return await db.transactions.select_one({"id": tx_id}, {"_id": 0})


async def _cancel_stok_from_references(references: List[str]) -> None:
    """Dual-way cleanup: batalkan StokMasuk terkait reference sinkronisasi-stok:* di PostgreSQL.

    - Set status_keuangan = 'dibatalkan' agar sinkronisasi-mingguan tidak meregenerasi jurnal.
    - Kembalikan stok fisik (stok_saat_ini -= jumlah).
    Gagal → raise HTTPException agar penghapusan keuangan tidak meninggalkan inkonsistensi.
    """
    stok_ids = []
    for ref in references:
        if not ref or not str(ref).startswith("sinkronisasi-stok:"):
            continue
        stok_id_str = str(ref).split(":")[-1].strip()
        if stok_id_str.isdigit():
            stok_ids.append(int(stok_id_str))
    if not stok_ids:
        return

    from database import SessionLocal
    # Import langsung dari sumber (bukan facade models.py) — hindari circular dependency
    from models.inventory import StokMasuk, Produk

    try:
        async with SessionLocal() as session:
            async with session.begin():
                for s_id in stok_ids:
                    stok_item = await session.get(StokMasuk, s_id)
                    if not stok_item or stok_item.status_keuangan == "dibatalkan":
                        continue
                    stok_item.status_keuangan = "dibatalkan"
                    product = await session.get(Produk, stok_item.produk_id)
                    if product:
                        product.stok_saat_ini = max(
                            0, int(product.stok_saat_ini or 0) - int(stok_item.jumlah or 0)
                        )
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("Gagal dual-way cancel stok dari reference sinkronisasi: %s", e)
        raise HTTPException(
            status_code=500,
            detail=(
                "Gagal membatalkan status stok terkait (dual-way cleanup). "
                f"Transaksi keuangan tidak dihapus. Detail: {e}"
            ),
        )


@router.delete("/transactions/bulk")
async def bulk_delete_transactions(ids: List[str], dep: dict = Depends(require_roles(*WRITE_LEVEL))):
    if not ids:
        return {"deleted": 0}

    # Ambil dokumen penuh agar field reference selalu ada (hindari projection hilang)
    txs = await db.transactions.select({"id": {"$in": ids}}, None).all(20000)

    for t in txs:
        await _check_period_not_blocked(dep, t.get("date", ""))

    refs = [str(t.get("reference") or "") for t in txs]
    await _cancel_stok_from_references(refs)

    r = await db.transactions.remove_many({"id": {"$in": ids}})
    return {"deleted": r.deleted_count}


@router.delete("/transactions/{tx_id}")
async def delete_transaction(tx_id: str, dep: dict = Depends(require_roles("admin", "direktur", "bendahara"))):
    # Ambil dokumen penuh (tanpa projection sempit) agar reference selalu terbaca
    existing = await db.transactions.select_one({"id": tx_id}, None)
    if not existing:
        return {"deleted": 0}

    await _check_period_not_blocked(dep, existing.get("date", ""))

    ref = existing.get("reference") or ""
    await _cancel_stok_from_references([str(ref)])

    r = await db.transactions.remove_one({"id": tx_id})
    return {"deleted": r.deleted_count}
