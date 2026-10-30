"""Diagnose boost denials even when a dependency rejects before the endpoint runs."""
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from fastapi.routing import APIRoute

logger = logging.getLogger(__name__)
DENIALS = {
    'Akses ditolak': ('erp_permission', 'Akses boost ditolak oleh izin ERP. Hubungi admin untuk akses Katalog Lanjutan.'),
    'Akses ditolak untuk akun marketplace ini': ('erp_account_scope', 'Akses ERP kamu tidak mencakup toko ini. Hubungi admin.'),
    'Produk bukan milik toko yang dipilih': ('erp_account_mismatch', 'Produk tidak cocok dengan toko yang dipilih. Muat ulang katalog.'),
}


class BoostDiagnosticRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except HTTPException as exc:
                if not self.path.endswith('/produk-boost') or exc.status_code != 403 or isinstance(exc.detail, dict):
                    raise
                # Only static messages/metadata: never log headers, request body or arbitrary exception text.
                source, message = DENIALS.get(str(exc.detail), ('erp_forbidden', 'Permintaan ditolak ERP. Hubungi dukungan dengan kode pemeriksaan.'))
                report = {
                    'diagnostic_id': str(uuid4()), 'time': datetime.now(timezone.utc).isoformat(),
                    'source': source, 'endpoint': self.path, 'account_id': request.path_params.get('id'),
                    'request_id': None, 'error': source, 'message': message, 'failures': [],
                }
                logger.warning('boost_diagnostic %s', json.dumps(report, ensure_ascii=False))
                raise HTTPException(403, {'message': message, 'diagnostic': report}, headers=exc.headers) from None

        return handle
