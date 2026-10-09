"""Bounded admin listings; existing array endpoints remain available to older clients."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from ..infrastructure.models import ProdukStore, PesananStore, PembeliStore
from . import services


async def page(session, kind, *, halaman=1, ukuran=25, cari="", status=None, dari=None, sampai=None, urutan="terbaru"):
    model = ProdukStore if kind == "produk" else PesananStore
    stmt = select(model)
    if kind == "produk":
        stmt = stmt.options(selectinload(ProdukStore.kategori), selectinload(ProdukStore.foto), selectinload(ProdukStore.varian))
        if cari:
            stmt = stmt.where(ProdukStore.nama.ilike(f"%{cari}%"))
    else:
        stmt = stmt.join(PembeliStore).options(selectinload(PesananStore.items))
        if cari:
            stmt = stmt.where(or_(PesananStore.id.ilike(f"%{cari}%"), PembeliStore.nama.ilike(f"%{cari}%"), PembeliStore.email.ilike(f"%{cari}%")))
        if status:
            stmt = stmt.where(PesananStore.status == status)
        if dari and sampai and dari > sampai:
            raise HTTPException(400, "Tanggal awal harus sebelum tanggal akhir")
        if dari:
            stmt = stmt.where(PesananStore.created_at >= datetime.combine(dari, time.min, ZoneInfo("Asia/Jakarta")))
        if sampai:
            stmt = stmt.where(PesananStore.created_at < datetime.combine(sampai + timedelta(days=1), time.min, ZoneInfo("Asia/Jakarta")))
    count = (await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    orders = {"terbaru": model.created_at.desc(), "terlama": model.created_at.asc()}
    if kind == "produk":
        orders.update(nama=ProdukStore.nama.asc(), harga=ProdukStore.harga.desc())
    else:
        orders.update(total=PesananStore.total.desc())
    rows = list((await session.execute(stmt.order_by(orders.get(urutan, orders["terbaru"]), model.id).offset((halaman-1)*ukuran).limit(ukuran))).scalars())
    output = services.produk_out if kind == "produk" else services.pesanan_out
    result = [output(r) for r in rows]
    if kind == "pesanan":
        buyers = {r.id: r for r in (await session.execute(select(PembeliStore).where(PembeliStore.id.in_({r.user_id for r in rows})))).scalars()}
        for data, r in zip(result, rows, strict=True):
            data["nama_pembeli"] = buyers[r.user_id].nama
    return {"items": result, "total": count, "halaman": halaman, "ukuran": ukuran}
