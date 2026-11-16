"""Read-only EasyBoss escrow projection via the existing Business Tools vault relay."""
import asyncio
import os
import time
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import HTTPException

_lock = asyncio.Lock()
_cache = None
STATUS = {
    'IN_TRANSIT': 'Dalam pengiriman', 'SHIPPED': 'Dalam pengiriman',
    'DELIVERED': 'Diterima', 'COMPLETED': 'Selesai', 'delivered': 'Diterima',
    'shipped': 'Dalam pengiriman', 'AWAITING_COLLECTION': 'Menunggu kurir',
    'AWAITING_SHIPMENT': 'Menunggu dikirim', 'pending': 'Menunggu diproses',
    'packed': 'Siap dikirim', 'ready_to_ship': 'Siap dikirim',
}
ELIGIBLE = {'IN_TRANSIT', 'SHIPPED', 'DELIVERED', 'COMPLETED', 'delivered', 'shipped'}


def projection(sources, platform='', toko='', status='', search='', page=1, page_size=25):
    # Conflicting split-package amounts cannot safely be added or arbitrarily picked.
    unique = {}
    conflicts = set()
    for source in sources:
        for row in source.get('pesanan', []):
            if row.get('platform') not in ('tiktok', 'lazada') or not row.get('no_pesanan'):
                continue
            key = (row['platform'], str(row.get('shopId')), row['no_pesanan'])
            if key in unique:
                previous = unique[key]
                if previous.get('perkiraan_dana_cair') != row.get('perkiraan_dana_cair') or previous.get('sudah_cair') != row.get('sudah_cair'):
                    conflicts.add(key)
            else:
                unique[key] = dict(row)
    rows = []
    uncertain = []
    for key, row in unique.items():
        code = row.get('status_platform') or ''
        if row.get('sudah_cair'):
            continue
        if code not in ELIGIBLE:
            if not any(word in code.lower() for word in ('cancel', 'return', 'shipped_back', 'awaiting', 'pending', 'packed', 'ready_to_ship', 'unpaid')):
                uncertain.append(row)
            continue
        row['key'] = ':'.join(key)
        row['status_label'] = STATUS.get(code, code or 'Belum diketahui')
        row['status_pencairan'] = 'Belum tercatat cair di EasyBoss'
        if key in conflicts:
            row['perkiraan_dana_cair'] = None
            row['catatan'] = 'Nilai antar paket berbeda; perlu rekonsiliasi'
        rows.append(row)
    stores = { (r['platform'], str(r.get('shopId'))): {'id': str(r.get('shopId')), 'nama': r.get('toko') or 'Nama toko belum tersedia', 'platform': r['platform']} for r in unique.values() }
    statuses = sorted({r['status_label'] for r in rows})
    rows = [r for r in rows if (not platform or r['platform'] == platform) and (not toko or str(r.get('shopId')) == toko) and (not status or r['status_label'] == status) and search.lower() in r['no_pesanan'].lower()]
    totals = {'tiktok': Decimal(0), 'lazada': Decimal(0)}
    per_store = {}
    for row in rows:
        key = (row['platform'], str(row.get('shopId')))
        item = per_store.setdefault(key, {**stores[key], 'jumlah': 0, 'total': Decimal(0), 'tanpa_angka': 0})
        item['jumlah'] += 1
        value = row.get('perkiraan_dana_cair')
        if value is None:
            item['tanpa_angka'] += 1
        else:
            value = Decimal(str(value))
            item['total'] += value
            totals[row['platform']] += value
    uncertain = [r for r in uncertain if (not platform or r['platform'] == platform) and (not toko or str(r.get('shopId')) == toko) and search.lower() in r['no_pesanan'].lower()]
    complete = all(s.get('lengkap') is True for s in sources)
    return {'perlu_verifikasi': uncertain, 'pesanan': rows[(page-1)*page_size:page*page_size], 'jumlah': len(rows), 'page': page, 'page_size': page_size,
            'total': sum(totals.values()), 'total_platform': totals, 'per_toko': list(per_store.values()),
            'toko': list(stores.values()), 'status': statuses, 'lengkap': complete,
            'tanpa_angka': sum(r.get('perkiraan_dana_cair') is None for r in rows),
            'diambil_wib': min((s.get('diambil_wib', '') for s in sources), default=''),
            'catatan': 'Perkiraan dari escrowAmount EasyBoss. Biaya platform mungkin belum diperhitungkan. Tidak ada tanggal pencairan bukan bukti saldo final. Hanya pesanan dikirim/diterima/selesai tanpa penanda pencairan; batal, retur, belum dikirim dan status tidak dikenal dikecualikan.',
            'cakupan': [{'platform': s.get('platform'), 'tab': s.get('tab'), 'terbaca': s.get('pesanan_terbaca'), 'total': s.get('pesanan_di_tab'), 'lengkap': s.get('lengkap')} for s in sources]}


_task = None
_error = None
_error_at = 0


async def _load():
    global _cache, _error, _error_at
    try:
        token = Path(os.environ['ERP_BUSINESS_TOOLS_TOKEN_FILE']).read_text().strip()
        sources = []
        async with httpx.AsyncClient(timeout=55, trust_env=False, follow_redirects=False) as client:
            for platform in ('tiktok', 'lazada'):
                combined = None
                # All tabs are covered by `all`, including delivered/completed awaiting payout.
                # Bounded batches retain the existing vault cache, lock and 429 circuit breaker.
                for start in range(1, 501, 4):
                    response = await client.post('http://ai-business-tools:8090/api/call', headers={'Authorization': 'Bearer '+token}, json={'tool': 'eb_vault_dana_cair', 'arguments': {'platform': platform, 'tab': 'all', 'halaman_mulai': start, 'maks_halaman': 4, 'rincian': True}})
                    if response.status_code != 200:
                        raise ValueError('Source unavailable')
                    data = response.json()['result']
                    if not isinstance(data, dict) or 'pesanan' not in data:
                        raise ValueError('Invalid source')
                    if combined is None:
                        combined = {**data, 'pesanan': []}
                    combined['pesanan'].extend(data['pesanan'])
                    combined['pesanan_terbaca'] = len(combined['pesanan'])
                    combined['lengkap'] = len(combined['pesanan']) >= data['pesanan_di_tab']
                    if combined['lengkap'] or not data['pesanan']:
                        break
                    await asyncio.sleep(2)
                sources.append(combined)
        _cache = (time.monotonic(), sources)
        _error = None
    except (OSError, KeyError, ValueError, httpx.HTTPError):
        _error = 'Data EasyBoss belum tersedia. Periksa koneksi dan sesi Credential Vault. Sumber mungkin membatasi akses; pembacaan ditunda 60 menit.'
        _error_at = time.monotonic()


async def snapshot():
    global _task
    async with _lock:
        if _cache and time.monotonic() - _cache[0] < 600:
            return _cache[1]
        if _error and time.monotonic() - _error_at < 3600:
            raise HTTPException(503, _error)
        if _task is None or _task.done():
            _task = asyncio.create_task(_load())
        return None
