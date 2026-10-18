"""BUMI TANI only: actual wallet credits, never a second copy of final escrow totals."""
from datetime import datetime, time, timedelta
from decimal import Decimal
import re

from fastapi import HTTPException

from .financial_reports import WIB, ZERO, money
from ..infrastructure.adapters import erp_shopee_workflows as provider

KINDS = {
    '452': 'advance', 'FAST_ESCROW_DISBURSE': 'advance',
    '459': 'remainder', 'FAST_ESCROW_DISBURSE_REMAIN': 'remainder',
    '458': 'refund', 'FAST_ESCROW_DEDUCT': 'refund',
    '101': 'escrow', 'ESCROW_VERIFIED_ADD': 'escrow',
    '102': 'negative', 'ESCROW_VERIFIED_MINUS': 'negative',
}


async def wallet_income(session, shop, start, now):
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


def dana_cepat_cells(events, periods, end, start):
    """Dana Cepat is a loan, not income: cash in the wallet, a payable until repaid, and a 0.5% fee.

    Receivables stay gross (estimate until the final escrow release) and are NOT touched by advances. Per week:
    dana_cair   wallet cash received in the week (advances + releases - repayments), as shown by the wallet;
    utang       total owed to Shopee at the Saturday cutoff = advances grossed up for the fee - repayments - deductions;
    biaya       fee on the advances of the week (estimated at 0.5%).
    """
    cutoffs = [min(end, datetime.combine(datetime.fromisoformat(w['akhir']).date() + timedelta(days=1), time.min, WIB)) for w in periods]
    first = datetime.fromisoformat(periods[0]['mulai']).date()
    cells = []
    for i, period in enumerate(periods):
        available = datetime.combine(datetime.fromisoformat(period['mulai']).date(), time.min, WIB) < end
        cash, owed, fee = ZERO, ZERO, ZERO
        for event in events:
            if event['at'] < cutoffs[i]:
                if event['kind'] in ('advance', 'remainder'):
                    owed += event['amount'] / (1 - FEE_RATE)
                elif event['kind'] in ('repayment', 'refund'):
                    owed += event['amount']  # Already negative.
            day = event['at'].date()
            if event['at'] >= start and day.weekday() != 6 and (day - first).days // 7 == i and event['at'] < cutoffs[i]:
                cash += event['amount']
                if event['kind'] in ('advance', 'remainder'):
                    fee += event['amount'] * FEE_RATE / (1 - FEE_RATE)
        cells.append(dict(dana_cair=str(money(cash)) if available else None,
                          utang_dana_cepat=str(money(max(ZERO, owed))) if available else None,
                          biaya_dana_cepat=str(money(fee)) if available else None))
    return cells
