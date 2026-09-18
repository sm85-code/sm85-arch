"""Modular routes: revenue share"""
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

@router.post("/revenue-share")
async def create_revenue_share(payload: RevenueShareCreate, dep: dict = Depends(require_roles(*WRITE_LEVEL))):
    net = payload.gross_revenue - payload.operational_cost
    manager = round(net * 0.30, 2)
    bumdes = round(net * 0.70, 2)
    rs = RevenueShare(
        period=payload.period, unit_usaha_id=payload.unit_usaha_id,
        gross_revenue=payload.gross_revenue, operational_cost=payload.operational_cost,
        net_revenue=net, manager_share=manager, bumdes_share=bumdes,
    )
    await db.revenue_shares.create(rs.model_dump())
    return rs

@router.delete("/revenue-share/{rs_id}")
async def delete_revenue_share(rs_id: str, _: dict = Depends(require_roles(*WRITE_LEVEL))):
    r = await db.revenue_shares.remove_one({"id": rs_id})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Data bagi hasil tidak ditemukan")
    return {"deleted": r.deleted_count}

@router.get("/revenue-share")
async def list_revenue_share(
    unit_usaha_id: Optional[str] = None,
    payload: dict = Depends(get_current_user_payload),
):
    user = await user_from_payload(payload)
    q: dict = {}
    if unit_usaha_id:
        q["unit_usaha_id"] = unit_usaha_id
    if user.role == UserRole.PENGELOLA and user.unit_usaha_id:
        q["unit_usaha_id"] = user.unit_usaha_id
    docs = await db.revenue_shares.select(q, {"_id": 0}).order("period", -1).all(200)
    return docs
