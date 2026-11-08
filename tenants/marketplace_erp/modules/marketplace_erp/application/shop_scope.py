"""Preserve marketplace assignment IDs; namespace internal store access."""

import json
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select, or_, literal, func
from ..infrastructure.models import (
    Pesanan,
    StaffTokoPenjualan,
    PesananTokoPenjualan,
    TokoPenjualan,
    UserMarketplaceErp,
    BridgeOrder,
)

PREFIX = "internal:"
ASSIGN_PREFIX = "internal-assignment:"


def condition(allowed):
    if allowed is None:
        return literal(True)
    native = [x for x in allowed if not x.startswith(PREFIX)]
    internal = [x[len(PREFIX) :] for x in allowed if x.startswith(PREFIX)]
    return or_(
        Pesanan.akun_id.in_(native),
        Pesanan.id.in_(select(PesananTokoPenjualan.pesanan_id).where(PesananTokoPenjualan.toko_id.in_(internal))),
    )


def key_expression():
    linked = (
        select(PesananTokoPenjualan.toko_id)
        .where(PesananTokoPenjualan.pesanan_id == Pesanan.id)
        .correlate(Pesanan)
        .scalar_subquery()
    )
    return func.coalesce(Pesanan.akun_id, literal(PREFIX) + linked)


def view(row):
    return SimpleNamespace(
        id=ASSIGN_PREFIX + row.id, user_id=row.user_id, akun_id=PREFIX + row.toko_id, created_at=row.created_at
    )


async def internal_rows(session, user_id=None):
    q = select(StaffTokoPenjualan).order_by(StaffTokoPenjualan.created_at.desc())
    if user_id:
        q = q.where(StaffTokoPenjualan.user_id == user_id)
    return [view(r) for r in (await session.scalars(q)).all()]


async def assign_many(session, user_id, ids):
    from . import akses, services
    from ..infrastructure.models import StaffAkunMarketplace

    user = await session.scalar(select(UserMarketplaceErp).where(UserMarketplaceErp.id == user_id).with_for_update())
    if not user or await akses.akses_toko(session, await akses.peran_pengguna(session, user)) != "ditugaskan":
        raise HTTPException(422, "Pilih pengguna dengan role yang aksesnya dibatasi per toko")
    ids = list(dict.fromkeys(ids))
    for value in ids:
        if value.startswith(PREFIX):
            shop = await session.get(TokoPenjualan, value[len(PREFIX) :])
            if not shop or shop.jenis != "internal" or not shop.aktif:
                raise HTTPException(422, "Pilih toko internal aktif yang tersedia")
        else:
            await services.get_akun_marketplace(session, value)
    existing = {r.akun_id: r for r in await services.list_staff_akun(session, user_id=user_id)}
    result = []
    for value in ids:
        if value not in existing:
            if value.startswith(PREFIX):
                row = StaffTokoPenjualan(user_id=user_id, toko_id=value[len(PREFIX) :])
                session.add(row)
                await session.flush()
                existing[value] = view(row)
            else:
                row = StaffAkunMarketplace(user_id=user_id, akun_id=value)
                session.add(row)
                await session.flush()
                existing[value] = row
        result.append(existing[value])
    return result


async def backfill_links(session):
    """Create permission links only; never rewrite orders or assignments."""
    known = set((await session.scalars(select(PesananTokoPenjualan.pesanan_id))).all())
    stores = set((await session.scalars(select(TokoPenjualan.id).where(TokoPenjualan.jenis == "internal"))).all())
    bridges = {
        r.pesanan_id: r.toko_id
        for r in (await session.scalars(select(BridgeOrder).where(BridgeOrder.pesanan_id.is_not(None)))).all()
    }
    for pid, detail in (
        await session.execute(select(Pesanan.id, Pesanan.detail_json).where(Pesanan.platform == "manual"))
    ).all():
        if pid in known:
            continue
        try:
            store = bridges.get(pid) or json.loads(detail or "{}").get("manual", {}).get("toko_id")
        except (ValueError, TypeError, AttributeError):
            continue
        if store in stores:
            session.add(PesananTokoPenjualan(pesanan_id=pid, toko_id=store))
    await session.flush()


async def options(session, allowed=None):
    from ..infrastructure.models import AkunMarketplace

    native = (await session.scalars(select(AkunMarketplace).order_by(AkunMarketplace.nama_toko))).all()
    internal = (
        await session.scalars(
            select(TokoPenjualan)
            .where(TokoPenjualan.jenis == "internal", TokoPenjualan.aktif.is_(True))
            .order_by(TokoPenjualan.nama)
        )
    ).all()
    values = [{"id": x.id, "nama_toko": x.nama_toko, "platform": x.platform, "jenis": "marketplace"} for x in native]
    values += [
        {"id": PREFIX + x.id, "nama_toko": x.nama, "platform": x.platform, "jenis": "internal"} for x in internal
    ]
    return [x for x in values if allowed is None or x["id"] in allowed]
