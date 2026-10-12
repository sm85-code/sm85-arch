"""Review replies and boost, based on api-docs endpoint JSON rather than SDK types."""
import json
import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from . import erp_shopee as provider, erp_shopee_management as management

logger = logging.getLogger(__name__)
BASE = '/api/v2/product/'


def safe(value, akun):
    text = str(value or '')
    for secret in (getattr(akun, 'access_token', None), getattr(akun, 'refresh_token', None), provider.SHOPEE_PARTNER_KEY):
        if secret:
            text = text.replace(str(secret), '[redacted]')
    text = re.sub(r'(?i)(access_token|refresh_token|partner_key|sign)([=:"\s]+)[^\s&,"}]+', r'\1\2[redacted]', text)
    return ' '.join(text.split())[:500]


def diagnostic(path, akun, data, failures=None, code=None):
    report = {'diagnostic_id': str(uuid4()), 'time': datetime.now(timezone.utc).isoformat(),
              'endpoint': path, 'account_id': getattr(akun, 'id', None),
              'request_id': safe(data.get('request_id'), akun) or None,
              'error': safe(code or data.get('error'), akun), 'message': safe(data.get('message'), akun),
              'failures': [{k: safe(v, akun) for k, v in row.items() if k in {'item_id', 'comment_id', 'error', 'message'}} for row in failures or []]}
    logger.warning('shopee_engagement_diagnostic %s', json.dumps(report, ensure_ascii=False))
    return report


async def call(session, akun, name, **kwargs):
    path = BASE + name
    try:
        return await provider.signed_shop_request(session, akun, path, **kwargs)
    except provider.ShopeeAPIError as exc:
        data = {'request_id': exc.request_id, 'error': exc.error, 'message': exc.message, 'response': exc.response}
        if name in {'reply_comment', 'boost_item'} and exc.response:
            return data
        report = diagnostic(path, akun, data)
        raise HTTPException(502, {'message': f'Shopee menolak permintaan: {report["error"]} {report["message"]}', 'diagnostic': report}) from None
    except provider.ShopeeNotConfigured as exc:
        report = diagnostic(path, akun, {'message': str(exc)}, code='not_configured')
        raise HTTPException(503, {'message': report['message'], 'diagnostic': report}) from None
    except HTTPException as exc:
        # A transport timeout after POST is uncertain; never silently retry.
        report = diagnostic(path, akun, {'message': safe(exc.detail, akun)}, code='http_' + str(exc.status_code))
        raise HTTPException(exc.status_code, {'message': 'Shopee belum mengonfirmasi hasil. Periksa data sebelum mengirim ulang.', 'diagnostic': report}) from None
    except Exception:
        report = diagnostic(path, akun, {'message': 'Koneksi atau decoding respons gagal; hasil perubahan belum pasti.'}, code='transport_or_decode_error')
        raise HTTPException(502, {'message': report['message'], 'diagnostic': report}) from None



def incomplete(name, akun, data):
    report = diagnostic(BASE + name, akun, data, code='unconfirmed_response')
    raise HTTPException(502, {'message': 'Respons Shopee belum mengonfirmasi hasil lengkap. Muat ulang sebelum mengirim ulang.', 'diagnostic': report})


async def comments(session, akun, item_id, cursor='', page_size=20, comment_id=None):
    params = {'cursor': cursor, 'page_size': page_size}
    if item_id is not None:
        params['item_id'] = int(item_id)
    if comment_id is not None:
        params['comment_id'] = comment_id
    data = await call(session, akun, 'get_comment', params=params)
    result = data.get('response')
    if not isinstance(result, dict) or not isinstance(result.get('item_comment_list'), list) or type(result.get('more')) is not bool or not isinstance(result.get('next_cursor'), str):
        incomplete('get_comment', akun, data)
    rows = result['item_comment_list']
    if any(not isinstance(r, dict) or not isinstance(r.get('comment', ''), str) or not isinstance(r.get('comment_reply') or r.get('cmt_reply') or {}, dict) for r in rows):
        incomplete('get_comment', akun, data)
    if any(not isinstance(row, dict) or (item_id is not None and str(row.get('item_id')) != str(item_id)) or not str(row.get('item_id', '')).isdigit() or not str(row.get('comment_id', '')).isdigit() or int(row['comment_id']) <= 0 for row in rows):
        incomplete('get_comment', akun, data)
    return {'items': [{'comment_id': str(row['comment_id']), 'item_id': str(row['item_id']),
                       'comment': row.get('comment', ''), 'rating_star': row.get('rating_star'),
                       'buyer_username': row.get('buyer_username'), 'create_time': row.get('create_time'),
                       'reply': (row.get('comment_reply') or row.get('cmt_reply') or {}).get('reply', '')} for row in rows],
            'more': result['more'], 'next_cursor': result['next_cursor'], 'request_id': data.get('request_id')}


async def reply(session, akun, item_id, payload, *, before_send=None):
    # Verify every review on the product and authorized shop before sending any reply.
    for row in payload.comment_list:
        found = await comments(session, akun, item_id, page_size=1, comment_id=row.comment_id)
        match = next((r for r in found['items'] if r['comment_id'] == str(row.comment_id)), None)
        if not match:
            raise HTTPException(422, 'Ulasan tidak ditemukan pada produk dan toko ini.')
        if match['reply']:
            raise HTTPException(409, 'Ulasan ini sudah dibalas. Muat ulang sebelum mengirim ulang.')
    if before_send is not None:
        await before_send()
    data = await call(session, akun, 'reply_comment', method='POST', body=payload.model_dump(mode='json'))
    response = data.get('response')
    results = response.get('result_list') if isinstance(response, dict) else None
    expected = {str(row.comment_id) for row in payload.comment_list}
    if not isinstance(results, list) or any(not isinstance(r, dict) for r in results) or len(results) != len(expected) or {str(r.get('comment_id')) for r in results} != expected or not data.get('request_id'):
        incomplete('reply_comment', akun, data)
    failures = [{'comment_id': str(r['comment_id']), 'error': safe(r.get('fail_error'), akun), 'message': safe(r.get('fail_message'), akun)} for r in results if r.get('fail_error') or r.get('fail_message')]
    failed = {r['comment_id'] for r in failures}
    # A top-level rejection with no explicit per-item failures is not success.
    if data.get('error') is None or data.get('error') is None or data.get('error') != '' and not failures:
        incomplete('reply_comment', akun, data)
    return {'ok': not failures, 'success_ids': sorted(expected - failed), 'failures': failures,
            'request_id': data.get('request_id'), 'warnings': [],
            'diagnostic': diagnostic(BASE + 'reply_comment', akun, data, failures) if failures else None}


async def boosted(session, akun):
    data = await call(session, akun, 'get_boosted_list')
    response = data.get('response')
    rows = response.get('item_list') if isinstance(response, dict) else None
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not str(r.get('item_id', '')).isdigit() or type(r.get('cool_down_second')) is not int or r['cool_down_second'] < 0 for r in rows):
        incomplete('get_boosted_list', akun, data)
    return {'items': [{'item_id': str(r['item_id']), 'cool_down_second': r['cool_down_second']} for r in rows], 'request_id': data.get('request_id')}


async def boost(session, akun, item_ids, *, before_send=None):
    item_ids = [str(i) for i in item_ids]
    if not 1 <= len(item_ids) <= 5 or len(set(item_ids)) != len(item_ids):
        raise HTTPException(422, 'Boost memerlukan 1–5 produk berbeda.')
    for item in item_ids:
        await management.item(session, akun, item)
    if before_send is not None:
        await before_send()
    data = await call(session, akun, 'boost_item', method='POST', body={'item_id_list': [int(i) for i in item_ids]})
    response = data.get('response')
    if not isinstance(response, dict):
        incomplete('boost_item', akun, data)
    success = response.get('success_list', {'item_id_list': []})
    if not isinstance(success, dict) or not isinstance(success.get('item_id_list'), list) or not isinstance(response.get('failure_list'), list):
        incomplete('boost_item', akun, data)
    successes = [str(i) for i in success['item_id_list']]
    rows = response['failure_list']
    if any(not isinstance(r, dict) or 'item_id' not in r or not isinstance(r.get('failed_reason'), str) for r in rows):
        incomplete('boost_item', akun, data)
    failures = [{'item_id': str(r['item_id']), 'error': 'boost_rejected', 'message': safe(r['failed_reason'], akun)} for r in rows]
    reported = successes + [r['item_id'] for r in failures]
    if set(reported) != {str(i) for i in item_ids} or len(reported) != len(item_ids) or not data.get('request_id') or data.get('error') is None or data.get('error') != '' and not failures:
        incomplete('boost_item', akun, data)
    return {'ok': not failures, 'success_ids': successes, 'failures': failures, 'request_id': data.get('request_id'), 'warnings': [],
            'diagnostic': diagnostic(BASE + 'boost_item', akun, data, failures) if failures else None}
