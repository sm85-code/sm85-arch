"""Official engagement samples, scoping, partial results and safe diagnostics."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_engagement as adapter
from tenants.marketplace_erp.modules.marketplace_erp.application.commerce_schemas import ReplyComments, BoostProducts
from tenants.marketplace_erp.adapters.api.v1 import commerce_router as routes
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import extra_tools, tools

ACCOUNT = SimpleNamespace(id='shop-1', access_token='SECRET-ACCESS', refresh_token='SECRET-REFRESH')


def sample(name):
    return json.loads((Path(__file__).parent / 'fixtures' / f'shopee_{name}.json').read_text())


def comment_data(ident=1540927):
    return {'error': '', 'request_id': 'read-1', 'response': {'item_comment_list': [{'item_id': 16509872, 'comment_id': ident, 'comment': 'Baik'}], 'more': False, 'next_cursor': ''}}


def test_batch_limits_and_duplicate_ids():
    with pytest.raises(ValidationError):
        ReplyComments(comment_list=[])
    with pytest.raises(ValidationError):
        ReplyComments(comment_list=[{'comment_id': 1, 'comment': ' '}])
    with pytest.raises(ValidationError):
        ReplyComments(comment_list=[{'comment_id': 1, 'comment': 'X'}] * 2)
    with pytest.raises(ValidationError):
        ReplyComments(comment_list=[{'comment_id': i + 1, 'comment': 'X'} for i in range(101)])
    with pytest.raises(ValidationError):
        BoostProducts(katalog_ids=['x'] * 2)
    with pytest.raises(ValidationError):
        BoostProducts(katalog_ids=[str(i) for i in range(6)])


@pytest.mark.asyncio
async def test_comment_read_uses_cursor_and_accepts_official_legacy_reply_key(monkeypatch):
    request = AsyncMock(return_value=sample('get_comment'))
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', request)
    result = await adapter.comments(None, ACCOUNT, 16509872, '20', 10)
    assert result['items'][0]['reply'].startswith('Thank you')
    assert result['items'][0]['comment_id'] == '120590834' and result['next_cursor'] == '20'
    assert request.call_args.kwargs == {'params': {'item_id': 16509872, 'cursor': '20', 'page_size': 10}}


@pytest.mark.asyncio
async def test_reply_sends_official_batch_body_after_ownership_read(monkeypatch):
    request = AsyncMock(side_effect=[comment_data(), sample('reply_comment')])
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', request)
    payload = ReplyComments(comment_list=[{'comment_id': 1540927, 'comment': 'Terima kasih'}])
    result = await adapter.reply(None, ACCOUNT, 16509872, payload)
    assert result['ok'] and result['success_ids'] == ['1540927']
    assert request.call_args_list[0].kwargs['params']['comment_id'] == 1540927
    assert request.call_args.kwargs == {'method': 'POST', 'body': {'comment_list': [{'comment_id': 1540927, 'comment': 'Terima kasih'}]}}


@pytest.mark.asyncio
async def test_foreign_comment_is_never_replied(monkeypatch):
    request = AsyncMock(return_value=comment_data(999))
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', request)
    with pytest.raises(HTTPException) as error:
        await adapter.reply(None, ACCOUNT, 16509872, ReplyComments(comment_list=[{'comment_id': 1, 'comment': 'X'}]))
    assert error.value.status_code == 422 and request.await_count == 1


@pytest.mark.asyncio
async def test_boost_partial_sample_and_safe_diagnostics(monkeypatch, caplog):
    data = sample('boost_item')
    data['response']['failure_list'][0]['failed_reason'] = 'access_token=SECRET-ACCESS sign=SECRET-SIGN SECRET-REFRESH'
    request = AsyncMock(return_value=data)
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', request)
    monkeypatch.setattr(adapter.management, 'item', AsyncMock())
    ids = [2300069665, 2400143710, 2700128119, 3200139749, 3400138725]
    result = await adapter.boost(None, ACCOUNT, ids)
    assert not result['ok'] and result['success_ids'] == ['3400138725'] and len(result['failures']) == 4
    assert request.call_args.kwargs['body'] == {'item_id_list': ids}
    assert result['diagnostic']['request_id'] == data['request_id']
    assert all(secret not in json.dumps(result) + caplog.text for secret in ['SECRET-ACCESS', 'SECRET-REFRESH', 'SECRET-SIGN'])


@pytest.mark.asyncio
async def test_boosted_list_sample_cooldown(monkeypatch):
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', AsyncMock(return_value=sample('get_boosted_list')))
    result = await adapter.boosted(None, ACCOUNT)
    assert result['items'][0] == {'item_id': '3200139749', 'cool_down_second': 59}


@pytest.mark.asyncio
async def test_top_level_error_keeps_partial_item_results(monkeypatch):
    data = sample('boost_item')
    exception = adapter.provider.ShopeeAPIError(adapter.BASE + 'boost_item', 'error_boost_item_failed', 'Some failed', 'trace', response=data['response'])
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', AsyncMock(side_effect=exception))
    monkeypatch.setattr(adapter.management, 'item', AsyncMock())
    result = await adapter.boost(None, ACCOUNT, [2300069665, 2400143710, 2700128119, 3200139749, 3400138725])
    assert result['success_ids'] == ['3400138725'] and result['diagnostic']['error'] == 'error_boost_item_failed'


@pytest.mark.asyncio
async def test_reply_per_item_failure_is_not_success(monkeypatch):
    data = sample('reply_comment')
    data['response']['result_list'][0].update(fail_error='product.duplicate_request', fail_message='Already replied')
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', AsyncMock(side_effect=[comment_data(), data]))
    result = await adapter.reply(None, ACCOUNT, 16509872, ReplyComments(comment_list=[{'comment_id': 1540927, 'comment': 'X'}]))
    assert not result['ok'] and not result['success_ids'] and result['failures'][0]['error'] == 'product.duplicate_request'


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [None, [], {'success_list': [], 'failure_list': []}, {'success_list': {'item_id_list': [7]}, 'failure_list': []}])
async def test_unconfirmed_boost_is_not_marked_success(monkeypatch, response):
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', AsyncMock(return_value={'error': '', 'request_id': 'trace', 'response': response}))
    monkeypatch.setattr(adapter.management, 'item', AsyncMock())
    with pytest.raises(HTTPException) as error:
        await adapter.boost(None, ACCOUNT, [8])
    assert error.value.detail['diagnostic']['error'] == 'unconfirmed_response'


@pytest.mark.asyncio
async def test_provider_error_and_timeout_are_copyable_without_retry(monkeypatch):
    for exception in [adapter.provider.ShopeeAPIError('/api/v2/product/get_comment', 'common.error_auth', 'Bad SECRET-ACCESS', 'trace'), HTTPException(504, 'Timeout SECRET-ACCESS')]:
        request = AsyncMock(side_effect=exception)
        monkeypatch.setattr(adapter.provider, 'signed_shop_request', request)
        with pytest.raises(HTTPException) as error:
            await adapter.comments(None, ACCOUNT, 7)
        assert 'SECRET-ACCESS' not in json.dumps(error.value.detail)
        assert error.value.detail['diagnostic']['endpoint'].endswith('get_comment')
        assert request.await_count == 1


@pytest.mark.asyncio
async def test_batch_route_rejects_other_shop_before_provider_write(monkeypatch):
    monkeypatch.setattr(routes, 'account', AsyncMock(return_value=ACCOUNT))
    monkeypatch.setattr(routes.services, 'get_katalog_shopee', AsyncMock(return_value=(SimpleNamespace(akun_id='foreign', item_id='7'), None)))
    request = AsyncMock()
    monkeypatch.setattr(routes.engagement, 'boost', request)
    with pytest.raises(HTTPException) as error:
        await routes.boost_products('shop-1', BoostProducts(katalog_ids=['product']), None, None)
    assert error.value.status_code == 403
    request.assert_not_awaited()


def test_agent_definitions_mark_mutations_as_confirmation_required():
    assert extra_tools.TOOLS['balas_ulasan_produk'][1] and extra_tools.TOOLS['boost_produk'][1]
    assert not extra_tools.TOOLS['baca_ulasan_produk'][1] and not extra_tools.TOOLS['daftar_produk_boost'][1]
    assert 'boost_produk' not in {d['name'] for d in tools.definitions(False)}
    assert 'boost_produk' in {d['name'] for d in tools.definitions(True, 'iklan')}


def test_large_review_ids_remain_exact_and_booleans_are_rejected():
    payload = ReplyComments(comment_list=[{'comment_id': '9007199254740993', 'comment': 'X'}])
    assert payload.comment_list[0].comment_id == 9007199254740993
    for value in [True, 1.0]:
        with pytest.raises(ValidationError):
            ReplyComments(comment_list=[{'comment_id': value, 'comment': 'X'}])


@pytest.mark.asyncio
async def test_complete_all_failed_response_without_success_list(monkeypatch):
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', AsyncMock(return_value={'error': 'error_boost_item_all_failed', 'request_id': 'trace', 'response': {'failure_list': [{'item_id': 7, 'failed_reason': 'Cooldown'}]}}))
    monkeypatch.setattr(adapter.management, 'item', AsyncMock())
    result = await adapter.boost(None, ACCOUNT, [7])
    assert not result['ok'] and not result['success_ids'] and result['failures'][0]['item_id'] == '7'


@pytest.mark.asyncio
async def test_decode_error_has_safe_diagnostic_not_raw_exception(monkeypatch):
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', AsyncMock(side_effect=ValueError('PRIVATE raw response SECRET-ACCESS')))
    with pytest.raises(HTTPException) as error:
        await adapter.comments(None, ACCOUNT, 7)
    assert error.value.detail['diagnostic']['error'] == 'transport_or_decode_error'
    assert 'PRIVATE' not in json.dumps(error.value.detail)
