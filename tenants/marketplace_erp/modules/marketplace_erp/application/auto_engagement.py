"""Per-shop opt-in templates/boost. Durable claims precede writes; unknown results never replay."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update, or_

from .commerce_schemas import ReplyComments
from ..infrastructure.models import EngagementPolicy, EngagementAttempt, AkunMarketplace, KatalogShopee, UserMarketplaceErp
from ..infrastructure.adapters import erp_shopee, erp_shopee_engagement as engagement

logger = logging.getLogger(__name__)
POLL_SECONDS = 300
REPLY_DELAY_SECONDS = 60


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def eligible(review, since):
    timestamp, rating = review.get('create_time'), review.get('rating_star')
    if type(timestamp) is not int or type(rating) is not int or not 1 <= rating <= 5 or review.get('reply') or not since:
        return False
    try:
        created = datetime.fromtimestamp(timestamp, timezone.utc)
        return utc(since) <= created <= now() - timedelta(seconds=REPLY_DELAY_SECONDS)
    except (ValueError, OverflowError, OSError):
        return False


async def authorized(session, shop_id, kind):
    shop = await session.scalar(select(AkunMarketplace).where(AkunMarketplace.id == shop_id).with_for_update().execution_options(populate_existing=True))
    rule = await session.scalar(select(EngagementPolicy).where(EngagementPolicy.akun_id == shop_id).with_for_update().execution_options(populate_existing=True))
    actor = await session.scalar(select(UserMarketplaceErp).where(UserMarketplaceErp.id == rule.enabled_by).with_for_update().execution_options(populate_existing=True)) if rule and rule.enabled_by else None
    if not erp_shopee.live_sync_enabled() or not shop or shop.status == 'nonaktif' or not shop.access_token or not shop.id_toko_eksternal or not rule or not actor or actor.role != 'admin' or not getattr(rule, 'replies_enabled' if kind == 'reply' else 'boost_enabled'):
        raise HTTPException(409, 'Izin otomatis atau koneksi toko tidak aktif.')
    return shop, rule


async def claim_shop(session):
    await session.execute(update(EngagementAttempt).where(EngagementAttempt.status == 'running', EngagementAttempt.created_at < now() - timedelta(minutes=10)).values(status='unknown', result_json=json.dumps({'message': 'Proses terputus. Periksa Seller Centre; tidak diulang otomatis.'})))
    rule = await session.scalar(select(EngagementPolicy).join(AkunMarketplace, AkunMarketplace.id == EngagementPolicy.akun_id).where(
        or_(EngagementPolicy.replies_enabled.is_(True), EngagementPolicy.boost_enabled.is_(True)),
        AkunMarketplace.status != 'nonaktif', or_(EngagementPolicy.next_scan_at.is_(None), EngagementPolicy.next_scan_at <= now()),
    ).order_by(EngagementPolicy.next_scan_at, EngagementPolicy.akun_id).limit(1).with_for_update(of=EngagementPolicy, skip_locked=True))
    if not rule:
        await session.commit()
        return None
    ident = rule.akun_id
    rule.next_scan_at = now() + timedelta(seconds=POLL_SECONDS)
    rule.last_scan_at = now()
    await session.commit()
    return ident


async def finish(session, attempt, result):
    attempt.status = 'succeeded' if result.get('ok') else 'rejected'
    attempt.result_json = json.dumps(result, ensure_ascii=False, default=str)
    await session.commit()


async def failed(session, shop_id, key, exc, sent):
    await session.rollback()
    attempt = await session.get(EngagementAttempt, (shop_id, key))
    if not attempt:
        return
    detail = getattr(exc, 'detail', None)
    result = detail if isinstance(detail, dict) else {'message': 'Hasil belum pasti; periksa Seller Centre.' if sent else 'Pemeriksaan tidak lolos; pengiriman otomatis dihentikan.'}
    attempt.status = 'unknown' if sent and getattr(exc, 'status_code', 500) >= 500 else 'blocked'
    attempt.result_json = json.dumps(result, ensure_ascii=False, default=str)
    await session.commit()


async def run_review(factory, shop_id, review):
    key = 'reply:' + str(review['comment_id'])
    async with factory() as session:
        sent = False
        try:
            shop = await session.get(AkunMarketplace, shop_id)
            await erp_shopee.pastikan_token_segar(session, shop)  # Token refresh commits; do it before authorization locks.
            shop, rule = await authorized(session, shop_id, 'reply')
            if not eligible(review, rule.replies_enabled_at) or await session.get(EngagementAttempt, (shop_id, key)):
                return
            session.add(EngagementAttempt(akun_id=shop_id, key=key, kind='reply', target=str(review['comment_id'])))
            await session.commit()
            shop, rule = await authorized(session, shop_id, 'reply')
            fresh = await engagement.comments(session, shop, review['item_id'], page_size=1, comment_id=int(review['comment_id']))
            current = next((r for r in fresh['items'] if r['comment_id'] == str(review['comment_id'])), None)
            if not current or not eligible(current, rule.replies_enabled_at):
                raise HTTPException(409, 'Ulasan sudah dibalas, berubah, atau mendahului izin aktif.')
            text = rule.high_template if current['rating_star'] >= 4 else rule.low_template
            if not text.strip():
                raise HTTPException(409, 'Template belum lengkap.')
            payload = ReplyComments(comment_list=[{'comment_id': current['comment_id'], 'comment': text}])
            attempt = await session.get(EngagementAttempt, (shop_id, key), with_for_update=True)
            sent = True
            async def recheck():
                _, latest = await authorized(session, shop_id, 'reply')
                expected = latest.high_template if current['rating_star'] >= 4 else latest.low_template
                if not eligible(current, latest.replies_enabled_at) or expected != text:
                    raise HTTPException(409, 'Izin atau template berubah sebelum pengiriman.')
            result = await engagement.reply(session, shop, current['item_id'], payload, before_send=recheck)
            await finish(session, attempt, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await failed(session, shop_id, key, exc, sent)


async def run_boost(factory, shop_id):
    key = None
    async with factory() as session:
        sent = False
        try:
            shop = await session.get(AkunMarketplace, shop_id)
            await erp_shopee.pastikan_token_segar(session, shop)
            shop, rule = await authorized(session, shop_id, 'boost')
            if rule.next_boost_at and utc(rule.next_boost_at) > now():
                return
            if await session.scalar(select(EngagementAttempt.key).where(EngagementAttempt.akun_id == shop_id, EngagementAttempt.kind == 'boost', EngagementAttempt.status.in_(['running', 'unknown'])).limit(1)):
                rule.last_message = 'Boost dijeda: hasil sebelumnya belum pasti. Periksa dan tandai catatan terlebih dahulu.'
                await session.commit()
                return
            ids = json.loads(rule.boost_catalog_ids)
            products = (await session.scalars(select(KatalogShopee).where(KatalogShopee.id.in_(ids), KatalogShopee.akun_id == shop_id, KatalogShopee.status == 'NORMAL'))).all()
            if not ids or {p.id for p in products} != set(ids):
                rule.last_message = 'Pilihan boost tidak aktif/tidak lengkap. Perbarui pilihan produk.'
                await session.commit()
                return
            status = await engagement.boosted(session, shop)
            cooldown = {r['item_id']: r['cool_down_second'] for r in status['items']}
            available = [p for p in products if cooldown.get(str(p.item_id), 0) <= 0]
            if not available:
                rule.next_boost_at = now() + timedelta(seconds=max(POLL_SECONDS, min(cooldown[str(p.item_id)] for p in products)))
                rule.last_message = 'Menunggu cooldown Shopee untuk produk pilihan.'
                await session.commit()
                return
            key = 'boost:' + now().isoformat()
            session.add(EngagementAttempt(akun_id=shop_id, key=key, kind='boost', target=json.dumps([p.item_id for p in available])))
            rule.next_boost_at = now() + timedelta(minutes=rule.boost_interval_minutes)
            await session.commit()  # Claim persists before external POST.
            shop, rule = await authorized(session, shop_id, 'boost')
            if set(json.loads(rule.boost_catalog_ids)) != set(ids):
                raise HTTPException(409, 'Pilihan produk berubah. Percobaan lama tidak dilanjutkan.')
            attempt = await session.get(EngagementAttempt, (shop_id, key), with_for_update=True)
            sent = True
            async def recheck():
                _, latest = await authorized(session, shop_id, 'boost')
                if set(json.loads(latest.boost_catalog_ids)) != set(ids):
                    raise HTTPException(409, 'Pilihan produk berubah sebelum pengiriman.')
            result = await engagement.boost(session, shop, [p.item_id for p in available], before_send=recheck)
            await finish(session, attempt, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if key:
                await failed(session, shop_id, key, exc, sent)
            else:
                await session.rollback()
                rule = await session.get(EngagementPolicy, shop_id)
                if rule:
                    detail = getattr(exc, 'detail', None)
                    rule.last_message = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else engagement.safe(detail, shop) if isinstance(exc, HTTPException) else 'Pemeriksaan boost gagal; periksa koneksi dan izin toko.'
                    await session.commit()


async def run_shop(factory, shop_id):
    async with factory() as session:
        rule = await session.get(EngagementPolicy, shop_id)
        shop = await session.get(AkunMarketplace, shop_id)
        enabled, since = bool(rule and rule.replies_enabled), rule.replies_enabled_at if rule else None
        if enabled:
            try:
                shop, rule = await authorized(session, shop_id, 'reply')
                since = rule.replies_enabled_at
                await session.commit()
                cursor, reviews = '', []
                for _ in range(5):
                    page = await engagement.comments(session, shop, None, cursor=cursor, page_size=100)
                    reviews.extend(r for r in page['items'] if eligible(r, since))
                    if not page['more'] or not page['next_cursor'] or page['next_cursor'] == cursor:
                        break
                    cursor = page['next_cursor']
                keys = ['reply:' + str(r['comment_id']) for r in reviews]
                existing = set((await session.scalars(select(EngagementAttempt.key).where(EngagementAttempt.akun_id == shop_id, EngagementAttempt.key.in_(keys)))).all())
                unique = {str(r['comment_id']): r for r in reviews if 'reply:' + str(r['comment_id']) not in existing}
                for review in list(unique.values())[:20]:
                    # Let each attempt use its own durable transaction.
                    await run_review(factory, shop_id, review)
                rule = await session.get(EngagementPolicy, shop_id, populate_existing=True)
                rule.last_message = 'Pemeriksaan rating selesai; maksimal 500 ulasan terbaru dapat dibaca dari API Shopee.'
                await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await session.rollback()
                rule = await session.get(EngagementPolicy, shop_id)
                if rule:
                    detail = getattr(exc, 'detail', None)
                    rule.last_message = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else engagement.safe(detail, shop) if isinstance(exc, HTTPException) else 'Pembacaan rating gagal; periksa koneksi/izin Shopee.'
                    await session.commit()
    async with factory() as session:
        rule = await session.get(EngagementPolicy, shop_id)
        boost_enabled = bool(rule and rule.boost_enabled)
    if boost_enabled:
        await run_boost(factory, shop_id)


async def run_forever(factory):
    while True:
        await asyncio.sleep(60)
        if not erp_shopee.live_sync_enabled():
            continue
        try:
            for _ in range(10):
                async with factory() as session:
                    ident = await claim_shop(session)
                if ident is None:
                    break
                await run_shop(factory, ident)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Automatic review/boost round failed')
