"""Create the ads-agent user from the environment. The password is never printed.

AGEN_IKLAN_USERNAME, AGEN_IKLAN_PASSWORD, and optional AGEN_IKLAN_AKUN (comma-separated shop ids).
Does not modify the hermes-ai account. Requires DATABASE_URL_MARKETPLACE_ERP.
"""
from __future__ import annotations

import asyncio
import sys

from tenants.marketplace_erp.modules.marketplace_erp.application.agen_pengguna import PenggunaAgenIn, periksa_env
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database


async def _jalan() -> int:
    try:
        data = periksa_env()
    except Exception as exc:  # noqa: BLE001 -- the command reports the guard message and stops
        print(getattr(exc, "detail", "Perintah agen iklan ditolak"), file=sys.stderr)
        return 2
    if database.SessionLocal is None:
        print("DATABASE_URL_MARKETPLACE_ERP belum diisi", file=sys.stderr)
        return 2
    async with database.SessionLocal() as session:
        from tenants.marketplace_erp.modules.marketplace_erp.application.agen_pengguna import buat
        hasil = await buat(session, PenggunaAgenIn(username=data["username"], password=data["password"], akun_ids=data["akun_ids"]))
        await session.commit()
    print(f"pengguna agen dibuat: {hasil['username']} role={hasil['role']} toko={len(hasil['akun_ids'])}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_jalan()))


if __name__ == "__main__":
    main()
