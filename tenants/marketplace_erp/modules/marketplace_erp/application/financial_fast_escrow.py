"""BUMI TANI only: actual wallet credits, never a second copy of final escrow totals."""
from datetime import datetime, time, timedelta
from decimal import Decimal
import os
import re
from unicodedata import normalize

from fastapi import HTTPException

from .financial_reports import WIB, ZERO, money
from ..infrastructure.adapters import erp_shopee_workflows as provider

def _key(name):
    return " ".join(normalize("NFKC", name or "").casefold().split())


def dana_cepat_shops():
    """Shops enrolled in Shopee Dana Cepat. Configurable (comma separated) so a renamed or newly enrolled shop does not
    need a code change; the default keeps the current shop."""
    raw = os.getenv("SHOPEE_DANA_CEPAT_TOKO", "BUMI TANI IND")
    return {_key(n) for n in raw.split(",") if n.strip()}


def pakai_dana_cepat(shop):
    return _key(getattr(shop, "nama_toko", "")) in dana_cepat_shops()


KINDS = {
    '452': 'advance', 'FAST_ESCROW_DISBURSE': 'advance',
    '459': 'remainder', 'FAST_ESCROW_DISBURSE_REMAIN': 'remainder',
    '458': 'refund', 'FAST_ESCROW_DEDUCT': 'refund',
    '101': 'escrow', 'ESCROW_VERIFIED_ADD': 'escrow',
    '102': 'negative', 'ESCROW_VERIFIED_MINUS': 'negative',
}


async def wallet_income(session, shop, start, now, ignored=None):
    """`ignored`, when given, counts the movements that are not Dana Cepat by "type/tab", so a missing kind can be spotted."""
    events, seen_events = [], set()
    day = start.date()
    pages = 0
    while day <= now.date():
        last = min(day + timedelta(days=13), now.date())
        offset, seen_pages = 0, set()
        while True:
            page = await provider.wallet(session, shop, day, last, offset, page_size=100)
            pages += 1
            signature = repr(page['items'])
            if signature in seen_pages or pages > 100:
                raise HTTPException(424, 'Halaman mutasi Dana Cepat belum lengkap atau berulang')
            seen_pages.add(signature)
            for row in page['items']:
                try:
                    stamp = datetime.fromtimestamp(row['create_time'], WIB)
                except (ValueError, OverflowError, OSError) as exc:
                    raise HTTPException(424, 'Waktu mutasi Dana Cepat tidak valid') from exc
                if not day <= stamp.date() <= last or not start <= stamp < now or row['status'] != 'COMPLETED':
                    continue
                kind = KINDS.get(str(row['transaction_type']).strip().upper())
                if row.get('transaction_tab_type') == 'fast_escrow_repayment' and kind not in ('refund', 'negative'):
                    kind = 'repayment'
                if kind is None:
                    if ignored is not None:
                        label = f"{row['transaction_type']}/{row.get('transaction_tab_type') or '-'}"
                        ignored[label] = ignored.get(label, 0) + 1
                    continue  # Withdrawals, ads and unrelated adjustments do not pay an order.
                amount = money(row['amount'])
                flow = row.get('money_flow')
                negative = kind in ('refund', 'negative', 'repayment')
                if flow and flow != ('MONEY_OUT' if negative else 'MONEY_IN'):
                    raise HTTPException(424, 'Arah mutasi Dana Cepat tidak sesuai jenis transaksi')
                if not negative and amount < ZERO:
                    raise HTTPException(424, 'Nominal penerimaan Dana Cepat bernilai negatif')
                amount = -abs(amount) if negative else amount
                numbers = {r['order_sn'] for r in row.get('pay_order_list', []) if r.get('order_sn')}
                if row.get('order_sn'):
                    numbers.add(row['order_sn'])
                if any(not isinstance(sn, str) or not re.fullmatch(r'[A-Za-z0-9]{1,50}', sn) for sn in numbers):
                    raise HTTPException(424, 'Kode pesanan mutasi Dana Cepat tidak valid')
                identity = (row['create_time'], kind, amount, tuple(sorted(numbers)), row.get('current_balance'), flow)
                if identity in seen_events:
                    raise HTTPException(424, 'Mutasi Dana Cepat berulang; nominal tidak dijumlahkan dua kali')
                seen_events.add(identity)
                events.append(dict(at=stamp, amount=amount, kind=kind, orders=numbers))
            if not page['ada_lagi']:
                break
            if not page['items'] or page['next_offset'] <= offset:
                raise HTTPException(424, 'Halaman mutasi Dana Cepat tidak maju')
            offset = page['next_offset']
        day = last + timedelta(days=1)  # Disjoint inclusive windows; no duplicate boundary credits.
    return events


FEE_RATE = Decimal('0.005')  # Shopee's disbursement fee: 0.5% of the eligible amount, deducted from each advance.
LOOKBACK_DAYS = 28  # Advances have a 14-day tenor; reading two tenors back recovers the loan open at the first cutoff.


def _open_loans(events):
    """Loans still open after these movements, matched order by order.

    An advance (grossed up for the fee) opens a loan on its order; repayments and deductions close it. A deduction whose
    advance was not read (it predates the movements fetched) says nothing about the loans we can see, so it is set
    aside instead of cancelling other loans. Returns (owed, owed before clamping each loan at zero, set-aside amount).
    """
    loans, paid, orphan = {}, {}, ZERO
    for n, event in enumerate(events):
        if event['kind'] in ('advance', 'remainder'):
            orders = sorted(event['orders']) or [('?', n)]
            for order in orders:
                loans[order] = loans.get(order, ZERO) + event['amount'] / (1 - FEE_RATE) / len(orders)
    for event in events:
        if event['kind'] not in ('repayment', 'refund'):
            continue
        matched = [o for o in event['orders'] if o in loans]
        if not matched:
            orphan += abs(event['amount'])
            continue
        weight = sum(loans[o] for o in matched)
        for order in matched:
            paid[order] = paid.get(order, ZERO) + abs(event['amount']) * (loans[order] / weight if weight else Decimal(1) / len(matched))
    raw = sum((loans[o] - paid.get(o, ZERO) for o in loans), ZERO)
    owed = sum((max(ZERO, loans[o] - paid.get(o, ZERO)) for o in loans), ZERO)
    return owed, raw, orphan


def dana_cepat_cells(events, periods, end, start):
    """Dana Cepat is a loan, not income: cash in the wallet, a payable until repaid, and a 0.5% fee.

    Receivables stay gross (estimate until the final escrow release) and are NOT touched by advances. Per week:
    dana_cair   wallet cash received in the week (advances + releases - repayments), as shown by the wallet;
    utang       total owed to Shopee at the Saturday cutoff = open loans, order by order (advance grossed up for the fee
                minus its repayments and deductions); deductions of advances that were not read are set aside;
    biaya       fee on the advances of the week (estimated at 0.5%).
    """
    cutoffs = [min(end, datetime.combine(datetime.fromisoformat(w['akhir']).date() + timedelta(days=1), time.min, WIB)) for w in periods]
    first = datetime.fromisoformat(periods[0]['mulai']).date()
    cells = []
    for i, period in enumerate(periods):
        available = datetime.combine(datetime.fromisoformat(period['mulai']).date(), time.min, WIB) < end
        cash, fee = ZERO, ZERO
        parts = dict(pencairan=ZERO, pencairan_lanjutan=ZERO, pelunasan=ZERO, retur=ZERO)
        earliest = None
        before = [e for e in events if e['at'] < cutoffs[i]]
        owed, raw, orphan = _open_loans(before)
        for event in events:
            if event['at'] < cutoffs[i]:
                part = {'advance': 'pencairan', 'remainder': 'pencairan_lanjutan', 'repayment': 'pelunasan', 'refund': 'retur'}.get(event['kind'])
                if part:
                    parts[part] += abs(event['amount'])
                    earliest = min(earliest or event['at'], event['at'])
            day = event['at'].date()
            if event['at'] >= start and day.weekday() != 6 and (day - first).days // 7 == i and event['at'] < cutoffs[i]:
                cash += event['amount']
                if event['kind'] in ('advance', 'remainder'):
                    fee += event['amount'] * FEE_RATE / (1 - FEE_RATE)
        cells.append(dict(dana_cair=str(money(cash)) if available else None,
                          utang_dana_cepat=str(money(max(ZERO, owed))) if available else None,
                          biaya_dana_cepat=str(money(fee)) if available else None,
                          rincian_dana_cepat=dict(
                              {k: str(money(v)) for k, v in parts.items()},
                              utang_sebelum_nol=str(money(raw)),
                              tanpa_pencairan=str(money(orphan)),
                              paling_awal=earliest.date().isoformat() if earliest else None,
                          ) if available else None))
    return cells


async def advanced_orders(session, shop, now=None):
    """Orders that received a Dana Cepat advance (wallet `advance` credits) in the last tenors, with the amount
    already paid out early. Read-only; empty for shops not enrolled."""
    if not pakai_dana_cepat(shop):
        return {"aktif": False, "pesanan": {}}
    now = now or datetime.now(WIB)
    events = await wallet_income(session, shop, now - timedelta(days=LOOKBACK_DAYS), now)
    paid = {}
    for event in events:
        if event["kind"] == "advance":
            for sn in event["orders"]:
                paid[sn] = paid.get(sn, ZERO) + event["amount"] / max(1, len(event["orders"]))
    return {"aktif": True, "pesanan": {sn: str(v) for sn, v in sorted(paid.items())}}
