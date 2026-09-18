"""Authentication routes: gdrive"""
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

@router.get("/admin/gdrive/status")
async def gdrive_status(_: dict = Depends(require_roles("admin"))):
    tok = await db.oauth_tokens.select_one({"provider": "gdrive"}, {"_id": 0, "refresh_token": 0})
    return {"connected": bool(tok), "email": (tok or {}).get("email"), "connected_at": (tok or {}).get("connected_at")}

@router.get("/admin/gdrive/connect")
async def gdrive_connect(request: Request, _: dict = Depends(require_roles("admin"))):
    """Return URL untuk memulai OAuth flow dengan redirect URI terkonfigurasi."""
    from services import gdrive_service
    import secrets
    redirect_uri = os.environ.get("GDRIVE_REDIRECT_URI")
    if not redirect_uri:
        proto = request.headers.get("x-forwarded-proto", "http")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if not host:
            raise HTTPException(status_code=500, detail="GDRIVE_REDIRECT_URI belum dikonfigurasi")
        redirect_uri = f"{proto}://{host}/api/gdrive/oauth-callback"
    state = secrets.token_urlsafe(24)
    await db.oauth_states.modify_one(
        {"state": state},
        {"set": {"provider": "gdrive", "redirect_uri": redirect_uri}},
        upsert=True,
    )
    return {"auth_url": gdrive_service.auth_url(state, redirect_uri), "redirect_uri": redirect_uri}

@router.get("/gdrive/oauth-callback")
async def gdrive_callback(code: str, state: str):
    """Callback dari Google. Tukar code → refresh_token dan simpan.
    redirect_uri diambil dari state yang sama untuk memastikan cocok dengan yang
    dipakai saat auth URL dibuat."""
    from services import gdrive_service
    st = await db.oauth_states.select_one({"state": state}, {"_id": 0})
    if not st:
        return HTMLResponse("<h3>State tidak valid.</h3>", status_code=400)
    redirect_uri = st.get("redirect_uri") or os.environ.get("GDRIVE_OAUTH_REDIRECT")
    await db.oauth_states.remove_one({"state": state})
    try:
        tokens = gdrive_service.exchange_code(code, redirect_uri)
        if not tokens.get("refresh_token"):
            return HTMLResponse("<h3>Refresh token tidak diterima. Coba lagi.</h3>", status_code=400)
        # 📌 Baris perbaikan untuk file server.py Anda:
        # Ambil email pemilik akun
        from google.oauth2.credentials import Credentials as OC
        from googleapiclient.discovery import build as _b
        import json as _j

        # 🌟 PERBAIKAN: Membaca teks string mentah langsung dari RAM DigitalOcean
        oauth_json_string = os.environ.get("GDRIVE_OAUTH_CLIENT_DATA")
        if not oauth_json_string:
            raise RuntimeError("Variabel lingkungan GDRIVE_OAUTH_CLIENT_DATA belum disetel!")

        cfg = _j.loads(oauth_json_string)
        w = cfg.get("web") or cfg.get("installed") or {}


        cr = OC(token=tokens["token"], refresh_token=tokens["refresh_token"],
                token_uri=w.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=w["client_id"], client_secret=w["client_secret"],
                scopes=tokens["scopes"])
        try:
            about = _b("drive", "v3", credentials=cr, cache_discovery=False).about().get(fields="user").execute()
            email = (about.get("user") or {}).get("emailAddress")
        except Exception:
            email = None
        await db.oauth_tokens.modify_one(
            {"provider": "gdrive"},
            {"set": {
                "provider": "gdrive",
                "refresh_token": tokens["refresh_token"],
                "email": email,
                "connected_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px;text-align:center'>"
            "<h2 style='color:#2E4F7C'>Google Drive terhubung.</h2>"
            f"<p>Akun: <b>{email or '-'}</b></p>"
            "<p>Silakan tutup tab ini dan kembali ke aplikasi.</p>"
            "</body></html>"
        )
    except Exception as e:
        return HTMLResponse(f"<h3>Gagal: {e}</h3>", status_code=500)
