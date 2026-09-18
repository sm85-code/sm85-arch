"""Authentication routes: session"""
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

@router.post("/auth/login")
async def login(payload: UserLogin, response: Response):
    doc = await db.users.select_one(
        {"$or": [{"username": payload.username}, {"email": payload.username}]}, {"_id": 0}
    )
    if not doc:
        raise HTTPException(status_code=401, detail="Username atau password salah")
    user = User(**doc)
    if not user.active:
        raise HTTPException(status_code=403, detail="Akun dinonaktifkan")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Username atau password salah")
    token = create_access_token(user.id, user.role, user.session_version, {"name": user.name, "unit": user.unit_usaha_id})
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, secure=True, samesite="none",
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600, path="/",
    )
    return {"user": UserOut(**user.model_dump()).model_dump()}

@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}

@router.get("/auth/me", response_model=UserOut)
async def me(payload: dict = Depends(get_current_user_payload)):
    user = await user_from_payload(payload)
    return UserOut(**user.model_dump())
