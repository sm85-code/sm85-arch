"""Financial reports JSON + PDF/Excel matching frontend-siabumdes."""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.api.deps import get_current_user, require_roles
from adapters.api.scope import is_pengelola, parse_date, scoped_unit_id
from adapters.external.excel_adapter import generate_excel_report
from adapters.external.pdf_generator import generate_pdf_report
from modules.identity.application.services import list_closed_periods
from modules.identity.infrastructure.models import User
from modules.siabumdes.application.reporting import ReportingService
from shared.config import REPORT_READ_LEVEL
from shared.database import get_db

router = APIRouter(prefix="/api", tags=["reports"])
READ = require_roles(*REPORT_READ_LEVEL)


async def _scope(session, user: User, unit_usaha_id: Optional[str]) -> Optional[str]:
    if is_pengelola(user):
        return await scoped_unit_id(session, user, None)
    return unit_usaha_id or None


def _need_dates(start_date: Optional[str], end_date: Optional[str]) -> tuple[date, date]:
    start = parse_date(start_date)
    end = parse_date(end_date)
    if not start or not end:
        raise HTTPException(status_code=400, detail="start_date dan end_date wajib")
    return start, end


def _pdf(title: str, headers: list[str], rows: list[list], subtitle: str = "") -> Response:
    blob = generate_pdf_report(title=title, subtitle=subtitle, table_headers=headers, table_rows=rows)
    return Response(
        content=blob,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{title.replace(" ", "-")}.pdf"'},
    )


def _xlsx(title: str, headers: list[str], rows: list[list]) -> Response:
    blob = generate_excel_report(headers, rows, title)
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{title.replace(" ", "-")}.xlsx"'},
    )


def _ekuitas_export_rows(data: dict) -> list[list]:
    rows: list[list] = []
    for r in data.get("rows") or []:
        indent = int(r.get("indent") or 0)
        label = ("    " * indent) + str(r.get("label") or "")
        if r.get("kind") == "section" and r.get("amount") is None:
            amount = ""
        else:
            amount = r.get("amount")
        rows.append([r.get("no"), label, amount])
    return rows


@router.get("/reports/dashboard")
async def dashboard(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    granularity: str = "month",
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    unit_id = await _scope(session, user, None) if is_pengelola(user) else None
    svc = ReportingService(session)
    return await svc.dashboard(
        parse_date(start_date),
        parse_date(end_date),
        granularity,
        unit_id,
        pusat_kpis=not is_pengelola(user),
    )


@router.get("/reports/laba-rugi")
async def laba_rugi(
    start_date: str,
    end_date: str,
    unit_usaha_id: Optional[str] = None,
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    start, end = _need_dates(start_date, end_date)
    return await ReportingService(session).laba_rugi(start, end, await _scope(session, user, unit_usaha_id))


@router.get("/reports/neraca")
async def neraca(
    as_of_date: str,
    unit_usaha_id: Optional[str] = None,
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    as_of = parse_date(as_of_date)
    if not as_of:
        raise HTTPException(status_code=400, detail="as_of_date wajib")
    return await ReportingService(session).neraca(as_of, await _scope(session, user, unit_usaha_id))


@router.get("/reports/arus-kas")
async def arus_kas(
    start_date: str,
    end_date: str,
    unit_usaha_id: Optional[str] = None,
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    start, end = _need_dates(start_date, end_date)
    return await ReportingService(session).arus_kas(start, end, await _scope(session, user, unit_usaha_id))


@router.get("/reports/perubahan-ekuitas")
async def perubahan_ekuitas(
    start_date: str,
    end_date: str,
    unit_usaha_id: Optional[str] = None,
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    start, end = _need_dates(start_date, end_date)
    return await ReportingService(session).perubahan_ekuitas(start, end, await _scope(session, user, unit_usaha_id))


@router.get("/reports/calk")
async def calk(
    start_date: str,
    end_date: str,
    unit_usaha_id: Optional[str] = None,
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    start, end = _need_dates(start_date, end_date)
    return await ReportingService(session).calk(start, end, await _scope(session, user, unit_usaha_id))


@router.get("/reports/ledger")
@router.get("/reports/buku-besar")
async def ledger(
    account_code: str,
    start_date: str,
    end_date: str,
    unit_usaha_id: Optional[str] = None,
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    start, end = _need_dates(start_date, end_date)
    try:
        return await ReportingService(session).ledger(
            account_code, start, end, await _scope(session, user, unit_usaha_id)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reports/per-unit")
async def per_unit(
    start_date: str,
    end_date: str,
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    start, end = _need_dates(start_date, end_date)
    data = await ReportingService(session).per_unit(start, end)
    if is_pengelola(user):
        data["units"] = [u for u in data["units"] if u["id"] == user.unit_usaha_id]
        data["bumdes"] = {"pendapatan": 0, "beban": 0, "laba_bersih": 0}
    return data


@router.get("/reports/penutupan-periode")
async def penutupan_periode(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    rows = await list_closed_periods(session)
    return [
        {"period": r.period, "group": r.group_code, "laba_bersih": float(r.laba_bersih), "entries": r.entries}
        for r in rows
    ]


@router.get("/reports/{report_key}/pdf")
async def report_pdf(
    report_key: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    as_of_date: Optional[str] = None,
    unit_usaha_id: Optional[str] = None,
    account_code: Optional[str] = None,
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    payload, title, headers, rows = await _materialize(
        session, user, report_key, start_date, end_date, as_of_date, unit_usaha_id, account_code
    )
    subtitle = ""
    if start_date and end_date:
        subtitle = f"{start_date} s.d. {end_date}"
    elif as_of_date:
        subtitle = f"Per {as_of_date}"
    return _pdf(title, headers, rows, subtitle)


@router.get("/reports/{report_key}/excel")
async def report_excel(
    report_key: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    as_of_date: Optional[str] = None,
    unit_usaha_id: Optional[str] = None,
    account_code: Optional[str] = None,
    user: User = Depends(READ),
    session: AsyncSession = Depends(get_db),
):
    payload, title, headers, rows = await _materialize(
        session, user, report_key, start_date, end_date, as_of_date, unit_usaha_id, account_code
    )
    return _xlsx(title, headers, rows)


async def _materialize(
    session,
    user: User,
    report_key: str,
    start_date: Optional[str],
    end_date: Optional[str],
    as_of_date: Optional[str],
    unit_usaha_id: Optional[str],
    account_code: Optional[str],
):
    svc = ReportingService(session)
    unit_id = await _scope(session, user, unit_usaha_id)
    if report_key == "laba-rugi":
        start, end = _need_dates(start_date, end_date)
        data = await svc.laba_rugi(start, end, unit_id)
        rows = [["Pendapatan", "", data["total_pendapatan"]]]
        rows += [[r["code"], r["name"], r["amount"]] for r in data["pendapatan"]]
        rows += [["Beban", "", data["total_beban"]]]
        rows += [[r["code"], r["name"], r["amount"]] for r in data["beban"]]
        rows.append(["Laba bersih", "", data["laba_bersih"]])
        return data, "Laba Rugi", ["Kode", "Nama", "Nominal"], rows
    if report_key == "neraca":
        as_of = parse_date(as_of_date or end_date)
        if not as_of:
            raise HTTPException(status_code=400, detail="as_of_date wajib")
        data = await svc.neraca(as_of, unit_id)
        rows = [[r["code"], r["name"], r["amount"]] for r in data["aset"] + data["kewajiban"] + data["ekuitas"]]
        return data, "Neraca", ["Kode", "Nama", "Nominal"], rows
    if report_key == "arus-kas":
        start, end = _need_dates(start_date, end_date)
        data = await svc.arus_kas(start, end, unit_id)
        rows = [[i["date"], i["description"], i["amount"]] for i in data["kas_masuk"] + data["kas_keluar"]]
        return data, "Arus Kas", ["Tanggal", "Uraian", "Nominal"], rows
    if report_key == "perubahan-ekuitas":
        start, end = _need_dates(start_date, end_date)
        data = await svc.perubahan_ekuitas(start, end, unit_id)
        return data, "Laporan Perubahan Ekuitas", ["No.", "Uraian", "Jumlah (Rp)"], _ekuitas_export_rows(data)
    if report_key == "calk":
        start, end = _need_dates(start_date, end_date)
        data = await svc.calk(start, end, unit_id)
        rows = [[k, v] for k, v in data["ringkasan_kinerja"].items()]
        return data, "CaLK", ["Uraian", "Nilai"], rows
    if report_key in {"ledger", "buku-besar"}:
        if not account_code:
            raise HTTPException(status_code=400, detail="account_code wajib")
        start, end = _need_dates(start_date, end_date)
        data = await svc.ledger(account_code, start, end, unit_id)
        rows = [[e["date"], e["description"], e["debit"], e["credit"], e["balance"]] for e in data["entries"]]
        return data, f"Buku Besar {account_code}", ["Tanggal", "Uraian", "Debit", "Kredit", "Saldo"], rows
    if report_key == "per-unit":
        start, end = _need_dates(start_date, end_date)
        data = await svc.per_unit(start, end)
        if is_pengelola(user):
            data["units"] = [u for u in data["units"] if u["id"] == user.unit_usaha_id]
        rows = [[u["code"], u["name"], u["pendapatan"], u["beban"], u["laba_bersih"]] for u in data["units"]]
        return data, "Kinerja Per Unit", ["Kode", "Nama", "Pendapatan", "Beban", "Laba"], rows
    raise HTTPException(status_code=404, detail="Jenis laporan tidak dikenali")
