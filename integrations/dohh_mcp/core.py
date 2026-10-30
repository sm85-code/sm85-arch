"""Dohh boundary: dedicated ERP principal, exact shops, no live writes."""
from uuid import UUID, uuid4
import json

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from .contract import inventory, verify_sources

BASE = 'https://api.ampelkuning.com/api/marketplace-erp'


class GuardError(Exception):
    """Static safe error; never include remote bodies or credentials."""


class Dohh:
    def __init__(self, http, token, shops):
        self.http = http
        self.token = token
        self.shops = frozenset(shops)
        if len(shops) != 11 or len(self.shops) != 11:
            raise GuardError('Perlu tepat 11 toko unik')
        if not token or any(str(UUID(s)) != s for s in shops):
            raise GuardError('Konfigurasi credential atau toko tidak valid')

    async def _request(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, BASE + path,
                headers={'Authorization': 'Bearer ' + self.token}, follow_redirects=False, **kwargs)
            if response.status_code != 200:
                raise GuardError(f'ERP menolak permintaan (HTTP {response.status_code})')
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise GuardError('ERP tidak tersedia atau respons tidak valid') from None

    async def _preflight(self):
        me = await self._request('GET', '/auth/me')
        if me.get('role') != 'agen_iklan' or me.get('peran_tambahan') or me.get('username', '').lower() == 'hermes-ai':
            raise GuardError('Wajib akun khusus agen_iklan tanpa peran tambahan')
        access = await self._request('GET', '/auth/akses')
        expected = {'iklan': 'tulis', 'produk': 'baca', 'katalog': 'baca', 'pesanan': 'baca'}
        if access.get('roles') != ['agen_iklan'] or access.get('akses_toko') != 'ditugaskan' or access.get('halaman') != expected:
            raise GuardError('Hak akses agen tidak sesuai')
        shops = await self._request('GET', '/akun')
        if not isinstance(shops, list) or len(shops) != 11 or any(s.get('platform') != 'shopee' for s in shops) or {s.get('id') for s in shops} != self.shops:
            raise GuardError('Cakupan toko ERP tidak sesuai 11 toko')

    async def call(self, name, args):
        tool = next((t for t in inventory() if t['name'] == name), None)
        if tool is None:
            raise GuardError('Alat tidak tersedia; live/STOP perlu gateway owner tervalidasi')
        try:
            verify_sources()
            json.dumps(args, allow_nan=False)
            Draft202012Validator(tool['inputSchema'], format_checker=FormatChecker()).validate(args)
        except Exception:
            raise GuardError('Kontrak source atau argumen tidak valid') from None
        if 'akun_id' in args and args['akun_id'] not in self.shops:
            raise GuardError('Toko tidak diizinkan')
        await self._preflight()
        if tool['method'] == 'PLAN':
            return {'kering': True, 'diterapkan': False, 'lokal_saja': True,
                    'rencana': args['body'], 'catatan': 'Tidak disimpan ke ERP; bukan validasi bisnis ERP'}
        path = tool['path']
        if not path.startswith('/'):
            path = f'/akun/{args["akun_id"]}/iklan/agen/' + path
        path = path.format(**args)
        params = args.get('params', {})
        if name == 'dohh_audit':
            params = {'akun_id': args['akun_id'], 'limit': args.get('limit', 50)}
        if tool['method'] == 'GET':
            return await self._request('GET', path, params=params)
        body = {**args['body'], 'kering': True, 'persetujuan': None,
                'request_id': 'dohh-dry-' + uuid4().hex}
        result = await self._request('POST', path, json=body)
        if not isinstance(result, dict) or result.get('kering') is not True or result.get('diterapkan') is not False:
            raise GuardError('Respons bukan simulasi aman; hentikan dan periksa ERP')
        return result
