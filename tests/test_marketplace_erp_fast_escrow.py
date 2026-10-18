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


def cells(events, start=stamp(1, 0)):
    return fast.dana_cepat_cells(events, weekly_periods('2026-10', 4)[1], stamp(14, 0), start)


def test_advance_is_cash_and_a_payable_with_a_half_percent_fee_never_income():
    first, second = cells([event(3, '99.5', 'advance')])[:2]
    assert first['dana_cair'] == '99.50'
    assert first['utang_dana_cepat'] == '100.00'  # 99.5 grossed up for the 0.5% fee.
    assert first['biaya_dana_cepat'] == '0.50'
    assert second['utang_dana_cepat'] == '100.00' and second['biaya_dana_cepat'] == '0.00'


def test_release_and_repayment_clear_the_payable():
    first, second = cells([event(3, '99.5', 'advance'), event(6, '100', 'escrow'), event(6, '-100', 'repayment')])[:2]
    assert first['utang_dana_cepat'] == '100.00'
    assert second['utang_dana_cepat'] == '0.00'
    assert second['dana_cair'] == '0.00'  # +100 escrow, -100 repayment.


def test_return_deduction_reduces_the_payable_and_loans_before_the_range_count():
    result = cells([event(1, '199', 'advance'), event(8, '-100', 'refund')], start=stamp(2, 0))
    assert result[0]['utang_dana_cepat'] == '200.00'  # The 1 Okt advance predates the range but is still owed.
    assert result[0]['dana_cair'] == '0.00'  # ...and is not cash of this range.
    assert result[1]['utang_dana_cepat'] == '100.00'


def test_future_weeks_have_no_figures():
    assert cells([event(3, '99.5', 'advance')])[3] == dict(dana_cair=None, utang_dana_cepat=None, biaya_dana_cepat=None)


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
