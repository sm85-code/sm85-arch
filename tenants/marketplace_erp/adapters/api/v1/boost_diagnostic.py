"""Diagnose boost denials even when a dependency rejects before the endpoint runs."""
import json
import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from fastapi.routing import APIRoute

logger = logging.getLogger(__name__)
DENIALS = {
    'Akses ditolak': ('erp_permission', 'Akses boost ditolak oleh izin ERP. Hubungi admin untuk akses Katalog Lanjutan.'),
    'Akses ditolak untuk akun marketplace ini': ('erp_account_scope', 'Akses ERP kamu tidak mencakup toko ini. Hubungi admin.'),
    'Produk bukan milik toko yang dipilih': ('erp_account_mismatch', 'Produk tidak cocok dengan toko yang dipilih. Muat ulang katalog.'),
    'Permintaan lintas situs ditolak': ('erp_origin', 'Permintaan lintas situs ditolak ERP. Buka ulang dari alamat ERP resmi.'),
}


def denial_detail(reason, endpoint, account_id):
    # Only static messages/metadata: no headers, body or arbitrary exception text.
    source, message = DENIALS.get(str(reason), ('erp_forbidden', 'Permintaan ditolak ERP. Hubungi dukungan dengan kode pemeriksaan.'))
    report = {
        'diagnostic_id': str(uuid4()), 'time': datetime.now(timezone.utc).isoformat(),
        'source': source, 'endpoint': endpoint, 'account_id': account_id,
        'request_id': None, 'error': source, 'message': message, 'failures': [],
    }
    logger.warning('boost_diagnostic %s', json.dumps(report, ensure_ascii=False))
    return {'message': message, 'diagnostic': report}


def boost_origin_denial(path):
    # Routing has not run yet. Restrict enrichment to this tenant's exact boost path.
    match = re.fullmatch(r'/api/marketplace-erp/akun/([^/]+)/produk-boost/?', path)
    if match:
        return denial_detail('Permintaan lintas situs ditolak', '/api/marketplace-erp/akun/{id}/produk-boost', match[1])
    return None


class BoostDiagnosticRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except HTTPException as exc:
                if not self.path.endswith('/produk-boost') or exc.status_code != 403 or isinstance(exc.detail, dict):
                    raise
                detail = denial_detail(exc.detail, self.path, request.path_params.get('id'))
                raise HTTPException(403, detail, headers=exc.headers) from None

        return handle
