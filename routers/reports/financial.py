"""Modular routes: financial"""
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

@router.get("/reports/laba-rugi")
async def rpt_laba_rugi(start_date: str, end_date: str, unit_usaha_id: Optional[str] = None,
                        payload: dict = Depends(require_roles(*REPORT_READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    return await _laba_rugi(start_date, end_date, unit_usaha_id)

@router.get("/reports/neraca")
async def rpt_neraca(as_of_date: str, unit_usaha_id: Optional[str] = None,
                     payload: dict = Depends(require_roles(*REPORT_READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    return await _neraca(as_of_date, unit_usaha_id)

@router.get("/reports/arus-kas")
async def rpt_arus_kas(start_date: str, end_date: str, unit_usaha_id: Optional[str] = None,
                       payload: dict = Depends(require_roles(*REPORT_READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    return await _arus_kas(start_date, end_date, unit_usaha_id)

@router.get("/reports/perubahan-ekuitas")
async def rpt_pe(start_date: str, end_date: str, unit_usaha_id: Optional[str] = None,
                 payload: dict = Depends(require_roles(*REPORT_READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    return await _perubahan_ekuitas(start_date, end_date, unit_usaha_id)

@router.get("/reports/per-unit")
async def rpt_per_unit(start_date: str, end_date: str, _: dict = Depends(get_current_user_payload)):
    return await _per_unit_report(start_date, end_date)

@router.get("/reports/calk")
async def rpt_calk(start_date: str, end_date: str, unit_usaha_id: Optional[str] = None,
                   payload: dict = Depends(require_roles(*READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    if unit_usaha_id:
        raise HTTPException(status_code=404, detail="CALK hanya tersedia untuk grup BUMDES")
    lr = await _laba_rugi(start_date, end_date)
    nr = await _neraca(end_date)
    ak = await _arus_kas(start_date, end_date)
    return {
        "informasi_umum": {
            "nama": "BUMDES Karya Raharja",
            "alamat": "Desa Wonoharjo, Kec. Pangandaran",
            "direktur": "Budianto",
            "dasar_hukum": "Kepmendesa PDTT No. 136 Tahun 2022",
        },
        "periode": {"start": start_date, "end": end_date},
        "ringkasan_kinerja": {
            "total_pendapatan": lr["total_pendapatan"],
            "total_beban": lr["total_beban"],
            "laba_bersih": lr["laba_bersih"],
            "total_aset": nr["total_aset"],
            "total_kewajiban": nr["total_kewajiban"],
            "total_ekuitas": nr["total_ekuitas"],
            "arus_kas_bersih": ak["arus_kas_bersih"],
        },
        "kebijakan_akuntansi": [
            "Laporan disusun sesuai Kepmendesa PDTT No. 136 Tahun 2022.",
            "Pengakuan pendapatan menggunakan basis akrual.",
            "Bagi hasil pengelola sebesar 30% dari laba bersih unit usaha.",
            "Bagi hasil BUMDES sebesar 70% dari laba bersih unit usaha.",
            "Bagi hasil mitra peternak (unit domba) sebesar 30% dari penjualan anakan.",
            "Setoran mitra ikan mujaer bioflok sebesar Rp3.000 per kg.",
            "Imbal hasil mitra dagang sebesar 3% per bulan dari modal yang dititipkan.",
        ],
    }
