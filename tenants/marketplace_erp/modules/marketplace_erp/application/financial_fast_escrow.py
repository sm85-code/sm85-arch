"""BUMI TANI only: actual wallet credits, never a second copy of final escrow totals."""
from datetime import datetime, time, timedelta
from decimal import Decimal
import re

from fastapi import HTTPException

from .financial_reports import WIB, ZERO, money
from .financial_shopee import shipping_day
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


def fast_balances(shop, pending, complete, releases, shipping, not_shipped, events, periods, end, now):
    cutoffs = [min(end, datetime.combine(datetime.fromisoformat(w['akhir']).date() + timedelta(days=1), time.min, WIB)) for w in periods]
    cells = [dict(piutang=ZERO, piutang_lama=ZERO, cair_minggu_lalu=ZERO, dana_cair=ZERO,
                  piutang_tidak_pasti=0, piutang_lama_tidak_pasti=0, cair_tidak_terpetakan=0) for _ in periods]
    by_order, ambiguous, unallocated, batches, resolved = {}, set(), [], [], []
    unmapped, unmapped_events = 0, 0
    first = datetime.fromisoformat(periods[0]['mulai']).date()

    def batch_cohort(orders):
        # Shopee often pays several orders in one movement. The total is known even though the split is not,
        # so it is attributable when every order shipped on a normal day in one weekly cohort.
        cohorts = set()
        for sn in orders:
            stamp = shipping.get(sn)
            if sn in not_shipped:
                return None, 'belum_dikirim'
            if stamp is None:
                return None, 'tanpa_tanggal_kirim'
            if shipping_day(stamp).weekday() == 6:
                return None, 'dikirim_hari_minggu'
            cohorts.add((shipping_day(stamp) - first).days // 7)
        return (cohorts.pop(), None) if len(cohorts) == 1 else (None, 'beda_minggu_kirim')

    diagnosis = []

    def aware(stamp):
        return stamp.replace(tzinfo=WIB) if stamp.tzinfo is None else stamp.astimezone(WIB)

    released = {r['order_sn']: money(r.get('payout') or 0) for r in releases}

    def advance_window(event, days):
        # A Dana Cepat advance is paid at 17:00 WIB for orders whose waybill was confirmed within the 16:00 cutoff
        # window (Shopee pays 50% of the estimated escrow up front). Longer windows cover days without a batch.
        window_end = datetime.combine(event['at'].date(), time(16), WIB)
        window_start = window_end - timedelta(days=days)
        candidates = [sn for sn, stamp in shipping.items() if window_start < aware(stamp) <= window_end and sn not in not_shipped]
        # Pending estimates are what is still owed AFTER the advance (equal to it); released orders paid their final amount.
        expected = sum((money(pending[sn].get('estimated_escrow_amount') or '0') if sn in pending else released.get(sn, ZERO) / 2
                        for sn in candidates), ZERO)
        cohorts = {}
        for sn in candidates:
            key = (shipping_day(shipping[sn]) - first).days // 7
            cohorts[key] = cohorts.get(key, 0) + 1
        fits = bool(candidates) and abs(event['amount'] - expected) <= max(Decimal(500), expected * Decimal('0.05'))
        return dict(dari=window_start, sampai=window_end, candidates=candidates, expected=expected, cohorts=cohorts, fits=fits)

    def advance_match(event):
        # Attribute only when the amount reconciles with the 50% rule AND every candidate order is in one weekly cohort.
        for days in (1, 2, 3):
            window = advance_window(event, days)
            if window['fits'] and len(window['cohorts']) == 1 and not any(
                    shipping_day(shipping[sn]).weekday() == 6 for sn in window['candidates']):
                return next(iter(window['cohorts'])), max(shipping_day(shipping[sn]) for sn in window['candidates'])
        return None

    def explain(event, reason):
        # Read-only aid for admins. Final releases hint at escrow/repayment movements.
        entry = dict(waktu=event['at'].isoformat(), jenis=event['kind'], nominal=str(event['amount']),
                     pesanan=sorted(event['orders']), alasan=reason)
        if event['kind'] == 'advance':
            window = advance_window(event, 1)
            entry['kandidat'] = dict(dari=window['dari'].isoformat(), sampai=window['sampai'].isoformat(), jumlah=len(window['candidates']),
                                     diharapkan=str(window['expected']), cocok=window['fits'], minggu_kirim=window['cohorts'])
        else:
            entry['rilis_terdekat'] = sorted(
                (dict(order_sn=r['order_sn'], payout=str(r.get('payout')), selisih_menit=round((r['dirilis'] - event['at']).total_seconds() / 60))
                 for r in releases if r.get('dirilis')),
                key=lambda r: abs(r['selisih_menit']))[:3]
        diagnosis.append(entry)

    for event in events:
        day = event['at'].date()
        i = (day - datetime.fromisoformat(periods[0]['mulai']).date()).days // 7
        if day.weekday() != 6 and 0 <= i < len(cells) and event['at'] < cutoffs[i]:
            cells[i]['dana_cair'] += event['amount']
        cohort, reason = batch_cohort(event['orders']) if len(event['orders']) > 1 else (None, 'tanpa_pesanan')
        newest = max((shipping_day(shipping[sn]) for sn in event['orders']), default=None) if cohort is not None else None
        if not event['orders'] and event['kind'] == 'advance' and (matched := advance_match(event)):
            cohort, newest = matched
        if cohort is not None:
            resolved.append(dict(event, cohort=cohort, shipped=newest))
            if 0 <= i < len(cells) and day.weekday() != 6 and cohort == i-1 and event['at'] < cutoffs[i]:
                cells[i]['cair_minggu_lalu'] += event['amount']
            continue
        if len(event['orders']) != 1:
            explain(event, reason)
            # A batch repayment has no per-order allocation in this API. Count its cash once,
            # but do not divide it equally across shipping cohorts or pretend it was zero.
            ambiguous.update(event['orders'])
            batches.append(event)
            unmapped_events += 1
            if event['kind'] == 'repayment':
                unallocated.append(event)
            if 0 <= i < len(cells) and event['at'] < cutoffs[i] and day.weekday() != 6:
                cells[i]['cair_tidak_terpetakan'] += 1
            continue
        sn = next(iter(event['orders']))
        by_order.setdefault(sn, []).append(event)
        stamp = shipping.get(sn)
        if stamp is None:
            continue
        shipped = shipping_day(stamp)
        cohort = (shipped - datetime.fromisoformat(periods[0]['mulai']).date()).days // 7
        if (day.weekday() != 6 and shipped.weekday() != 6 and 0 <= i < len(cells)
                and cohort == i-1 and event['at'] < cutoffs[i]):
            cells[i]['cair_minggu_lalu'] += event['amount']
    paid = {r['order_sn'] for r in releases}
    for sn in pending.keys() | paid | by_order.keys() | ambiguous:
        if sn in not_shipped:
            continue
        stamp = shipping.get(sn)
        if stamp is None:
            unmapped += 1
            continue
        stamp = stamp.replace(tzinfo=WIB) if stamp.tzinfo is None else stamp.astimezone(WIB)
        if stamp.weekday() == 6:
            continue
        cohort = (stamp.date() - datetime.fromisoformat(periods[0]['mulai']).date()).days // 7
        # Shopee describes estimated_escrow_amount as the amount STILL pending.
        # Never subtract advances from that amount a second time.
        estimate = pending[sn].get('estimated_escrow_amount') if sn in pending else '0'
        receipts = [e for e in by_order.get(sn, []) if e['kind'] not in ('refund', 'negative')]
        for i, cutoff in enumerate(cutoffs):
            if stamp >= cutoff or cohort > i:
                continue
            key = 'piutang' if cohort == i else 'piutang_lama'
            if estimate is None or any(e['at'] >= cutoff and sn in e['orders'] for e in batches) or any(e['at'] >= cutoff and not e['orders'] for e in unallocated):
                cells[i][key + '_tidak_pasti'] += 1
                continue
            outstanding = money(estimate) + sum((e['amount'] for e in receipts if e['at'] >= cutoff), ZERO)
            cells[i][key] += max(ZERO, outstanding)
    for i, cutoff in enumerate(cutoffs):
        for event in resolved:
            if event['kind'] in ('refund', 'negative') or event['at'] < cutoff or event['cohort'] > i:
                continue
            if datetime.combine(event['shipped'], time.min, WIB) >= cutoff:
                continue
            key = 'piutang' if event['cohort'] == i else 'piutang_lama'
            cells[i][key] = max(ZERO, cells[i][key] + event['amount'])
    for i, cell in enumerate(cells):
        for event in batches:
            if not event['orders'] and event['kind'] not in ('refund', 'negative') and event['at'] >= cutoffs[i]:
                cell['piutang_tidak_pasti'] += 1
                cell['piutang_lama_tidak_pasti'] += 1
        available = datetime.combine(datetime.fromisoformat(periods[i]['mulai']).date(), time.min, WIB) < end
        for key in ('piutang', 'piutang_lama', 'cair_minggu_lalu', 'dana_cair'):
            unknown = cell.get({'piutang': 'piutang_tidak_pasti', 'piutang_lama': 'piutang_lama_tidak_pasti',
                                'cair_minggu_lalu': 'cair_tidak_terpetakan'}.get(key, ''), 0)
            uncertain = key != 'dana_cair' and cell[key] == ZERO and (unknown or unmapped or (key != 'cair_minggu_lalu' and not complete))
            cell[key] = str(cell[key]) if available and not uncertain else None
    return dict(akun_id=shop.id, nama=shop.nama_toko, minggu=cells, halaman_lengkap=complete,
                tanpa_tanggal_kirim=unmapped, mutasi_tidak_terpetakan=unmapped_events, mutasi_diagnosa=diagnosis[:60], sumber='shopee_wallet_dana_cepat', dana_cepat=True, dibaca_at=now.isoformat())
