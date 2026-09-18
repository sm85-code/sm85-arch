"""Modular routes: ledger"""
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
    _pdf_header,
    _section_row,
    _signature_block,
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

@router.get("/reports/ledger")
async def ledger_report(
    account_code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    unit_usaha_id: Optional[str] = None,
    payload: dict = Depends(require_roles(*REPORT_READ_LEVEL)),
):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    return await _ledger_data(account_code, start_date, end_date, unit_usaha_id)

@router.get("/reports/ledger/pdf")
async def pdf_ledger(
    account_code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    unit_usaha_id: Optional[str] = None,
    payload: dict = Depends(require_roles(*REPORT_READ_LEVEL)),
):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    data = await _ledger_data(account_code, start_date, end_date, unit_usaha_id)
    sig = _signature_block()

    def build():
        story = []
        acc = data["account"]
        subtitle = f"Akun: {acc['code']} — {acc['name']}"
        if start_date or end_date:
            subtitle += f"<br/>Periode: {start_date or '(awal)'} s.d. {end_date or '(akhir)'}"
        _pdf_header(story, _STYLES, "BUKU BESAR", subtitle)

        rows = [[
            P("Tanggal", _CELL_BOLD),
            P("Keterangan", _CELL_BOLD),
            P("Akun Lawan", _CELL_BOLD),
            P("Ref.", _CELL_BOLD),
            P("Debit", _CELL_BOLD_RIGHT),
            P("Kredit", _CELL_BOLD_RIGHT),
            P("Saldo", _CELL_BOLD_RIGHT),
        ]]
        rows.append([
            P(""), P("Saldo Awal", _CELL_BOLD), P(""), P(""), P(""), P(""),
            P(fmt_rp(data["saldo_awal"]), _CELL_BOLD_RIGHT),
        ])
        saldo_awal_idx = len(rows) - 1
        for e in data["entries"]:
            other = f"{e['other_account_code']}\n{e.get('other_account_name','')}"
            rows.append([
                P(e["date"]),
                P(e["description"] or ""),
                P(other),
                P(e.get("reference") or ""),
                P(fmt_rp(e["debit"]) if e["debit"] else "-", _CELL_RIGHT),
                P(fmt_rp(e["credit"]) if e["credit"] else "-", _CELL_RIGHT),
                P(fmt_rp(e["balance"]), _CELL_RIGHT),
            ])
        rows.append([
            P(""),
            P("TOTAL PERIODE", _CELL_BOLD),
            P(""), P(""),
            P(fmt_rp(data["total_debit"]), _CELL_BOLD_RIGHT),
            P(fmt_rp(data["total_credit"]), _CELL_BOLD_RIGHT),
            P(fmt_rp(data["saldo_akhir"]), _CELL_BOLD_RIGHT),
        ])
        total_row_idx = len(rows) - 1
        t = Table(
            rows,
            colWidths=[2.2 * cm, 4.5 * cm, 3.5 * cm, 1.8 * cm, 2.2 * cm, 2.2 * cm, 2.6 * cm],
            repeatRows=1,
        )
        ts = _table_style()
        ts.add("BACKGROUND", (0, saldo_awal_idx), (-1, saldo_awal_idx), colors.HexColor("#EEF3F9"))
        ts.add("BACKGROUND", (0, total_row_idx), (-1, total_row_idx), colors.HexColor("#DCE8FE"))
        t.setStyle(ts)
        story.append(t)
        story.extend(sig)
        return story
    return _pdf_response(build, f"Buku-Besar_{account_code}.pdf")
