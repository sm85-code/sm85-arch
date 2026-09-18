"""Authentication routes: admin_users"""
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

@router.post("/auth/register")
async def register(payload: UserCreate, admin: dict = Depends(require_roles("admin"))):
    exists = await db.users.select_one({"$or": [{"email": payload.email}, {"username": payload.username}]})
    if exists:
        raise HTTPException(status_code=400, detail="Email atau username sudah terdaftar")
    if payload.role not in (UserRole.ADMIN, UserRole.DIREKTUR, UserRole.BENDAHARA,
                            UserRole.PENGELOLA, UserRole.PENGAWAS, UserRole.PENASIHAT):
        raise HTTPException(status_code=400, detail="Role tidak valid")
    if payload.role == UserRole.PENGELOLA and not payload.unit_usaha_id:
        raise HTTPException(status_code=400, detail="Pengelola harus memiliki unit_usaha_id")
    user = User(
        email=payload.email, username=payload.username, name=payload.name,
        role=payload.role, unit_usaha_id=payload.unit_usaha_id,
        password_hash=hash_password(payload.password),
    )
    await db.users.create(user.model_dump())
    return UserOut(**user.model_dump())

@router.get("/users", response_model=List[UserOut])
async def list_users(admin: dict = Depends(require_roles("admin"))):
    docs = await db.users.select({}, {"_id": 0, "password_hash": 0}).all(200)
    return [UserOut(**d) for d in docs]

@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: str, payload: PasswordResetRequest,
                         admin: dict = Depends(require_roles("admin"))):
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password sementara minimal 8 karakter")
    r = await db.users.modify_one(
        {"id": user_id},
        {"set": {"password_hash": hash_password(payload.new_password), "must_change_password": True},
         "increment": {"session_version": 1},
         "unset": {"plain_password": ""}}
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    return {"ok": True}

@router.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: dict = Depends(require_roles("admin"))):
    r = await db.users.remove_one({"id": user_id})
    return {"deleted": r.deleted_count}

@router.put("/users/{user_id}/blocked-periods")
async def set_blocked_periods(user_id: str, payload: dict, admin: dict = Depends(require_roles("admin"))):
    """Admin sets the list of blocked YYYY-MM periods for a user (transaction lock)."""
    periods = payload.get("blocked_periods", [])
    if not isinstance(periods, list):
        raise HTTPException(400, "blocked_periods must be a list")
    # normalise + validate YYYY-MM
    cleaned = []
    for p in periods:
        if not isinstance(p, str):
            continue
        s = p.strip()
        if len(s) == 7 and s[4] == "-":
            cleaned.append(s)
    r = await db.users.modify_one({"id": user_id}, {"set": {"blocked_periods": sorted(set(cleaned))}})
    if r.matched_count == 0:
        raise HTTPException(404, "User tidak ditemukan")
    doc = await db.users.select_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    return UserOut(**doc)
