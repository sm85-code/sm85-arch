"""Explicit standing authorization per shop; disabled until an admin saves it."""
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application.workflow_schemas import Input
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import EngagementPolicy, EngagementAttempt, KatalogShopee, UserMarketplaceErp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from .management_router import account

router = APIRouter(prefix='/akun/{id}/otomasi-produk')
DB = Depends(get_db_marketplace_erp)
ADMIN = Depends(require_roles_marketplace_erp('admin'))


class SettingsIn(Input):
    replies_enabled: bool = False
    high_template: str = Field('', max_length=5000)
    low_template: str = Field('', max_length=5000)
    boost_enabled: bool = False
    boost_catalog_ids: list[str] = Field(default_factory=list, max_length=5)
    boost_interval_minutes: int = Field(240, ge=240, le=10080)

    @model_validator(mode='after')
    def complete(self):
        self.high_template, self.low_template = self.high_template.strip(), self.low_template.strip()
        if self.replies_enabled and (not self.high_template or not self.low_template):
            raise ValueError('Isi template rating 4–5 dan rating 1–3 sebelum mengaktifkan.')
        if self.boost_enabled and not self.boost_catalog_ids:
            raise ValueError('Pilih produk sebelum mengaktifkan boost otomatis.')
        if len(set(self.boost_catalog_ids)) != len(self.boost_catalog_ids) or any(not v or len(v) > 64 for v in self.boost_catalog_ids):
            raise ValueError('Pilih maksimal lima produk berbeda.')
        return self


@router.get('')
async def settings(id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    shop = await account(session, user, id)
    rule = await session.get(EngagementPolicy, id)
    attempts = (await session.scalars(select(EngagementAttempt).where(EngagementAttempt.akun_id == id).order_by(EngagementAttempt.created_at.desc()).limit(30))).all()
    return {'nama_toko': shop.nama_toko, 'worker_enabled': erp_shopee.live_sync_enabled(), 'poll_seconds': 300,
            **{k: getattr(rule, k) if rule else v for k, v in {'replies_enabled': False, 'high_template': '', 'low_template': '', 'boost_enabled': False, 'boost_interval_minutes': 240}.items()},
            'boost_catalog_ids': json.loads(rule.boost_catalog_ids) if rule else [],
            'replies_enabled_at': rule.replies_enabled_at if rule else None,
            'last_scan_at': rule.last_scan_at if rule else None, 'last_message': rule.last_message if rule else '',
            'next_boost_at': rule.next_boost_at if rule else None,
            'attempts': [{'key': a.key, 'kind': a.kind, 'target': a.target, 'status': a.status, 'created_at': a.created_at, 'result': json.loads(a.result_json)} for a in attempts]}


@router.put('')
async def save(id: str, body: SettingsIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    shop = await account(session, user, id)
    # This lock also serializes policy creation and worker authorization.
    await session.refresh(shop, with_for_update=True)
    if (body.replies_enabled or body.boost_enabled) and (shop.status == 'nonaktif' or not shop.access_token or not shop.id_toko_eksternal or not erp_shopee.live_sync_enabled()):
        raise HTTPException(409, 'Hubungkan toko dan aktifkan Shopee live sync sebelum mengaktifkan otomatis.')
    if body.boost_catalog_ids:
        rows = (await session.scalars(select(KatalogShopee).where(KatalogShopee.id.in_(body.boost_catalog_ids), KatalogShopee.akun_id == id, KatalogShopee.status == 'NORMAL'))).all()
        if {r.id for r in rows} != set(body.boost_catalog_ids) or len({r.item_id for r in rows}) != len(rows):
            raise HTTPException(422, 'Pilih produk aktif yang berbeda dari toko ini; sinkronkan katalog bila perlu.')
    rule = await session.get(EngagementPolicy, id, with_for_update=True)
    if not rule:
        rule = EngagementPolicy(akun_id=id, replies_enabled=False, boost_enabled=False)
        session.add(rule)
    now = datetime.now(timezone.utc)
    if body.replies_enabled and not rule.replies_enabled:
        rule.replies_enabled_at = now
    changed_boost = body.boost_enabled and (not rule.boost_enabled or rule.boost_catalog_ids != json.dumps(body.boost_catalog_ids))
    for key, value in body.model_dump(exclude={'boost_catalog_ids'}).items():
        setattr(rule, key, value)
    rule.boost_catalog_ids = json.dumps(body.boost_catalog_ids)
    rule.enabled_by = user.id
    rule.next_scan_at = now
    if changed_boost:
        rule.next_boost_at = now
    await session.commit()
    return {'ok': True}


class ReviewedIn(Input):
    key: str = Field(min_length=1, max_length=100)
    decision: Literal['already_done', 'skip'] = 'skip'


@router.post('/sudah-diperiksa')
async def reviewed(id: str, body: ReviewedIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    shop = await account(session, user, id)
    await session.refresh(shop, with_for_update=True)
    row = await session.get(EngagementAttempt, (id, body.key), with_for_update=True)
    if not row or row.status != 'unknown':
        raise HTTPException(409, 'Hanya hasil belum pasti yang dapat ditandai setelah diperiksa.')
    row.status = 'reviewed'
    row.result_json = json.dumps({'message': 'Admin telah memeriksa hasil di Seller Centre; tidak mengulang percobaan lama.', 'decision': body.decision, 'reviewed_by': user.id})
    await session.commit()
    return {'ok': True}
