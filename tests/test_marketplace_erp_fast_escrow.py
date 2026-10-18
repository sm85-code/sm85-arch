"""Fast escrow cash is wallet movement, never escrow totals plus the same advance."""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tenants.marketplace_erp.modules.marketplace_erp.application import financial_fast_escrow as fast
from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_periods


def stamp(day, hour=12):
    return datetime(2026, 10, day, hour, tzinfo=fast.WIB)


def event(day, value, kind, orders=('A',)):
    return dict(at=stamp(day), amount=fast.money(value), kind=kind, orders=set(orders))


def balances(events, pending=None, releases=None, shipping=None):
    return fast.fast_balances(SimpleNamespace(id='bumi', nama_toko='BUMI TANI IND'), pending or {}, True,
        releases or [], shipping or {'A': datetime(2026, 9, 30, 12, tzinfo=fast.WIB)}, set(), events,
        weekly_periods('2026-10', 4)[1], stamp(14, 0), stamp(16))


def test_advance_remainder_and_return_do_not_double_count_final_settlement():
    result = balances([event(3, '80', 'advance'), event(6, '20', 'remainder'), event(8, '-10', 'refund')],
                      releases=[{'order_sn': 'A', 'payout': '100'}])
    first, second, third, future = result['minggu']
    assert first['dana_cair'] == '80.00'
    assert first['piutang'] == '20.00'  # Only the unpaid remainder at Saturday cutoff.
    assert second['dana_cair'] == '10.00'
    assert second['cair_minggu_lalu'] == '10.00'  # 20 released minus 10 refunded.
    assert fast.money(second['piutang_lama']) == 0
    assert fast.money(third['piutang_lama']) == 0
    assert future['dana_cair'] is None


def test_pending_estimate_already_remaining_is_not_reduced_twice():
    result = balances([event(6, '80', 'advance')], pending={'A': {'estimated_escrow_amount': '20'}},
                      shipping={'A': stamp(5)})
    assert result['minggu'][1]['piutang'] == '20.00'
    assert result['minggu'][1]['dana_cair'] == '80.00'
    assert result['minggu'][2]['piutang_lama'] == '20.00'


def test_actual_settlement_credit_and_repayment_are_netted_not_added_to_escrow_total():
    result = balances([event(3, '80', 'advance'), event(6, '100', 'escrow'), event(6, '-80', 'repayment')],
                      releases=[{'order_sn': 'A', 'payout': '100'}])
    assert result['minggu'][0]['piutang'] == '20.00'
    assert result['minggu'][1]['dana_cair'] == '20.00'
    assert fast.money(result['minggu'][1]['piutang_lama']) == 0


def test_unallocated_batch_repayment_is_not_split_evenly_or_counted_twice():
    result = balances([event(6, '100', 'escrow'), event(6, '-80', 'repayment', ('A', 'B'))])
    assert result['minggu'][1]['dana_cair'] == '20.00'
    assert result['minggu'][0]['piutang'] is None
    assert result['minggu'][0]['piutang_tidak_pasti'] == 1
    assert result['mutasi_tidak_terpetakan'] == 1


def test_multi_order_movement_in_one_cohort_is_attributed_not_unmapped():
    shipping = {'A': stamp(1), 'B': stamp(2)}
    result = balances([event(3, '80', 'advance', ('A', 'B')), event(6, '120', 'escrow', ('A', 'B'))],
                      pending={'A': {'estimated_escrow_amount': '90'}, 'B': {'estimated_escrow_amount': '110'}},
                      shipping=shipping)
    first, second = result['minggu'][:2]
    assert result['mutasi_tidak_terpetakan'] == 0
    assert first['piutang'] == '320.00'  # 200 still pending + 120 released after the Saturday cutoff; the 80 advance predates it.
    assert second['cair_minggu_lalu'] == '120.00'
    assert second['piutang_tidak_pasti'] == 0


def test_unmapped_movements_are_explained_with_nearest_releases():
    release = {'order_sn': 'R1', 'payout': '75', 'dirilis': stamp(6, 11)}
    result = fast.fast_balances(SimpleNamespace(id='bumi', nama_toko='BUMI TANI IND'), {}, True, [release],
        {'A': stamp(1)}, set(), [event(6, '75', 'escrow', ()), event(6, '-80', 'repayment', ('A', 'B'))],
        weekly_periods('2026-10', 4)[1], stamp(14, 0), stamp(16))
    reasons = {d['alasan'] for d in result['mutasi_diagnosa']}
    assert reasons == {'tanpa_pesanan', 'tanpa_tanggal_kirim'}
    assert result['mutasi_diagnosa'][0]['rilis_terdekat'] == [dict(order_sn='R1', payout='75', selisih_menit=-60)]


def test_advance_diagnosis_lists_orders_confirmed_in_the_16h_window():
    result = balances([event(3, '30', 'advance', ()), event(6, '45', 'advance', ())],
                      pending={'A': {'estimated_escrow_amount': '70'}, 'B': {'estimated_escrow_amount': '55'}},
                      shipping={'A': stamp(5, 17), 'B': stamp(2)})
    last = result['mutasi_diagnosa'][-1]['kandidat']
    assert last['jumlah'] == 1 and last['estimasi_pending'] == '70.00' and last['rasio'] == '0.391'


@pytest.mark.asyncio
async def test_wallet_windows_offsets_statuses_and_boundaries(monkeypatch):
    def row(day, kind, amount, status='COMPLETED'):
        return dict(create_time=int(stamp(day).timestamp()), transaction_type=kind, amount=amount,
                    status=status, order_sn='A', money_flow=None, current_balance='100')
    provider = AsyncMock(side_effect=[
        {'items': [row(1, '452', '80'), row(2, '201', '50')], 'ada_lagi': True, 'next_offset': 2},
        {'items': [row(2, '459', '20', 'PENDING')], 'ada_lagi': False, 'next_offset': 3},
        # A provider returning a boundary record again must not count it in the wrong window.
        {'items': [row(1, '452', '80'), row(15, '458', '10')], 'ada_lagi': False, 'next_offset': 2},
    ])
    monkeypatch.setattr(fast.provider, 'wallet', provider)
    events = await fast.wallet_income(None, SimpleNamespace(), stamp(1, 0), stamp(16))
    assert [(e['kind'], str(e['amount'])) for e in events] == [('advance', '80.00'), ('refund', '-10.00')]
    assert provider.call_args_list[1].args[-1] == 2
    assert provider.call_args_list[2].args[2:4] == (date(2026, 10, 15), date(2026, 10, 16))
