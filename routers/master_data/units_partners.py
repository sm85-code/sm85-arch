"""Modular routes: units partners"""
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

@router.get("/unit-usaha", response_model=List[UnitUsaha])
async def list_units(_: dict = Depends(get_current_user_payload)):
    docs = await db.unit_usaha.select({}, {"_id": 0}).order("code", 1).all(50)
    return [UnitUsaha(**d) for d in docs]

@router.post("/unit-usaha", response_model=UnitUsaha)
async def create_unit(payload: UnitUsahaCreate, _: dict = Depends(require_roles("admin", "direktur"))):
    if await db.unit_usaha.select_one({"code": payload.code}):
        raise HTTPException(status_code=400, detail="Kode unit sudah ada")
    u = UnitUsaha(**payload.model_dump())
    await db.unit_usaha.create(u.model_dump())
    return u

@router.get("/mitra", response_model=List[Mitra])
async def list_mitra(unit_usaha_id: Optional[str] = None, payload: dict = Depends(get_current_user_payload)):
    user = await user_from_payload(payload)
    q: dict = {}
    if unit_usaha_id:
        q["unit_usaha_id"] = unit_usaha_id
    if user.role == UserRole.PENGELOLA and user.unit_usaha_id:
        q["unit_usaha_id"] = user.unit_usaha_id
    docs = await db.mitra.select(q, {"_id": 0}).all(500)
    return [Mitra(**d) for d in docs]

@router.post("/mitra", response_model=Mitra)
async def create_mitra(payload: MitraCreate, dep: dict = Depends(require_not_readonly())):
    user = await user_from_payload(dep)
    unit_id = payload.unit_usaha_id
    if user.role == UserRole.PENGELOLA:
        unit_id = user.unit_usaha_id
    m = Mitra(**{**payload.model_dump(), "unit_usaha_id": unit_id})
    await db.mitra.create(m.model_dump())
    return m

@router.delete("/mitra/{mitra_id}")
async def delete_mitra(mitra_id: str, _: dict = Depends(require_roles("admin", "direktur", "bendahara"))):
    r = await db.mitra.remove_one({"id": mitra_id})
    return {"deleted": r.deleted_count}
