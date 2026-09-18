"""Authentication routes: profile"""
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

router = APIRouter()

@router.put("/auth/profile", response_model=UserOut)
async def update_profile(payload: ProfileUpdateRequest, current: dict = Depends(get_current_user_payload)):
    user = await user_from_payload(current)
    duplicate = await db.users.select_one({
        "$or": [{"email": payload.email}, {"username": payload.username}],
        "id": {"$ne": user.id},
    })
    if duplicate:
        raise HTTPException(status_code=400, detail="Email atau username sudah digunakan")
    await db.users.modify_one(
        {"id": user.id},
        {"set": {"name": payload.name.strip(), "email": str(payload.email), "username": payload.username.strip()}},
    )
    updated = await db.users.select_one({"id": user.id}, {"_id": 0, "password_hash": 0})
    return UserOut(**updated)

@router.post("/auth/change-password")
async def change_password(payload: ChangePasswordRequest, current: dict = Depends(get_current_user_payload)):
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password baru minimal 8 karakter")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="Password baru harus berbeda")
    user = await user_from_payload(current)
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Password sementara salah")
    await db.users.modify_one(
        {"id": user.id},
        {"set": {"password_hash": hash_password(payload.new_password), "must_change_password": False}, "increment": {"session_version": 1}},
    )
    return {"ok": True}
