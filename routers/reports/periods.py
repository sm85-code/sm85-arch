"""Modular routes: periods"""
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

@router.post("/reports/close-period")
async def close_period(payload: dict, dep: dict = Depends(require_roles(*ADMIN_LEVEL))):
    """Tutup buku bulanan (jurnal penutup fisik) untuk 1 grup.
    Payload: {period: "YYYY-MM", group: "BUMDES"|"UUxx"}
    Efek:
      1. Generate 1 tx net closing per-akun pendapatan (DR akun pendapatan, CR akun Ikhtisar L/R)
      2. Generate 1 tx net closing per-akun beban+hpp (DR Ikhtisar L/R, CR akun beban)
      3. Transfer Ikhtisar → Saldo Laba (DR/CR sesuai laba/rugi)
      4. Tag semua tx dengan `is_closing=True` untuk transparansi
      5. Simpan period ke closed_periods (agar user lain terkunci)
    """
    period = payload.get("period", "")
    group = payload.get("group", "BUMDES")
    if not period or len(period) != 7 or period[4] != "-":
        raise HTTPException(400, "Format period harus YYYY-MM")
    # Range tanggal
    y, m = int(period[:4]), int(period[5:])
    from calendar import monthrange
    start = f"{y:04d}-{m:02d}-01"
    end = f"{y:04d}-{m:02d}-{monthrange(y, m)[1]:02d}"
    # Cek sudah closed?
    if await db.closed_periods.select_one({"period": period, "group": group}):
        raise HTTPException(400, f"Periode {period} ({group}) sudah ditutup")

    # Derive unit_usaha_id from group
    unit_uid = None
    if group != "BUMDES":
        u = await db.unit_usaha.select_one({"code": group}, {"_id": 0, "id": 1})
        if not u:
            raise HTTPException(400, f"Grup {group} tidak ditemukan")
        unit_uid = u["id"]

    # Find Ikhtisar L/R & Saldo Laba account in this group
    accounts = await _get_accounts_map(group)
    ikhtisar = next((c for c, a in accounts.items()
                     if a.get("category") == "ekuitas" and a.get("subcategory") == "ikhtisar_laba_rugi"), None)
    saldo_laba = next((c for c, a in accounts.items()
                       if a.get("category") == "ekuitas" and a.get("subcategory") == "saldo_laba"), None)
    if not ikhtisar or not saldo_laba:
        raise HTTPException(400, f"Grup {group} belum punya akun Ikhtisar L/R (subcategory=ikhtisar_laba_rugi) atau Saldo Laba (subcategory=saldo_laba). Tambahkan dulu di COA.")

    # Hitung saldo pendapatan/beban/hpp per akun untuk periode ini
    bal, _ = await _calc_balances(start, end, unit_uid)
    entries = []
    total_pend, total_beban = 0.0, 0.0
    user = await user_from_payload(dep)
    now = now_utc().isoformat()

    def _mk_tx(dr, cr, amt, desc):
        return {
            "id": str(uuid.uuid4()), "date": end,
            "unit_usaha_id": unit_uid,
            "transaction_type": "jurnal_penutup",
            "description": desc,
            "amount": round(amt, 2),
            "debit_account_code": dr, "credit_account_code": cr,
            "reference": f"CLOSE-{period}-{group}",
            "created_by": user.id, "created_at": now,
            "is_closing": True,
        }

    # 1) DR akun pendapatan (kredit-normal) → CR Ikhtisar
    for code, acc in accounts.items():
        if acc.get("category") == "pendapatan":
            saldo = bal.get(code, {}).get("saldo", 0)
            if saldo > 0:
                entries.append(_mk_tx(code, ikhtisar, saldo, f"Tutup pendapatan {code}"))
                total_pend += saldo
    # 2) DR Ikhtisar → CR akun beban/hpp (debit-normal)
    for code, acc in accounts.items():
        if acc.get("category") in ("beban", "hpp"):
            saldo = bal.get(code, {}).get("saldo", 0)
            if saldo > 0:
                entries.append(_mk_tx(ikhtisar, code, saldo, f"Tutup beban/HPP {code}"))
                total_beban += saldo
    # 3) Transfer Ikhtisar → Saldo Laba
    net = total_pend - total_beban
    if net > 0:  # Laba
        entries.append(_mk_tx(ikhtisar, saldo_laba, net, f"Transfer laba {period} ke Saldo Laba"))
    elif net < 0:  # Rugi
        entries.append(_mk_tx(saldo_laba, ikhtisar, abs(net), f"Transfer rugi {period} ke Saldo Laba"))

    if entries:
        await db.transactions.create_many(entries)
    await db.closed_periods.create({"period": period, "group": group, "closed_at": now, "closed_by": user.id, "laba_bersih": net})
    return {"closed": True, "period": period, "group": group, "entries": len(entries), "laba_bersih": net}

@router.get("/reports/closed-periods")
async def list_closed_periods(_: dict = Depends(get_current_user_payload)):
    docs = await db.closed_periods.select({}, {"_id": 0}).order("period", -1).all(500)
    return docs

@router.delete("/reports/close-period")
async def reopen_period(period: str, group: str, dep: dict = Depends(require_roles(*ADMIN_LEVEL))):
    """Batalkan tutup buku: hapus jurnal penutup dan hapus record closed_periods."""
    r = await db.transactions.remove_many({"reference": f"CLOSE-{period}-{group}", "is_closing": True})
    await db.closed_periods.remove_one({"period": period, "group": group})
    return {"deleted_entries": r.deleted_count}
