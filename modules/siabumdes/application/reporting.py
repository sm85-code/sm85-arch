"""Financial statements computed from transactions + chart of accounts."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.siabumdes.infrastructure.models import Account, Transaction, UnitUsaha


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

    async def laba_rugi(self, start: date, end: date, unit_usaha_id: Optional[str] = None) -> dict[str, Any]:
        group = await self._group_for(unit_usaha_id)
        accounts = {a.code: a for a in await self._accounts(group)}
        txs = await self._txs(start=start, end=end, unit_usaha_id=unit_usaha_id, pusat_only=unit_usaha_id is None)
        pendapatan: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        beban: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for tx in txs:
            credit = accounts.get(tx.credit_account_code)
            debit = accounts.get(tx.debit_account_code)
            if credit and credit.category == "pendapatan":
                pendapatan[credit.code] += tx.amount
            if debit and debit.category in {"beban", "hpp"}:
                beban[debit.code] += tx.amount
        pend_rows = [
            {"code": c, "name": accounts[c].name, "amount": _f(v)}
            for c, v in sorted(pendapatan.items())
            if c in accounts
        ]
        beban_rows = [
            {"code": c, "name": accounts[c].name, "amount": _f(v)}
            for c, v in sorted(beban.items())
            if c in accounts
        ]
        total_p = sum(r["amount"] for r in pend_rows)
        total_b = sum(r["amount"] for r in beban_rows)
        return {
            "pendapatan": pend_rows,
            "beban": beban_rows,
            "total_pendapatan": total_p,
            "total_beban": total_b,
            "laba_bersih": total_p - total_b,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "group": group,
        }

    async def neraca(self, as_of: date, unit_usaha_id: Optional[str] = None) -> dict[str, Any]:
        group = await self._group_for(unit_usaha_id)
        accounts = {a.code: a for a in await self._accounts(group)}
        txs = await self._txs(end=as_of, unit_usaha_id=unit_usaha_id, pusat_only=unit_usaha_id is None)
        bal: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for tx in txs:
            d, c = tx.debit_account_code, tx.credit_account_code
            if d in accounts:
                nb = accounts[d].normal_balance
                bal[d] += tx.amount if nb == "debit" else -tx.amount
            if c in accounts:
                nb = accounts[c].normal_balance
                bal[c] += tx.amount if nb == "kredit" else -tx.amount

        def pack(cat: str) -> list[dict[str, Any]]:
            rows = []
            for code, acc in sorted(accounts.items()):
                if acc.category != cat:
                    continue
                amt = _f(bal.get(code, Decimal("0")))
                if amt == 0:
                    continue
                rows.append({"code": code, "name": acc.name, "amount": amt})
            return rows

        aset = pack("aset")
        kewajiban = pack("kewajiban")
        ekuitas = pack("ekuitas")
        total_aset = sum(r["amount"] for r in aset)
        total_kew = sum(r["amount"] for r in kewajiban)
        total_eku = sum(r["amount"] for r in ekuitas)
        total_pasiva = total_kew + total_eku
        return {
            "as_of": as_of.isoformat(),
            "aset": aset,
            "kewajiban": kewajiban,
            "ekuitas": ekuitas,
            "total_aset": total_aset,
            "total_kewajiban": total_kew,
            "total_ekuitas": total_eku,
            "total_pasiva": total_pasiva,
            "balanced": abs(total_aset - total_pasiva) < 0.5,
            "group": group,
        }

    async def arus_kas(self, start: date, end: date, unit_usaha_id: Optional[str] = None) -> dict[str, Any]:
        group = await self._group_for(unit_usaha_id)
        accounts = {a.code: a for a in await self._accounts(group)}
        txs = await self._txs(start=start, end=end, unit_usaha_id=unit_usaha_id, pusat_only=unit_usaha_id is None)
        masuk, keluar = [], []
        for tx in txs:
            debit = accounts.get(tx.debit_account_code)
            credit = accounts.get(tx.credit_account_code)
            item = {"date": tx.date.isoformat(), "description": tx.description or tx.transaction_type, "amount": _f(tx.amount)}
            if debit and debit.subcategory == "kas_bank":
                masuk.append(item)
            elif credit and credit.subcategory == "kas_bank":
                keluar.append(item)
        total_masuk = sum(i["amount"] for i in masuk)
        total_keluar = sum(i["amount"] for i in keluar)
        return {
            "kas_masuk": masuk,
            "kas_keluar": keluar,
            "total_masuk": total_masuk,
            "total_keluar": total_keluar,
            "arus_kas_bersih": total_masuk - total_keluar,
        }

    async def _period_penyertaan(
        self,
        start: date,
        end: date,
        unit_usaha_id: Optional[str],
    ) -> tuple[float, float]:
        group = await self._group_for(unit_usaha_id)
        accounts = {a.code: a for a in await self._accounts(group)}
        txs = await self._txs(start=start, end=end, unit_usaha_id=unit_usaha_id, pusat_only=unit_usaha_id is None)
        desa = 0.0
        masyarakat = 0.0
        for tx in txs:
            credit = accounts.get(tx.credit_account_code)
            debit = accounts.get(tx.debit_account_code)
            amt = _f(tx.amount)
            for acc, sign in ((credit, 1.0), (debit, -1.0)):
                if not acc or acc.category != "ekuitas" or _is_laba_account(acc):
                    continue
                if _is_masyarakat_modal(acc):
                    masyarakat += sign * amt
                else:
                    desa += sign * amt
        return _r(desa), _r(masyarakat)

    async def perubahan_ekuitas(self, start: date, end: date, unit_usaha_id: Optional[str] = None) -> dict[str, Any]:
        prev_year = start.year - 1
        prev_end = date(prev_year, 12, 31)
        prev_start = date(prev_year, 1, 1)
        opening = await self.neraca(prev_end, unit_usaha_id)
        prev_lr = await self.laba_rugi(prev_start, prev_end, unit_usaha_id)
        curr_lr = await self.laba_rugi(start, end, unit_usaha_id)

        prev_equity = _r(opening["total_ekuitas"])
        n3 = prev_equity
        n4 = 0.0
        n6, n7 = await self._period_penyertaan(start, end, unit_usaha_id)
        n8 = _r(n3 + n4 + n6 + n7)

        laba_asli = _r(curr_lr["laba_bersih"])
        n11 = 0.0
        n12 = _r(_r(prev_lr["laba_bersih"]) * 0.18)
        n13 = _r(laba_asli - laba_asli * (0.35 + 0.07 + 0.05 + 0.05))
        n15 = _r(laba_asli * 0.20)
        n16 = 0.0
        n17 = _r(n11 + n12 + n13 - n15 - n16)
        n18 = _r(n8 + n17)

        def row(no, label, amount=None, *, kind="data", indent=0, bold=False):
            return {
                "no": no,
                "label": label,
                "amount": amount,
                "kind": kind,
                "indent": indent,
                "bold": bold,
            }

        rows = [
            row(1, "PENYERTAAN MODAL", kind="section"),
            row(2, "Penyertaan modal awal:", kind="section", indent=1),
            row(3, "Penyertaan Modal Desa", n3, indent=2),
            row(4, "Penyertaan Modal Masyarakat", n4, indent=2),
            row(5, "Penambahan Investasi periode berjalan:", kind="section", indent=1),
            row(6, "Penyertaan Modal Desa", n6, indent=2),
            row(7, "Penyertaan Modal Masyarakat", n7, indent=2),
            row(8, "Penyertaan Modal Akhir (3+4+6+7)", n8, indent=1, bold=True),
            row(9, "SALDO LABA", kind="section"),
            row(10, "Saldo Laba Awal:", kind="section", indent=1),
            row(11, "Saldo Laba Tidak Dicadangkan", n11, indent=2),
            row(12, "Saldo Laba Dicadangkan", n12, indent=2),
            row(13, "Laba (Rugi) periode berjalan", n13, indent=1),
            row(14, "Bagi Hasil Penyertaan:", kind="section", indent=1),
            row(15, "Bagi Hasil Penyertaan Modal Desa", n15, indent=2),
            row(16, "Bagi Hasil Penyertaan Modal Masyarakat", n16, indent=2),
            row(17, "Saldo Laba Akhir (11+12+13-15-16)", n17, indent=1, bold=True),
            row(18, "EKUITAS AKHIR (8+17)", n18, kind="data", bold=True),
        ]
        return {
            "rows": rows,
            "modal_awal": n3,
            "laba_periode": n13,
            "laba_bersih_asli": laba_asli,
            "modal_akhir": n18,
            "alokasi": {
                "pengurus_35": _r(laba_asli * 0.35),
                "penasihat_7": _r(laba_asli * 0.07),
                "pengawas_5": _r(laba_asli * 0.05),
                "dana_sosial_5": _r(laba_asli * 0.05),
                "pades_desa_20": n15,
                "penguatan_modal_18_tahun_lalu": n12,
            },
        }

    async def calk(self, start: date, end: date, unit_usaha_id: Optional[str] = None) -> dict[str, Any]:
        lr = await self.laba_rugi(start, end, unit_usaha_id)
        nr = await self.neraca(end, unit_usaha_id)
        ak = await self.arus_kas(start, end, unit_usaha_id)
        return {
            "informasi_umum": {
                "nama": "BUMDes",
                "periode_awal": start.isoformat(),
                "periode_akhir": end.isoformat(),
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

    async def ledger(
        self,
        account_code: str,
        start: date,
        end: date,
        unit_usaha_id: Optional[str] = None,
    ) -> dict[str, Any]:
        group = await self._group_for(unit_usaha_id)
        acc = (
            await self.session.execute(
                select(Account).where(Account.code == account_code, Account.group_code == group)
            )
        ).scalar_one_or_none()
        if not acc:
            raise LookupError(f"Akun {account_code} tidak ditemukan di {group}")
        before = await self._txs(end=start - timedelta(days=1), unit_usaha_id=unit_usaha_id, pusat_only=unit_usaha_id is None)
        saldo = Decimal("0")
        for tx in before:
            if tx.debit_account_code == account_code:
                saldo += tx.amount if acc.normal_balance == "debit" else -tx.amount
            if tx.credit_account_code == account_code:
                saldo += tx.amount if acc.normal_balance == "kredit" else -tx.amount
        saldo_awal = saldo
        period = await self._txs(start=start, end=end, unit_usaha_id=unit_usaha_id, pusat_only=unit_usaha_id is None)
        entries = []
        total_d = total_c = Decimal("0")
        accounts = {a.code: a for a in await self._accounts(group)}
        for tx in period:
            if tx.debit_account_code != account_code and tx.credit_account_code != account_code:
                continue
            debit = tx.amount if tx.debit_account_code == account_code else Decimal("0")
            credit = tx.amount if tx.credit_account_code == account_code else Decimal("0")
            if acc.normal_balance == "debit":
                saldo += debit - credit
            else:
                saldo += credit - debit
            total_d += debit
            total_c += credit
            other = tx.credit_account_code if tx.debit_account_code == account_code else tx.debit_account_code
            other_acc = accounts.get(other)
            entries.append(
                {
                    "id": tx.id,
                    "date": tx.date.isoformat(),
                    "description": tx.description,
                    "other_account_code": other,
                    "other_account_name": other_acc.name if other_acc else other,
                    "reference": tx.reference,
                    "debit": _f(debit),
                    "credit": _f(credit),
                    "balance": _f(saldo),
                }
            )
        return {
            "account": {
                "code": acc.code,
                "name": acc.name,
                "category": acc.category,
                "normal_balance": acc.normal_balance,
            },
            "saldo_awal": _f(saldo_awal),
            "saldo_akhir": _f(saldo),
            "total_debit": _f(total_d),
            "total_credit": _f(total_c),
            "entries": entries,
        }

    async def per_unit(self, start: date, end: date) -> dict[str, Any]:
        lr_pusat = await self.laba_rugi(start, end, None)
        bumdes = {
            "pendapatan": lr_pusat["total_pendapatan"],
            "beban": lr_pusat["total_beban"],
            "laba_bersih": lr_pusat["laba_bersih"],
            "share_pades_30": round(lr_pusat["laba_bersih"] * 0.30),
            "share_modal_18": round(lr_pusat["laba_bersih"] * 0.18),
            "share_lain": round(lr_pusat["laba_bersih"] * 0.52),
        }
        units = []
        for unit in await self._units():
            lr = await self.laba_rugi(start, end, unit.id)
            units.append(
                {
                    "id": unit.id,
                    "code": unit.code,
                    "name": unit.name,
                    "pendapatan": lr["total_pendapatan"],
                    "beban": lr["total_beban"],
                    "laba_bersih": lr["laba_bersih"],
                    "share_pengelola_30": round(lr["laba_bersih"] * 0.30),
                    "share_bumdes_70": round(lr["laba_bersih"] * 0.70),
                }
            )
        return {"bumdes": bumdes, "units": units}

    async def dashboard(
        self,
        start: Optional[date],
        end: Optional[date],
        granularity: str,
        unit_usaha_id: Optional[str],
        pusat_kpis: bool,
    ) -> dict[str, Any]:
        if unit_usaha_id:
            lr = await self.laba_rugi(start or date(2000, 1, 1), end or date.today(), unit_usaha_id)
            txs = await self._txs(start=start, end=end, unit_usaha_id=unit_usaha_id)
        elif pusat_kpis:
            lr = await self.laba_rugi(start or date(2000, 1, 1), end or date.today(), None)
            txs = await self._txs(start=start, end=end, pusat_only=True)
        else:
            lr = await self.laba_rugi(start or date(2000, 1, 1), end or date.today(), None)
            txs = await self._txs(start=start, end=end)

        bucket_len = 10 if granularity == "day" else 7
        monthly: dict[str, dict[str, float]] = {}
        group = await self._group_for(unit_usaha_id)
        accounts = {a.code: a for a in await self._accounts(group)}
        trend_txs = txs if unit_usaha_id or pusat_kpis else await self._txs(start=start, end=end, pusat_only=True)
        trend_acc = accounts if unit_usaha_id else {a.code: a for a in await self._accounts("BUMDES")}
        for tx in trend_txs:
            key = tx.date.isoformat()[:bucket_len]
            monthly.setdefault(key, {"month": key, "pendapatan": 0.0, "beban": 0.0})
            credit = trend_acc.get(tx.credit_account_code)
            debit = trend_acc.get(tx.debit_account_code)
            if credit and credit.category == "pendapatan":
                monthly[key]["pendapatan"] += _f(tx.amount)
            if debit and debit.category in {"beban", "hpp"}:
                monthly[key]["beban"] += _f(tx.amount)
        series = [monthly[k] for k in sorted(monthly)]
        if not start and not end:
            series = series[-6:]

        unit_summaries = []
        for unit in await self._units():
            if unit_usaha_id and unit.id != unit_usaha_id:
                continue
            u_lr = await self.laba_rugi(start or date(2000, 1, 1), end or date.today(), unit.id)
            unit_summaries.append(
                {
                    "id": unit.id,
                    "code": unit.code,
                    "name": unit.name,
                    "pendapatan": u_lr["total_pendapatan"],
                    "beban": u_lr["total_beban"],
                    "laba": u_lr["laba_bersih"],
                }
            )
        return {
            "total_pendapatan": lr["total_pendapatan"],
            "total_beban": lr["total_beban"],
            "laba_bersih": lr["laba_bersih"],
            "total_transactions": len(txs),
            "monthly": series,
            "unit_summaries": unit_summaries,
        }
