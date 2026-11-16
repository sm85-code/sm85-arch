"""Run after a verified database backup. Idempotent, no manual figures changed.

python -m scripts.seed_kas_lazada 2026-11
"""
import asyncio
import sys
from types import SimpleNamespace
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import SessionLocal, engine
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import LaporanManualAudit, LaporanKeuanganManual
from tenants.marketplace_erp.modules.marketplace_erp.application import financial_manual as m


async def run(period):
    async with engine.begin() as conn:
        await conn.run_sync(LaporanManualAudit.__table__.create, checkfirst=True)
    async with SessionLocal() as session:
        # Snapshot exists on the production month; seed only the requested month.
        stored = await session.get(LaporanKeuanganManual, period)
        if stored is None:
            raise RuntimeError("Bulan sumber belum tersimpan")
        original = await m.muat(session, period)
        structure = await m.struktur(session, period)
        rows = structure["rows"]
        added = 0
        for parent, label, key in (("k_toko", "Lazada: BUMI TERNAK IND", "c:lazada-bumi-ternak"),
                                   ("k_cat", "Lazada: AZFA Furniture", "c:lazada-azfa")):
            if not any(r["id"] == key or (r["parent_id"] == parent and r["label"].casefold() == label.casefold()) for r in rows):
                rows.append(dict(id=key, label=label, parent_id=parent, aktif=True, urut=len(rows)))
                added += 1
        if added:
            actor = SimpleNamespace(id=stored.updated_by)
            result = await m.simpan_struktur(session, period, m.KasStructure(version=structure["version"], rows=rows), actor)
            assert result["lembar"]["sel"] == original["lembar"]["sel"]
        print(f"periode={period} added={added}; existing figures preserved")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
