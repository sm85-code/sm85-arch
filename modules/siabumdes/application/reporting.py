"""Financial statements computed from transactions + chart of accounts."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.siabumdes.infrastructure.models import Account, Transaction, UnitUsaha
from shared.coa_taxonomy import SUB_LABA_DICADANGKAN, SUB_MODAL_DESA


def _f(value: Decimal | float | int | None) -> float:
    return float(value or 0)


def _r(value: float | Decimal | int | None) -> float:
    return round(float(value or 0), 2)


def _acc_text(acc: Account) -> str:
    return f"{acc.name} {acc.subcategory or ''} {acc.code}".lower()


def _is_laba_account(acc: Account) -> bool:
    text = _acc_text(acc)
    return any(token in text for token in ("laba", "rugi", "ditahan"))


def _is_masyarakat_modal(acc: Account) -> bool:
    return "masyarakat" in _acc_text(acc)


class ReportingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _units(self) -> list[UnitUsaha]:
        return list((await self.session.execute(select(UnitUsaha).order_by(UnitUsaha.code))).scalars())

    async def _accounts(self, group: Optional[str] = None) -> list[Account]:
        stmt = select(Account)
        if group:
            stmt = stmt.where(Account.group_code == group)
        return list((await self.session.execute(stmt)).scalars())

    async def _txs(
        self,
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
        unit_usaha_id: Optional[str] = None,
        pusat_only: bool = False,
    ) -> list[Transaction]:
        stmt = select(Transaction).order_by(Transaction.date.asc(), Transaction.created_at.asc())
        if start:
            stmt = stmt.where(Transaction.date >= start)
        if end:
            stmt = stmt.where(Transaction.date <= end)
        if pusat_only:
            stmt = stmt.where(Transaction.unit_usaha_id.is_(None))
        elif unit_usaha_id:
            stmt = stmt.where(Transaction.unit_usaha_id == unit_usaha_id)
        return list((await self.session.execute(stmt)).scalars())

    async def _group_for(self, unit_usaha_id: Optional[str]) -> str:
        if not unit_usaha_id:
            return "BUMDES"
        unit = await self.session.get(UnitUsaha, unit_usaha_id)
        return unit.code if unit else "BUMDES"

    _HPP_ENTITIES = {"UU05", "UU06"}

    @staticmethod
    def _entity_prefix(entity: str) -> str:
        return f"{(entity or 'BUMDES').strip().upper()}-"

    def _account_index(self, accounts: list[Account], entity: str) -> dict[str, Account]:
        prefix = self._entity_prefix(entity)
        idx: dict[str, Account] = {}
        for acc in accounts:
            idx[acc.code] = acc
            if not acc.code.upper().startswith(prefix):
                idx[f"{entity}-{acc.code}"] = acc
        return idx

    def _code_in_entity(self, code: str, entity: str) -> bool:
        if not code:
            return False
        prefix = self._entity_prefix(entity)
        raw = code.strip()
        return raw.upper().startswith(prefix)

    async def _resolve_entity(
        self,
        unit_usaha_id: Optional[str] = None,
        entity: Optional[str] = None,
    ) -> str:
        if entity:
            return entity.strip().upper()
        return await self._group_for(unit_usaha_id)

    async def laba_rugi(
        self,
        start: date,
        end: date,
        unit_usaha_id: Optional[str] = None,
        entity: Optional[str] = None,
    ) -> dict[str, Any]:
        """Laba rugi per entitas (bukan konsolidasi).

        Entitas: BUMDES | UU01..UU06.
        Saldo neto: pendapatan = kredit - debit; beban/HPP = debit - kredit.
        HPP hanya untuk UU05 dan UU06.
        """
        group = await self._resolve_entity(unit_usaha_id, entity)
        prefix = self._entity_prefix(group)
        has_hpp = group in self._HPP_ENTITIES
        account_rows = await self._accounts(group)
        accounts = self._account_index(account_rows, group)
        txs = await self._txs(
            start=start,
            end=end,
            unit_usaha_id=unit_usaha_id,
            pusat_only=unit_usaha_id is None and group == "BUMDES",
        )

        debit: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        credit: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for tx in txs:
            d_code = (tx.debit_account_code or "").strip()
            c_code = (tx.credit_account_code or "").strip()
            in_scope = (
                self._code_in_entity(d_code, group)
                or self._code_in_entity(c_code, group)
                or d_code in accounts
                or c_code in accounts
            )
            if not in_scope:
                continue
            if d_code:
                debit[d_code] += tx.amount
            if c_code:
                credit[c_code] += tx.amount

        def _net_rows(category: str, formula: str) -> list[dict[str, Any]]:
            rows = []
            seen: set[str] = set()
            codes = set(debit) | set(credit) | set(accounts)
            for code in sorted(codes):
                acc = accounts.get(code)
                if not acc or acc.category != category:
                    continue
                if acc.code in seen:
                    continue
                seen.add(acc.code)
                keys = {acc.code, f"{group}-{acc.code}"}
                d = sum((debit.get(k, Decimal("0")) for k in keys), Decimal("0"))
                c = sum((credit.get(k, Decimal("0")) for k in keys), Decimal("0"))
                amt = (c - d) if formula == "kredit" else (d - c)
                if amt == 0:
                    continue
                rows.append({
                    "code": acc.code if acc.code.upper().startswith(prefix) else f"{group}-{acc.code}",
                    "name": acc.name,
                    "amount": _f(amt),
                })
            return rows

        pend_rows = _net_rows("pendapatan", "kredit")
        hpp_rows = _net_rows("hpp", "debit") if has_hpp else []
        beban_rows = _net_rows("beban", "debit")
        total_p = _r(sum(r["amount"] for r in pend_rows))
        total_hpp = _r(sum(r["amount"] for r in hpp_rows)) if has_hpp else 0.0
        total_b = _r(sum(r["amount"] for r in beban_rows))
        laba_kotor = _r(total_p - total_hpp) if has_hpp else total_p
        laba_bersih = _r(laba_kotor - total_b)
        return {
            "pendapatan": pend_rows,
            "beban": beban_rows,
            "hpp": hpp_rows,
            "total_pendapatan": total_p,
            "total_hpp": total_hpp,
            "laba_kotor": laba_kotor if has_hpp else None,
            "total_beban": total_b,
            "laba_bersih": laba_bersih,
            "has_hpp": has_hpp,
            "format": "laba-kotor" if has_hpp else "ringkas",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "group": group,
            "entity": group,
            "account_prefix": prefix.rstrip("-"),
        }
