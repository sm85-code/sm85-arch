"""Modular routes: pdf"""
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
    scope_unit_for_pengelola,
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

@router.get("/reports/laba-rugi/pdf")
async def pdf_lr(start_date: str, end_date: str, unit_usaha_id: Optional[str] = None,
                 payload: dict = Depends(require_roles(*REPORT_READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    data = await _laba_rugi(start_date, end_date, unit_usaha_id)
    sig = _signature_block()
    def build():
        story = []
        _pdf_header(story, _STYLES, "LAPORAN LABA RUGI", f"Periode: {start_date} s.d. {end_date}")
        rows = [[P("Kode", _CELL_BOLD), P("Nama Akun", _CELL_BOLD), P("Jumlah", _CELL_BOLD_RIGHT)]]
        section_rows = []
        rows.append(_section_row("PENDAPATAN", 3)); section_rows.append(len(rows) - 1)
        for it in data["pendapatan"]:
            rows.append([P(it["code"]), P(it["name"]), P(fmt_rp(it["amount"]), _CELL_RIGHT)])
        rows.append([P(""), P("Total Pendapatan", _CELL_BOLD), P(f"{fmt_rp(data['total_pendapatan'])}", _CELL_BOLD_RIGHT)])
        rows.append(_section_row("BEBAN", 3)); section_rows.append(len(rows) - 1)
        for it in data["beban"]:
            rows.append([P(it["code"]), P(it["name"]), P(fmt_rp(it["amount"]), _CELL_RIGHT)])
        rows.append([P(""), P("Total Beban", _CELL_BOLD), P(f"{fmt_rp(data['total_beban'])}", _CELL_BOLD_RIGHT)])
        rows.append([P(""), P("LABA / (RUGI) BERSIH", _CELL_BOLD), P(f"{fmt_rp(data['laba_bersih'])}", _CELL_BOLD_RIGHT)])
        total_row_idx = len(rows) - 1
        t = Table(rows, colWidths=[2.3 * cm, 9.7 * cm, 5.5 * cm], repeatRows=1)
        ts = _table_style()
        for sr in section_rows:
            ts.add("BACKGROUND", (0, sr), (-1, sr), colors.HexColor("#EEF3F9"))
        ts.add("BACKGROUND", (0, total_row_idx), (-1, total_row_idx), colors.HexColor("#DCE8FE"))
        t.setStyle(ts)
        story.append(t)
        story.extend(sig)
        return story
    return _pdf_response(build, f"Laba-Rugi_{start_date}_sd_{end_date}.pdf")

@router.get("/reports/neraca/pdf")
async def pdf_neraca(as_of_date: str, unit_usaha_id: Optional[str] = None,
                     payload: dict = Depends(require_roles(*REPORT_READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    data = await _neraca(as_of_date, unit_usaha_id)
    sig = _signature_block()
    def build():
        story = []
        _pdf_header(story, _STYLES, "NERACA", f"Per tanggal: {as_of_date}")
        rows = [[P("Kode", _CELL_BOLD), P("Akun", _CELL_BOLD), P("Jumlah", _CELL_BOLD_RIGHT)]]
        section_rows = []
        rows.append(_section_row("ASET", 3)); section_rows.append(len(rows) - 1)
        for it in data["aset"]:
            rows.append([P(it["code"]), P(it["name"]), P(fmt_rp(it["amount"]), _CELL_RIGHT)])
        rows.append([P(""), P("Total Aset", _CELL_BOLD), P(f"{fmt_rp(data['total_aset'])}", _CELL_BOLD_RIGHT)])
        rows.append(_section_row("KEWAJIBAN", 3)); section_rows.append(len(rows) - 1)
        for it in data["kewajiban"]:
            rows.append([P(it["code"]), P(it["name"]), P(fmt_rp(it["amount"]), _CELL_RIGHT)])
        rows.append([P(""), P("Total Kewajiban", _CELL_BOLD), P(f"{fmt_rp(data['total_kewajiban'])}", _CELL_BOLD_RIGHT)])
        rows.append(_section_row("EKUITAS", 3)); section_rows.append(len(rows) - 1)
        for it in data["ekuitas"]:
            rows.append([P(it["code"]), P(it["name"]), P(fmt_rp(it["amount"]), _CELL_RIGHT)])
        rows.append([P(""), P("Total Ekuitas", _CELL_BOLD), P(f"{fmt_rp(data['total_ekuitas'])}", _CELL_BOLD_RIGHT)])
        rows.append([P(""), P("TOTAL PASIVA (Kewajiban + Ekuitas)", _CELL_BOLD),
                     P(f"{fmt_rp(data['total_pasiva'])}", _CELL_BOLD_RIGHT)])
        total_row_idx = len(rows) - 1
        t = Table(rows, colWidths=[2.3 * cm, 9.7 * cm, 5.5 * cm], repeatRows=1)
        ts = _table_style()
        for sr in section_rows:
            ts.add("BACKGROUND", (0, sr), (-1, sr), colors.HexColor("#EEF3F9"))
        ts.add("BACKGROUND", (0, total_row_idx), (-1, total_row_idx), colors.HexColor("#DCE8FE"))
        t.setStyle(ts)
        story.append(t)
        story.extend(sig)
        return story
    return _pdf_response(build, f"Neraca_{as_of_date}.pdf")

@router.get("/reports/arus-kas/pdf")
async def pdf_ak(start_date: str, end_date: str, unit_usaha_id: Optional[str] = None,
                 payload: dict = Depends(require_roles(*REPORT_READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    data = await _arus_kas(start_date, end_date, unit_usaha_id)
    sig = _signature_block()
    def build():
        story = []
        _pdf_header(story, _STYLES, "LAPORAN ARUS KAS", f"Periode: {start_date} s.d. {end_date}")
        rows = [[P("Tanggal", _CELL_BOLD), P("Keterangan", _CELL_BOLD), P("Jumlah", _CELL_BOLD_RIGHT)]]
        section_rows = []
        rows.append(_section_row("KAS MASUK", 3)); section_rows.append(len(rows) - 1)
        for it in data["kas_masuk"]:
            rows.append([P(it["date"]), P(it["description"]), P(fmt_rp(it["amount"]), _CELL_RIGHT)])
        rows.append([P(""), P("Total Kas Masuk", _CELL_BOLD), P(f"{fmt_rp(data['total_masuk'])}", _CELL_BOLD_RIGHT)])
        rows.append(_section_row("KAS KELUAR", 3)); section_rows.append(len(rows) - 1)
        for it in data["kas_keluar"]:
            rows.append([P(it["date"]), P(it["description"]), P(fmt_rp(it["amount"]), _CELL_RIGHT)])
        rows.append([P(""), P("Total Kas Keluar", _CELL_BOLD), P(f"{fmt_rp(data['total_keluar'])}", _CELL_BOLD_RIGHT)])
        rows.append([P(""), P("ARUS KAS BERSIH", _CELL_BOLD), P(f"{fmt_rp(data['arus_kas_bersih'])}", _CELL_BOLD_RIGHT)])
        total_row_idx = len(rows) - 1
        t = Table(rows, colWidths=[2.5 * cm, 9.5 * cm, 5.5 * cm], repeatRows=1)
        ts = _table_style()
        for sr in section_rows:
            ts.add("BACKGROUND", (0, sr), (-1, sr), colors.HexColor("#EEF3F9"))
        ts.add("BACKGROUND", (0, total_row_idx), (-1, total_row_idx), colors.HexColor("#DCE8FE"))
        t.setStyle(ts)
        story.append(t)
        story.extend(sig)
        return story
    return _pdf_response(build, f"Arus-Kas_{start_date}_sd_{end_date}.pdf")

@router.get("/reports/perubahan-ekuitas/pdf")
async def pdf_pe(start_date: str, end_date: str, unit_usaha_id: Optional[str] = None,
                 payload: dict = Depends(require_roles(*REPORT_READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    data = await _perubahan_ekuitas(start_date, end_date, unit_usaha_id)
    sig = _signature_block()

    def build():
        story = []
        _pdf_header(story, _STYLES, "LAPORAN PERUBAHAN EKUITAS", f"Periode: {start_date} s.d. {end_date}")
        rows = [[P("No.", _CELL_BOLD), P("Uraian", _CELL_BOLD), P("Jumlah (Rp)", _CELL_BOLD_RIGHT)]]
        section_rows = []
        total_rows = []
        for item in data.get("rows", []):
            amount = "" if item.get("amount") is None else fmt_rp(item["amount"])
            label = ("  " * item.get("indent", 0)) + item["label"]
            rows.append([P(str(item["no"]), _CELL_BOLD if item.get("bold") else None),
                         P(label, _CELL_BOLD if item.get("bold") or item.get("kind") == "section" else None),
                         P(amount, _CELL_BOLD_RIGHT if item.get("bold") else _CELL_RIGHT)])
            if item.get("kind") == "section": section_rows.append(len(rows) - 1)
            if item.get("bold"): total_rows.append(len(rows) - 1)
        t = Table(rows, colWidths=[1.1 * cm, 10.4 * cm, 6 * cm], repeatRows=1)
        ts = _table_style()
        for row_index in section_rows:
            ts.add("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#EEF3F9"))
        for row_index in total_rows:
            ts.add("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#DCE8FE"))
        t.setStyle(ts)
        story.append(t)
        story.extend(sig)
        return story
    return _pdf_response(build, f"Perubahan-Ekuitas_{start_date}_sd_{end_date}.pdf")

@router.get("/reports/per-unit/pdf")
async def pdf_per_unit(start_date: str, end_date: str, _: dict = Depends(get_current_user_payload)):
    data = await _per_unit_report(start_date, end_date)
    sig = _signature_block()
    b = data.get("bumdes") or {}

    def build():
        story = []
        _pdf_header(story, _STYLES, "REKAP KINERJA BUMDES & UNIT USAHA",
                    f"Periode: {start_date} s.d. {end_date}")

        # ==== Tabel Kinerja BUMDES ====
        story.append(P("Kinerja BUMDES", _CELL_BOLD))
        story.append(Spacer(1, 6))
        alokasi_82 = (
            f"PADes (30%) = {fmt_rp(b.get('share_pades_30', 0))}<br/>"
            f"Penasihat (7%) = {fmt_rp(b.get('share_penasihat_7', 0))}<br/>"
            f"Pengawas (5%) = {fmt_rp(b.get('share_pengawas_5', 0))}<br/>"
            f"Pengurus (35%) = {fmt_rp(b.get('share_pengurus_35', 0))}<br/>"
            f"Dana Sosial (5%) = {fmt_rp(b.get('share_dana_sosial_5', 0))}"
        )
        bumdes_rows = [
            [P("Kode", _CELL_BOLD), P("Pendapatan", _CELL_BOLD_RIGHT),
             P("Beban", _CELL_BOLD_RIGHT), P("Laba Bersih", _CELL_BOLD_RIGHT),
             P("18% Modal", _CELL_BOLD_RIGHT), P("82% Unsur Lain", _CELL_BOLD)],
            [P("BUMDES"),
             P(fmt_rp(b.get("pendapatan", 0)), _CELL_RIGHT),
             P(fmt_rp(b.get("beban", 0)), _CELL_RIGHT),
             P(fmt_rp(b.get("laba_bersih", 0)), _CELL_RIGHT),
             P(fmt_rp(b.get("share_modal_18", 0)), _CELL_RIGHT),
             P(alokasi_82)],
        ]
        tb = Table(bumdes_rows, colWidths=[1.5 * cm, 2.6 * cm, 2.4 * cm, 2.8 * cm, 2.4 * cm, 5.8 * cm], repeatRows=1)
        tb.setStyle(_table_style())
        story.append(tb)
        story.append(Spacer(1, 14))

        # ==== Tabel Kinerja Per Unit ====
        story.append(P("Kinerja Per Unit Usaha", _CELL_BOLD))
        story.append(Spacer(1, 6))
        rows = [[P("Kode", _CELL_BOLD), P("Unit Usaha", _CELL_BOLD),
                 P("Pendapatan", _CELL_BOLD_RIGHT), P("Beban", _CELL_BOLD_RIGHT),
                 P("Laba Bersih", _CELL_BOLD_RIGHT),
                 P("30% Pengelola", _CELL_BOLD_RIGHT), P("70% BUMDES", _CELL_BOLD_RIGHT)]]
        total_p = total_b = total_l = total_m = total_bmd = 0.0
        for u in data["units"]:
            rows.append([P(u["code"]), P(u["name"]),
                         P(fmt_rp(u["pendapatan"]), _CELL_RIGHT), P(fmt_rp(u["beban"]), _CELL_RIGHT),
                         P(fmt_rp(u["laba_bersih"]), _CELL_RIGHT),
                         P(fmt_rp(u["share_pengelola_30"]), _CELL_RIGHT),
                         P(fmt_rp(u["share_bumdes_70"]), _CELL_RIGHT)])
            total_p += u["pendapatan"]; total_b += u["beban"]; total_l += u["laba_bersih"]
            total_m += u["share_pengelola_30"]; total_bmd += u["share_bumdes_70"]
        rows.append([P(""), P("TOTAL", _CELL_BOLD),
                     P(f"{fmt_rp(total_p)}", _CELL_BOLD_RIGHT),
                     P(f"{fmt_rp(total_b)}", _CELL_BOLD_RIGHT),
                     P(f"{fmt_rp(total_l)}", _CELL_BOLD_RIGHT),
                     P(f"{fmt_rp(total_m)}", _CELL_BOLD_RIGHT),
                     P(f"{fmt_rp(total_bmd)}", _CELL_BOLD_RIGHT)])
        total_row_idx = len(rows) - 1
        t = Table(rows, colWidths=[1.4 * cm, 4.6 * cm, 2.4 * cm, 1.9 * cm, 2.4 * cm, 2.4 * cm, 2.4 * cm], repeatRows=1)
        ts = _table_style()
        ts.add("BACKGROUND", (0, total_row_idx), (-1, total_row_idx), colors.HexColor("#DCE8FE"))
        t.setStyle(ts)
        story.append(t)
        story.extend(sig)
        return story
    return _pdf_response(build, f"Rekap-Kinerja_{start_date}_sd_{end_date}.pdf")

@router.get("/reports/calk/pdf")
async def pdf_calk(start_date: str, end_date: str, unit_usaha_id: Optional[str] = None,
                   payload: dict = Depends(require_roles(*READ_LEVEL))):
    unit_usaha_id = await scope_unit_for_pengelola(payload, unit_usaha_id)
    if unit_usaha_id:
        raise HTTPException(status_code=404, detail="CALK hanya tersedia untuk grup BUMDES")
    lr = await _laba_rugi(start_date, end_date)
    nr = await _neraca(end_date)
    ak = await _arus_kas(start_date, end_date)
    data = {
        "informasi_umum": {
            "nama": "BUMDES Karya Raharja",
            "alamat": "Desa Wonoharjo, Kec. Pangandaran",
            "direktur": "Budianto",
            "dasar_hukum": "Kepmendesa PDTT No. 136 Tahun 2022",
        },
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
        ],
    }
    sig = _signature_block()
    def build():
        story = []
        _pdf_header(story, _STYLES, "CATATAN ATAS LAPORAN KEUANGAN (CaLK)",
                    f"Periode: {start_date} s.d. {end_date}")
        story.append(Paragraph("<b>1. Informasi Umum</b>", _STYLES["Heading3"]))
        info = data["informasi_umum"]
        for k, v in info.items():
            story.append(Paragraph(f"• <b>{k.replace('_', ' ').title()}</b>: {v}", _STYLES["Normal"]))
        story.append(Spacer(1, 0.4 * cm))
        story.append(Paragraph("<b>2. Ringkasan Kinerja Keuangan</b>", _STYLES["Heading3"]))
        rk = data["ringkasan_kinerja"]
        rows = [[P("Uraian", _CELL_BOLD), P("Jumlah", _CELL_BOLD_RIGHT)]]
        for k, v in rk.items():
            rows.append([P(k.replace("_", " ").title()), P(fmt_rp(v), _CELL_RIGHT)])
        t = Table(rows, colWidths=[11 * cm, 6 * cm], repeatRows=1)
        t.setStyle(_table_style())
        story.append(t)
        story.append(Spacer(1, 0.4 * cm))
        story.append(Paragraph("<b>3. Kebijakan Akuntansi</b>", _STYLES["Heading3"]))
        for k in data["kebijakan_akuntansi"]:
            story.append(Paragraph(f"• {k}", _STYLES["Normal"]))
            story.append(Spacer(1, 0.15 * cm))
        story.extend(sig)
        return story
    return _pdf_response(build, f"CaLK_{start_date}_sd_{end_date}.pdf")
