"""Startup seed: taxonomy + COA from data/*.xlsx, default users. Resets finance master data."""
from __future__ import annotations

import logging
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from modules.identity.infrastructure.models import SystemControl, User
from modules.siabumdes.infrastructure.models import (
    Account,
    AccountCategory,
    AccountSubcategory,
    JournalEntry,
    JournalItem,
    Transaction,
    UnitUsaha,
)
from shared.coa_taxonomy import COA_PATH, TAXONOMY_PATH, load_taxonomy_rows, valid_pair
from shared.database import SessionLocal
from shared.security import hash_password

logger = logging.getLogger("sm85.seed")

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
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header = [str(c or "").strip().lower() for c in rows[0]]
        if header[:5] != ["code", "name", "category", "subcategory", "normal_balance"]:
            continue
        for raw in rows[1:]:
            if not raw or not raw[0]:
                continue
            code = str(raw[0]).strip()
            name = str(raw[1] or "").strip()
            category = str(raw[2] or "").strip().lower()
            subcategory = str(raw[3] or "").strip().lower()
            normal_balance = str(raw[4] or "").strip().lower()
            group = str(raw[5] or sheet).strip().upper() if len(raw) > 5 else sheet.upper()
            if not name or normal_balance not in {"debit", "kredit"}:
                continue
            if not valid_pair(category, subcategory):
                logger.warning("skip COA %s %s: unknown %s/%s", group, code, category, subcategory)
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
            await _reset_finance(session)
            units = await _seed_units(session)
            await _seed_taxonomy(session)
            await _seed_coa(session, units)
            await _seed_users(session, units)
            existing = await session.get(SystemControl, "default")
            if not existing:
                session.add(SystemControl(id="default", recording_locked=False))
            await session.commit()
            logger.info("seed completed from %s + %s", TAXONOMY_PATH.name, COA_PATH.name)
        except Exception:
            await session.rollback()
            logger.exception("seed failed")
            raise


async def _reset_finance(session) -> None:
    """Empty transactional + COA tables so workbook is the only source."""
    await session.execute(delete(JournalItem))
    await session.execute(delete(JournalEntry))
    await session.execute(delete(Transaction))
    await session.execute(delete(Account))
    await session.execute(delete(AccountSubcategory))
    await session.execute(delete(AccountCategory))
    logger.info("finance master data reset")


async def _seed_units(session) -> dict[str, UnitUsaha]:
    existing = {u.code: u for u in (await session.execute(select(UnitUsaha))).scalars()}
    for code, (name, desc) in UNIT_CATALOG.items():
        if code == "BUMDES":
            continue
        if code in existing:
            row = existing[code]
            row.name = name
            row.description = desc
            continue
        row = UnitUsaha(code=code, name=name, description=desc, active=True)
        session.add(row)
        existing[code] = row
    await session.flush()
    return existing


async def _seed_taxonomy(session) -> None:
    rows = load_taxonomy_rows()
    if not rows:
        logger.warning("taxonomy workbook missing at %s", TAXONOMY_PATH)
        return
    seen_cat: set[str] = set()
    for item in rows:
        if item["category"] not in seen_cat:
            session.add(
                AccountCategory(
                    slug=item["category"],
                    label=item["label_category"],
                    normal_balance=item["normal_balance"] or "debit",
                )
            )
            seen_cat.add(item["category"])
    await session.flush()
    for item in rows:
        session.add(
            AccountSubcategory(
                slug=item["subcategory"],
                category_slug=item["category"],
                label=item["label_subcategory"],
                is_system=item["is_system"],
            )
        )
    await session.flush()
    logger.info("taxonomy seeded categories=%s rows=%s", len(seen_cat), len(rows))


async def _seed_coa(session, units: dict[str, UnitUsaha]) -> None:
    if not COA_PATH.is_file():
        logger.warning("COA workbook missing at %s", COA_PATH)
        return
    inserted = 0
    for item in _iter_coa_rows(COA_PATH):
        unit = units.get(item["group_code"])
        session.add(
            Account(
                code=item["code"],
                name=item["name"],
                category=item["category"],
                subcategory=item["subcategory"],
                normal_balance=item["normal_balance"],
                group_code=item["group_code"],
                unit_usaha_id=unit.id if unit else None,
                active=True,
            )
        )
        inserted += 1
    await session.flush()
    logger.info("coa seed inserted=%s from %s", inserted, COA_PATH.name)


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
