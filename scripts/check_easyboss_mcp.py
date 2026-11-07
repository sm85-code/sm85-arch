"""Read-only live check; prints only counts and eligibility, never source PII."""
import argparse
import asyncio
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import easyboss as eb

async def main(samples):
    async with eb.client() as client:
        gateway = eb.Gateway(client)
        shops = await gateway.shops()
        print('shops', {p: sum(s.platform == p for s in shops) for p in ['tiktokshop', 'lazada']})
        for sample in samples:
            platform, shop, order = sample.split(':', 2)
            row = await gateway.one(platform, shop, order)
            print(platform, type(row).__name__, 'status', getattr(row, 'status', None),
                  'paid', getattr(row, 'paid', None), 'complete', getattr(row, 'complete', None),
                  'buyer_available', bool(getattr(row, 'buyer', '')), 'lines', len(getattr(row, 'items', [])))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', action='append', default=[], help='native_platform:shop_id:order_number')
    asyncio.run(main(parser.parse_args().sample))
