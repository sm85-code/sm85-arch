"""Read-only MCP transport. Source cookies remain inside the existing MCP."""
import json
from decimal import Decimal, InvalidOperation
from .easyboss import BridgeProblem, Line, Order, RejectedOrder, Shop, native_time

PLATFORMS = {'tiktok': 'tiktokshop', 'lazada': 'lazada'}
READS = {'eb_toko_list', 'eb_pesanan_hitung', 'eb_pesanan_list', 'eb_pesanan_detail'}


def native_amount(value):
    if value in (None, '', '--'):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError()
        return amount
    except InvalidOperation:
        raise ValueError('invalid source amount') from None


class MCPGateway:
    def __init__(self, client):
        self.client = client
        self.counter = 0

    async def call(self, name, arguments):
        if name not in READS:
            raise BridgeProblem('mcp_read_not_allowed')
        self.counter += 1
        try:
            r = await self.client.post(str(self.client.base_url).rstrip('/'), json={'jsonrpc': '2.0', 'id': self.counter,
                'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
            r.raise_for_status()
            if 'text/event-stream' in r.headers.get('content-type', ''):
                events = [json.loads(s[6:]) for s in r.text.splitlines() if s.startswith('data: ')]
                envelope = next(e for e in events if e.get('id') == self.counter)
            else:
                envelope = r.json()
            if envelope.get('id') != self.counter or 'error' in envelope:
                raise ValueError()
            result = envelope['result']
            blocks = result['content']
            if result.get('isError') or len(blocks) != 1 or blocks[0]['type'] != 'text':
                raise ValueError()
            data = json.loads(blocks[0]['text'])
            if isinstance(data, dict) and 'error' in data:
                raise ValueError()
            return data
        except Exception:
            raise BridgeProblem('mcp_read_failed') from None

    async def shops(self):
        rows = await self.call('eb_toko_list', {})
        if not isinstance(rows, list):
            raise BridgeProblem('mcp_shop_contract')
        counts = await self.call('eb_pesanan_hitung', {'tab': 'all'})
        if not isinstance(counts, dict) or not isinstance(counts.get('per_toko'), list):
            raise BridgeProblem('mcp_shop_alias_contract')
        aliases = {(s.get('platform'), str(s.get('shopId'))): s.get('shopName')
                   for s in counts['per_toko'] if isinstance(s, dict)}
        result = []
        for platform, canonical in PLATFORMS.items():
            shops = [s for s in rows if s.get('platform') == platform]
            if len(shops) >= 1000:
                raise BridgeProblem('mcp_shop_directory_truncated')
            for s in shops:
                result.append(Shop(platform=canonical, shop_id=str(s['shopId']),
                    marketplace_shop_id=str(s['platformShopId']),
                    name=(aliases.get((platform, str(s['shopId']))) or s['platformShopName'])
                    if '@' in s['platformShopName'] else s['platformShopName'],
                    active=s.get('authStatus') == 'valid' and s.get('status') == 'normal'
                    and s.get('platformStatus') == 'normal'))
        if len({(s.platform, s.shop_id) for s in result}) != len(result):
            raise BridgeProblem('duplicate_shop_identity')
        return result

    async def one(self, platform, shop_id, order_id):
        identity = (PLATFORMS[platform], shop_id, order_id)
        data = await self.call('eb_pesanan_list', {'tab': 'all', 'platform': platform,
            'per_halaman': 2, 'extra': {'platformOrderSns': order_id}})
        rows = data.get('pesanan')
        if data.get('total') != 1 or not isinstance(rows, list) or len(rows) != 1:
            return RejectedOrder(platform=identity[0], shop_id=shop_id, order_id=order_id)
        row = rows[0]
        if str(row.get('shopId')) != shop_id or str(row.get('no_pesanan')) != order_id:
            raise BridgeProblem('mcp_order_identity')
        package = await self.call('eb_pesanan_detail', {'no_pesanan': order_id})
        if str(package.get('opOrderPackageId')) != str(row.get('id')):
            raise BridgeProblem('mcp_package_changed')
        return normalize(package, identity)

    async def orders(self, since):
        # New orders come from active queues. Imported/failed identities are
        # explicitly re-read across ALL tabs, so moved/cancelled orders are kept.
        identities = {}
        for platform in PLATFORMS:
            for tab in ('waitProcess', 'waitShip', 'submitPlatform'):
                seen, total = set(), None
                for page in range(1, 1001):
                    data = await self.call('eb_pesanan_list', {'tab': tab, 'platform': platform,
                        'halaman': page, 'per_halaman': 50})
                    batch, count = data.get('pesanan'), data.get('total')
                    if type(count) is not int or count < 0 or not isinstance(batch, list) or len(batch) > 50:
                        raise BridgeProblem('mcp_list_contract')
                    if total is not None and count != total:
                        raise BridgeProblem('mcp_unstable_pagination')
                    total = count
                    for row in batch:
                        key = str(row.get('id') or '')
                        shop_id, order_id = str(row.get('shopId') or ''), str(row.get('no_pesanan') or '')
                        if not key or key in seen or not shop_id or not order_id or row.get('platform') != platform:
                            raise BridgeProblem('mcp_unstable_pagination')
                        seen.add(key)
                        identities[(platform, shop_id, order_id)] = True
                    if len(seen) == total:
                        break
                    if not batch or len(seen) > total:
                        raise BridgeProblem('mcp_incomplete_page')
                else:
                    raise BridgeProblem('pagination_limit')
        reverse = {canonical: native for native, canonical in PLATFORMS.items()}
        for platform, shop_id, order_id in getattr(self, 'tracked_orders', []):
            if platform in reverse:
                identities[(reverse[platform], shop_id, order_id)] = True
        return [await self.one(platform, shop_id, order_id)
                for platform, shop_id, order_id in sorted(identities)]


def normalize(p, identity):
    """Existing detail tool returns FIRST package only; caller proves total==1.

    Lazada ready_to_ship COD is eligible for fulfilment, without being marked paid.
    The marketplace username is an opaque source identifier, not a verified legal name.
    """
    platform, shop_id, order_id = identity
    try:
        o = p['orderInfo']
        if (PLATFORMS.get(o['platform']), str(o['shopId']), str(o['platformOrderSn'])) != identity:
            raise ValueError()
        if str(p['shopId']) != shop_id or str(p['platformOrderSn']) != order_id:
            raise ValueError()
        paid = True if o.get('gmtPay') and str(o.get('isCod')) == '0' else None
        if o.get('gmtPay'):
            native_time(o['gmtPay'])
        source, status = o['platformOrderStatus'], 'unknown'
        cod = str(o.get('isCod')) == '1' and str(o.get('paymentMethod') or '').upper() == 'COD'
        cod_eligible = platform == 'lazada' and source in {'ready_to_ship', 'shipped', 'delivered'} and cod
        from ...application.bridge_status import native_state
        mapped = native_state(platform, source)
        status = mapped[0] if mapped else 'unknown'
        if (p.get('isOnHoldOrder') or p.get('isPendingStatus') or str(p.get('isIsolation')) == '1'
                or o.get('gmtRefund') or o.get('opOrderReturns') or p.get('cancelOrder') or p.get('returnOrder')):
            status = 'hold'
        if not mapped and o.get('gmtDelivery'):
            status = 'shipped'
        if not mapped and o.get('gmtFinish'):
            status = 'completed'
        raw = p['items']
        if not isinstance(raw, dict) or not raw:
            raise ValueError()
        lines = []
        for item in raw.values():
            qty = str(item['quantity'])
            if not qty.isdigit() or int(qty) <= 0:
                raise ValueError()
            if str(item['opOrderId']) != str(o['opOrderId']) or str(item['shopId']) != shop_id:
                raise ValueError()
            lines.append(Line(item_id=str(item['opOrderItemId']), product=item['title'],
                variant=item.get('skuSubName') or '', quantity=int(qty),
                unit_price=native_amount(item.get('discountedPrice')), line_total=native_amount(item.get('discountedAmount')),
                image_url=item.get('picUrl') or None,
                product_sku=item.get('platformItemNum') or None, variant_sku=item.get('platformOuterSkuId') or None))
        complete = (p.get('isSplit') == 0 and len({i.item_id for i in lines}) == len(lines)
            and sum(i.quantity for i in lines) == int(o['itemQuantity'])
            and sum(i.quantity for i in lines) == int(p['itemQuantity']))
        buyer = o.get('buyerUsername') or ''
        deadline = p.get('gmtLastDelivery') or o.get('gmtLastDelivery')
        return Order(platform=platform, shop_id=shop_id, order_id=order_id,
            easyboss_order_id=str(o['opOrderId']), package_ids=[str(p['opOrderPackageId'])],
            complete=complete, paid=paid, cod=cod, cod_eligible=cod_eligible, status=status, source_status=source, buyer=buyer,
            order_total=native_amount(o.get('orderAmount')), buyer_paid=native_amount(o.get('payAmount')),
            currency=o.get('currency') or '', recipient_name=(p.get('consigneeInfo') or {}).get('name') or '',
            recipient_phone=(p.get('consigneeInfo') or {}).get('phone') or '',
            recipient_address=(p.get('consigneeInfo') or {}).get('fullAddress') or '',
            payment_method=o.get('paymentMethod') or '', city=(p.get('consigneeInfo') or {}).get('city') or '',
            ordered_at=native_time(o['gmtOrderStart']) if o.get('gmtOrderStart') else None,
            deadline=native_time(deadline) if deadline else None, buyer_note=o.get('buyerMessage') or '',
            carrier=p.get('logisticsCompany'), tracking_number=p.get('logisticsNo'), items=lines)
    except (KeyError, ValueError, TypeError):
        return RejectedOrder(platform=platform, shop_id=shop_id, order_id=order_id)
