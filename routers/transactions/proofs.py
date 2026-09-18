"""Modular routes: proofs"""
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
    _check_period_not_blocked,
    _check_period_not_closed,
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

@router.post("/transactions/{tx_id}/proof")
async def upload_proof(
    tx_id: str,
    file: UploadFile = File(...),
    dep: dict = Depends(require_roles("admin", "direktur", "bendahara", "pengelola")),
):
    """Tambah bukti transaksi ke Google Drive (maks 3 file per transaksi)."""
    from services import gdrive_service
    tok = await db.oauth_tokens.select_one({"provider": "gdrive"}, {"_id": 0})
    if not tok or not tok.get("refresh_token"):
        raise HTTPException(status_code=503, detail="Google Drive belum terhubung. Admin perlu klik 'Hubungkan Drive' terlebih dahulu.")
    refresh_token = tok["refresh_token"]
    tx = await db.transactions.select_one({"id": tx_id}, {"_id": 0})
    if not tx:
        raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan")
    # Scope pengelola: hanya bukti transaksi unit sendiri
    if dep.get("role") == "pengelola" and tx.get("unit_usaha_id") != dep.get("unit"):
        raise HTTPException(status_code=403, detail="Bukan transaksi unit Anda")
    await _check_period_not_blocked(dep, tx.get("date", ""))

    # Ambil daftar bukti (auto-migrate dari field lama `proof`)
    proofs = list(tx.get("proofs") or [])
    if not proofs and tx.get("proof"):
        proofs = [tx["proof"]]
    if len(proofs) >= 3:
        raise HTTPException(status_code=400, detail="Maksimal 3 file bukti per transaksi.")

    # Validasi ekstensi
    fname = (file.filename or "").lower()
    ext = fname.rsplit(".", 1)[-1] if "." in fname else ""
    if ext not in ("pdf", "jpg", "jpeg", "png"):
        raise HTTPException(status_code=400, detail="Format harus PDF/JPG/JPEG/PNG")

    # Baca isi & cek ukuran max 1 MB
    data = await file.read()
    if len(data) > 1 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Ukuran file maksimal 1 MB")

    # Tentukan kode kelompok
    if tx.get("unit_usaha_id"):
        u = await db.unit_usaha.select_one({"id": tx["unit_usaha_id"]}, {"_id": 0, "code": 1})
        group_code = (u or {}).get("code") or "UNIT"
    else:
        group_code = "BUMDES"

    # Format tanggal DDMMYYYY
    d = tx.get("date", "")  # YYYY-MM-DD
    try:
        y, m, day = d.split("-")
        ddmmyyyy = f"{day}{m}{y}"
    except Exception:
        raise HTTPException(status_code=400, detail="Tanggal transaksi tidak valid")

    # Urut per kelompok per tanggal — hitung total file bukti (bukan hanya transaksi)
    q_same = {"date": tx.get("date")}
    if tx.get("unit_usaha_id"):
        q_same["unit_usaha_id"] = tx["unit_usaha_id"]
    else:
        q_same["unit_usaha_id"] = None
    max_urut = 0
    async for other in db.transactions.select(q_same, {"_id": 0, "proofs": 1, "proof": 1}):
        arr = list(other.get("proofs") or [])
        if not arr and other.get("proof"):
            arr = [other["proof"]]
        for p in arr:
            u_val = int(p.get("urut") or 0)
            if u_val > max_urut:
                max_urut = u_val
    urut = max_urut + 1

    new_name = f"{group_code}_{ddmmyyyy}_{urut}.{ext}"
    meta = gdrive_service.upload_bytes(refresh_token, data, new_name)

    proof = {
        "file_id": meta["id"],
        "file_name": meta["name"],
        "url": meta.get("webViewLink"),
        "urut": urut,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "uploaded_by": dep.get("sub"),
        "size": len(data),
    }
    proofs.append(proof)
    await db.transactions.modify_one(
        {"id": tx_id},
        {"set": {"proofs": proofs}, "unset": {"proof": ""}},
    )
    return {"ok": True, "proofs": proofs}

@router.delete("/transactions/{tx_id}/proofs/{file_id}")
async def delete_proof(
    tx_id: str,
    file_id: str,
    dep: dict = Depends(require_roles("admin", "direktur", "bendahara", "pengelola")),
):
    """Hapus 1 file bukti dari transaksi (juga hapus di Google Drive)."""
    from services import gdrive_service
    tok = await db.oauth_tokens.select_one({"provider": "gdrive"}, {"_id": 0})
    refresh_token = (tok or {}).get("refresh_token")
    tx = await db.transactions.select_one({"id": tx_id}, {"_id": 0})
    if not tx:
        raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan")
    if dep.get("role") == "pengelola" and tx.get("unit_usaha_id") != dep.get("unit"):
        raise HTTPException(status_code=403, detail="Bukan transaksi unit Anda")
    await _check_period_not_blocked(dep, tx.get("date", ""))

    proofs = list(tx.get("proofs") or [])
    if not proofs and tx.get("proof"):
        proofs = [tx["proof"]]
    target = next((p for p in proofs if p.get("file_id") == file_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="File bukti tidak ditemukan")

    if refresh_token:
        gdrive_service.delete_file(refresh_token, file_id)

    proofs = [p for p in proofs if p.get("file_id") != file_id]
    await db.transactions.modify_one(
        {"id": tx_id},
        {"set": {"proofs": proofs}, "unset": {"proof": ""}},
    )
    return {"ok": True, "proofs": proofs}

@router.post("/transactions/verify-proofs")
async def verify_proofs(dep: dict = Depends(require_roles("admin", "direktur", "bendahara", "pengelola"))):
    """Cek semua file bukti di Google Drive. Jika sudah dihapus oleh pemilik akun Drive,
    hapus juga entry di DB agar link di aplikasi ikut hilang."""
    from services import gdrive_service
    tok = await db.oauth_tokens.select_one({"provider": "gdrive"}, {"_id": 0})
    refresh_token = (tok or {}).get("refresh_token")
    if not refresh_token:
        return {"ok": True, "checked": 0, "removed": 0, "note": "Drive belum terhubung"}

    q: dict = {"any_of": [{"proofs": {"$exists": True, "$ne": []}}, {"proof": {"$exists": True}}]}
    # Scope pengelola: hanya transaksi unitnya
    if dep.get("role") == "pengelola":
        q["unit_usaha_id"] = dep.get("unit")

    checked = 0
    removed = 0
    async for tx in db.transactions.select(q, {"_id": 0, "id": 1, "proofs": 1, "proof": 1}):
        arr = list(tx.get("proofs") or [])
        if not arr and tx.get("proof"):
            arr = [tx["proof"]]
        if not arr:
            continue
        kept = []
        changed = False
        for p in arr:
            fid = p.get("file_id")
            if not fid:
                continue
            checked += 1
            if gdrive_service.file_exists(refresh_token, fid):
                kept.append(p)
            else:
                removed += 1
                changed = True
        if changed:
            await db.transactions.modify_one(
                {"id": tx["id"]},
                {"set": {"proofs": kept}, "unset": {"proof": ""}},
            )
    return {"ok": True, "checked": checked, "removed": removed}
