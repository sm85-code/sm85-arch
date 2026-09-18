"""Modular routes: transaction types"""
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

@router.post("/transaction-types")
async def create_tx_type(payload: dict, _: dict = Depends(require_roles(*ADMIN_LEVEL))):
    required = {"code", "name", "debit", "credit"}
    if not required.issubset(payload.keys()):
        raise HTTPException(status_code=400, detail="Field wajib: code, name, debit, credit")
    if await db.transaction_types.select_one({"code": payload["code"]}):
        raise HTTPException(status_code=400, detail="Kode jenis transaksi sudah ada")
    doc = {
        "code": payload["code"], "name": payload["name"],
        "debit": payload["debit"], "credit": payload["credit"],
        "unit_codes": payload.get("unit_codes", []),
        "group": payload.get("group", "BUMDES"),
    }
    await db.transaction_types.create(doc)
    doc.pop("_id", None)
    return doc

@router.put("/transaction-types/{code}")
async def update_tx_type(code: str, payload: dict, _: dict = Depends(require_roles(*ADMIN_LEVEL))):
    existing = await db.transaction_types.select_one({"code": code}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Jenis transaksi tidak ditemukan")
    upd = {}
    for k in ("name", "debit", "credit", "unit_codes", "group"):
        if k in payload:
            upd[k] = payload[k]
    if "code" in payload and payload["code"] != code:
        if await db.transaction_types.select_one({"code": payload["code"]}):
            raise HTTPException(status_code=400, detail="Kode tujuan sudah ada")
        upd["code"] = payload["code"]
    await db.transaction_types.modify_one({"code": code}, {"set": upd})
    new_code = upd.get("code", code)
    return await db.transaction_types.select_one({"code": new_code}, {"_id": 0})

@router.delete("/transaction-types/{code}")
async def delete_tx_type(code: str, _: dict = Depends(require_roles(*ADMIN_LEVEL))):
    r = await db.transaction_types.remove_one({"code": code})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Jenis transaksi tidak ditemukan")
    return {"deleted": r.deleted_count}

@router.get("/transaction-types")
async def list_tx_types(_: dict = Depends(get_current_user_payload)):
    docs = await db.transaction_types.select({}, {"_id": 0}).all(200)
    return docs
