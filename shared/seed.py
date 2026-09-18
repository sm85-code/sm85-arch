"""Idempotent startup seed: units, COA from data/COA.xlsx, default users."""
from __future__ import annotations

import logging
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from modules.identity.infrastructure.models import SystemControl, User
from modules.siabumdes.infrastructure.models import Account, UnitUsaha
from shared.config import ROOT_DIR
from shared.database import SessionLocal
from shared.security import hash_password

logger = logging.getLogger("sm85.seed")

COA_PATH = ROOT_DIR / "data" / "COA.xlsx"

UNIT_CATALOG = {
    "BUMDES": ("BUMDes (Pusat)", "Kantor pusat BUMDes"),
    "UU01": ("Pembibitan Domba Garut", "Unit usaha UU01"),
    "UU02": ("Peternakan Ikan Air Tawar Sistem Bioflok", "Unit usaha UU02"),
    "UU03": ("Sewa Kendaraan Angkutan", "Unit usaha UU03"),
    "UU04": ("Karya Raharja Sinergi Digital (KRSD)", "Unit usaha UU04"),
    "UU05": ("Toko Offline BUMDES", "Unit usaha UU05 — inventori"),
    "UU06": ("Toko Online BUMDES", "Unit usaha UU06"),
}

DEFAULT_USERS = [
    ("admin", "admin123@", "Administrator", "admin", None),
    ("direktur", "direktur123@", "Direktur", "direktur", None),
    ("bendahara", "bendahara123@", "Bendahara", "bendahara", None),
    ("penasihat", "penasihat123@", "Penasihat", "penasihat", None),
    ("pengawas", "pengawas123@", "Pengawas", "pengawas", None),
    ("pengelola1", "unit1.iyes", "Pengelola UU01", "pengelola", "UU01"),
    ("pengelola2", "unit2.iyes", "Pengelola UU02", "pengelola", "UU02"),
    ("pengelola3", "unit3.iyes", "Pengelola UU03", "pengelola", "UU03"),
    ("pengelola4", "unit4.iyes", "Pengelola UU04", "pengelola", "UU04"),
    ("pengelola5", "unit5.iyes", "Pengelola UU05", "pengelola", "UU05"),
    ("pengelola6", "unit6.iyes", "Pengelola UU06", "pengelola", "UU06"),
]


def _iter_coa_rows(path: Path):
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        code = str(row[0]).strip() if row[0] else ""
        if not code or code.lower() == "kode akun":
            continue
        name = str(row[1] or "").strip()
        category = str(row[2] or "").strip().lower()
        subcategory = str(row[3] or "").strip().lower()
        normal_balance = str(row[4] or "").strip().lower()
        group = str(row[5] or "BUMDES").strip().upper()
        if not name or not category or normal_balance not in {"debit", "kredit"}:
            continue
        yield {
            "code": code,
            "name": name,
            "category": category,
            "subcategory": subcategory,
            "normal_balance": normal_balance,
            "group_code": group,
        }
    wb.close()


async def seed_if_needed() -> None:
    async with SessionLocal() as session:
        try:
            units = await _seed_units(session)
            await _seed_coa(session, units)
            await _seed_users(session, units)
            existing = await session.get(SystemControl, "default")
            if not existing:
                session.add(SystemControl(id="default", recording_locked=False))
            await session.commit()
            logger.info("seed completed")
        except Exception:
            await session.rollback()
            logger.exception("seed failed")
            raise


async def _seed_units(session) -> dict[str, UnitUsaha]:
    existing = {u.code: u for u in (await session.execute(select(UnitUsaha))).scalars()}
    for code, (name, desc) in UNIT_CATALOG.items():
        if code == "BUMDES":
            continue
        if code in existing:
            row = existing[code]
            if row.name != name or row.description != desc:
                row.name = name
                row.description = desc
            continue
        row = UnitUsaha(code=code, name=name, description=desc, active=True)
        session.add(row)
        existing[code] = row
    await session.flush()
    return existing


async def _seed_coa(session, units: dict[str, UnitUsaha]) -> None:
    if not COA_PATH.is_file():
        logger.warning("COA workbook missing at %s", COA_PATH)
        return
    inserted = 0
    for item in _iter_coa_rows(COA_PATH):
        unit = units.get(item["group_code"])
        stmt = (
            pg_insert(Account)
            .values(
                code=item["code"],
                name=item["name"],
                category=item["category"],
                subcategory=item["subcategory"],
                normal_balance=item["normal_balance"],
                group_code=item["group_code"],
                unit_usaha_id=unit.id if unit else None,
                active=True,
            )
            .on_conflict_do_nothing(constraint="uq_accounts_code_group")
        )
        result = await session.execute(stmt)
        inserted += result.rowcount or 0
    logger.info("coa seed inserted=%s", inserted)


async def _seed_users(session, units: dict[str, UnitUsaha]) -> None:
    existing = {u.username: u for u in (await session.execute(select(User))).scalars()}
    created = 0
    for username, password, name, role, unit_code in DEFAULT_USERS:
        if username in existing:
            continue
        unit = units.get(unit_code) if unit_code else None
        session.add(
            User(
                username=username,
                email=f"{username}@siabumdes.local",
                name=name,
                password_hash=hash_password(password),
                role=role,
                unit_usaha_id=unit.id if unit else None,
                must_change_password=False,
                active=True,
            )
        )
        created += 1
    logger.info("user seed created=%s", created)
