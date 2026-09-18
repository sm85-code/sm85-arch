"""Modular routes: public dashboard"""
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

@router.get("/public/summary")
async def public_summary():
    """Ringkasan agregat BUMDES tahun berjalan — endpoint PUBLIK untuk landing page.
    Hanya total agregat, tidak membocorkan rincian per akun/tx/unit."""
    year = datetime.now(timezone.utc).year
    start = f"{year}-01-01"
    end = f"{year}-12-31"
    all_txs = await db.transactions.select(
        {
            "date": {"$gte": start, "$lte": end},
            "is_closing": {"$ne": True},
            "unit_usaha_id": None,  # BUMDES agregat pusat saja
        },
        {"_id": 0, "date": 1, "amount": 1, "debit_account_code": 1,
         "credit_account_code": 1, "unit_usaha_id": 1},
    ).all(20000)
    accounts = {a["code"]: a for a in await db.accounts.select({}, {"_id": 0}).all(500)}
    total_pendapatan = 0.0
    total_beban = 0.0
    monthly: dict = {}
    for t in all_txs:
        d = accounts.get(t.get("debit_account_code"), {})
        c = accounts.get(t.get("credit_account_code"), {})
        amt = t.get("amount", 0)
        m = (t.get("date") or "")[:7]
        monthly.setdefault(m, {"p": 0.0, "b": 0.0})
        if c.get("category") == "pendapatan":
            total_pendapatan += amt
            monthly[m]["p"] += amt
        if d.get("category") == "beban":
            total_beban += amt
            monthly[m]["b"] += amt
    laba = total_pendapatan - total_beban
    pades_estimasi = round(laba * 0.30, 2) if laba > 0 else 0
    unit_count = await db.unit_usaha.count({})
    unit_active = await db.transactions.unique_values(
        "unit_usaha_id",
        {"date": {"$gte": start, "$lte": end}, "unit_usaha_id": {"$ne": None}},
    )
    # Isi 12 bulan (Jan-Des tahun berjalan) supaya chart tetap proporsional walau
    # sebagian bulan belum ada transaksi.
    trend = []
    for m in range(1, 13):
        key = f"{year}-{m:02d}"
        v = monthly.get(key, {"p": 0.0, "b": 0.0})
        trend.append({"month": key, "pendapatan": v["p"], "beban": v["b"]})
    return {
        "year": year,
        "total_pendapatan": total_pendapatan,
        "total_beban": total_beban,
        "laba_bersih": laba,
        "pades_estimasi": pades_estimasi,
        "unit_aktif": len(unit_active),
        "unit_total": unit_count,
        "trend": trend,
    }

@router.get("/reports/dashboard")
async def dashboard(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    granularity: str = "month",
    payload: dict = Depends(get_current_user_payload),
):
    user = await user_from_payload(payload)
    is_pengelola = user.role == UserRole.PENGELOLA

    date_q: dict = {}
    if start_date and end_date:
        date_q["date"] = {"$gte": start_date, "$lte": end_date}
    elif start_date:
        date_q["date"] = {"$gte": start_date}
    elif end_date:
        date_q["date"] = {"$lte": end_date}

    # KPI Rules:
    #   - Pengelola: hanya transaksi unit sendiri; akun dilookup dari grup unit sendiri
    #   - Non-pengelola (Admin/Direktur/Bendahara/Pengawas/Penasihat):
    #       KPI hanya menampilkan data BUMDES (unit_usaha_id = null); akun dilookup dari grup BUMDES
    kpi_q = dict(date_q)
    if is_pengelola and user.unit_usaha_id:
        kpi_q["unit_usaha_id"] = user.unit_usaha_id
        kpi_group = await _group_from_unit(user.unit_usaha_id)
    else:
        kpi_q["unit_usaha_id"] = None
        kpi_group = "BUMDES"
    kpi_txs = await db.transactions.select(kpi_q, {"_id": 0}).all(20000)

    # Unit summaries: semua unit + BUMDES (untuk breakdown non-pengelola)
    all_txs = await db.transactions.select(date_q, {"_id": 0}).all(20000) if not is_pengelola else kpi_txs

    # Akun untuk KPI (lookup di grup yang tepat)
    kpi_accounts = await _get_accounts_map(kpi_group)

    total_pendapatan = 0.0
    total_beban = 0.0
    for tx in kpi_txs:
        amt = tx.get("amount", 0)
        d_acc = kpi_accounts.get(tx["debit_account_code"], {})
        c_acc = kpi_accounts.get(tx["credit_account_code"], {})
        if c_acc.get("category") == "pendapatan":
            total_pendapatan += amt
        if d_acc.get("category") == "beban":
            total_beban += amt

    # Untuk breakdown per unit: lookup akun per grup dari tx.unit_usaha_id
    # Map unit_usaha_id → group code, cached
    unit_group_cache: dict = {}
    async def _grp(uid):
        if uid in unit_group_cache: return unit_group_cache[uid]
        g = await _group_from_unit(uid)
        unit_group_cache[uid] = g
        return g

    # Load all groups' accounts once for per_unit breakdown
    all_accounts_all: dict = {}
    for doc in await db.accounts.select({}, {"_id": 0}).all(500):
        all_accounts_all.setdefault(doc.get("group", "BUMDES"), {})[doc["code"]] = doc

    per_unit: dict = {}
    for tx in all_txs:
        uid = tx.get("unit_usaha_id") or None
        grp = await _grp(uid)
        acc_map = all_accounts_all.get(grp, {})
        d_acc = acc_map.get(tx["debit_account_code"], {})
        c_acc = acc_map.get(tx["credit_account_code"], {})
        key = uid or "bumdes"
        per_unit.setdefault(key, {"pendapatan": 0, "beban": 0})
        amt = tx.get("amount", 0)
        if c_acc.get("category") == "pendapatan":
            per_unit[key]["pendapatan"] += amt
        if d_acc.get("category") == "beban":
            per_unit[key]["beban"] += amt

    units = await db.unit_usaha.select({}, {"_id": 0}).all(50)
    units.sort(key=lambda x: x.get("code", ""))
    unit_summaries = []
    for u in units:
        p = per_unit.get(u["id"], {"pendapatan": 0, "beban": 0})
        unit_summaries.append({
            "id": u["id"], "code": u["code"], "name": u["name"],
            "pendapatan": p["pendapatan"], "beban": p["beban"],
            "laba": p["pendapatan"] - p["beban"],
        })

    # trend buckets: 'day' or 'month' based on granularity
    from collections import defaultdict
    bucket = defaultdict(lambda: {"pendapatan": 0, "beban": 0})
    trend_key = "day" if granularity == "day" else "month"
    for tx in kpi_txs:
        date_str = tx.get("date") or ""
        if not date_str:
            continue
        key = date_str[:10] if trend_key == "day" else date_str[:7]
        d_acc = kpi_accounts.get(tx["debit_account_code"], {})
        c_acc = kpi_accounts.get(tx["credit_account_code"], {})
        if c_acc.get("category") == "pendapatan":
            bucket[key]["pendapatan"] += tx.get("amount", 0)
        if d_acc.get("category") == "beban":
            bucket[key]["beban"] += tx.get("amount", 0)
    monthly_list = [{"month": k, **v} for k, v in sorted(bucket.items())]
    if not (start_date or end_date):
        monthly_list = monthly_list[-6:]

    return {
        "total_pendapatan": total_pendapatan,
        "total_beban": total_beban,
        "laba_bersih": total_pendapatan - total_beban,
        "unit_summaries": unit_summaries,
        "monthly": monthly_list,
        "total_transactions": len(kpi_txs),
    }
