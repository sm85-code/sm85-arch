"""Cash expenditure views over immutable keu journal entries; no duplicate ledger."""
from decimal import Decimal

from sqlalchemy import and_, func, or_, select

from . import keu_ledger as ledger, keu_services as svc, kategori_core
from .schemas_keu_expenses import CATEGORIES
from ..infrastructure import models_keu as m, models_keu_finance as f


async def create(session, user, payload):
    await ledger.lock(session)
    await ledger.book(session)
    await ledger.chart(session)
    _, label, code = CATEGORIES[payload.kategori]
    expense_account = await svc.get(session, f.KeuCoa, await ledger.coa(session, code))
    expected_group = next(row[3] for row in ledger.CHART if row[0] == code)
    if expense_account.jenis != "beban" or expense_account.kelompok != expected_group:
        svc.bad("Kode akun pengeluaran bertabrakan dengan pemetaan akun lain; tinjau Bagan Akun", 409)
    return await ledger.post(session, user, f"pengeluaran:{payload.referensi}", "pengeluaran", payload.tanggal,
        [ledger.line(expense_account.id, payload.jumlah),
         ledger.line(await ledger.cash_coa(session, payload.akun_kas_id), -payload.jumlah, arus="operasional")],
        payload.keterangan, rincian={**ledger.snapshot(payload), "kategori_nama": label})


def classification(tab):
    group = f.KeuCoa.kelompok
    debit = select(f.KeuJurnalBaris.id).join(f.KeuCoa, f.KeuCoa.id == f.KeuJurnalBaris.coa_id).where(
        f.KeuJurnalBaris.jurnal_id == f.KeuJurnal.id, f.KeuJurnalBaris.debet > 0)
    legacy_salary = f.KeuJurnal.rincian["kategori_asli"].as_string().in_(
        [kategori_core.KATEGORI_GAJI, kategori_core.KATEGORI_BIAYA_IKLAN])
    if tab == "vendor":
        return f.KeuJurnal.jenis == "vendor_bayar"
    tagged = and_(f.KeuJurnal.jenis == "pengeluaran", f.KeuJurnal.rincian["tab"].as_string() == tab)
    # Existing cash/manual journals remain classified from actual recorded accounts/category.
    if tab == "bahan":
        recorded = debit.where(group == "hpp_bahan").exists()
    elif tab == "gaji_iklan":
        recorded = and_(or_(debit.where(group.in_(["beban_gaji", "beban_pemasaran"])).exists(), legacy_salary),
                        ~debit.where(group == "hpp_bahan").exists())
    else:
        recorded = and_(debit.where(group.in_(["beban_operasional", "beban_langganan", "beban_sewa", "beban_pemeliharaan", "beban_admin"])).exists(),
                        ~func.coalesce(legacy_salary, False), ~debit.where(group.in_(["beban_gaji", "beban_pemasaran", "hpp_bahan"])).exists())
    return or_(tagged, and_(f.KeuJurnal.jenis.in_(["kas", "manual"]), recorded))


async def listing(session, tab, start, end, search, status, limit, offset):
    await ledger.book(session)
    if start > end:
        svc.bad("Tanggal awal tidak boleh melebihi tanggal akhir")
    cash = select(f.KeuJurnalBaris.jurnal_id).join(f.KeuCoa, f.KeuCoa.id == f.KeuJurnalBaris.coa_id).where(
        f.KeuCoa.kas_akun_id.is_not(None), f.KeuJurnalBaris.kredit > 0)
    query = select(f.KeuJurnal).where(classification(tab), f.KeuJurnal.id.in_(cash),
        f.KeuJurnal.tanggal >= start, f.KeuJurnal.tanggal <= end)
    if status == "pengerjaan":
        query = query.where(ledger.visible())
    elif status == "batal":
        query = query.where(f.KeuJurnal.status == "dibatalkan")
    if search:
        term = "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        vendors = select(f.KeuJurnalBaris.jurnal_id).join(m.KeuVendor, m.KeuVendor.id == f.KeuJurnalBaris.vendor_id).where(
            or_(m.KeuVendor.nama.ilike(term, escape="\\"), m.KeuVendor.kode.ilike(term, escape="\\")))
        query = query.where(or_(f.KeuJurnal.keterangan.ilike(term, escape="\\"), f.KeuJurnal.sumber_key.ilike(term, escape="\\"),
            f.KeuJurnal.rincian["kategori_nama"].as_string().ilike(term, escape="\\"), f.KeuJurnal.id.in_(vendors)))
    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    journals = (await session.execute(query.order_by(f.KeuJurnal.tanggal.desc(), f.KeuJurnal.created_at.desc(), f.KeuJurnal.id)
                                     .offset(offset).limit(limit))).scalars().all()
    ids = [journal.id for journal in journals]
    if not ids:
        return {"rows": [], "total": total, "limit": limit, "offset": offset}
    lines = (await session.execute(select(f.KeuJurnalBaris).where(f.KeuJurnalBaris.jurnal_id.in_(ids))
                                  .order_by(f.KeuJurnalBaris.jurnal_id, f.KeuJurnalBaris.nomor))).scalars().all()
    cash_rows = (await session.execute(select(f.KeuJurnalBaris.jurnal_id, f.KeuJurnalBaris.kredit, m.KeuAkun.nama)
        .join(f.KeuCoa, f.KeuCoa.id == f.KeuJurnalBaris.coa_id).join(m.KeuAkun, m.KeuAkun.id == f.KeuCoa.kas_akun_id)
        .where(f.KeuJurnalBaris.jurnal_id.in_(ids), f.KeuJurnalBaris.kredit > 0))).all()
    vendor_rows = (await session.execute(select(f.KeuJurnalBaris.jurnal_id, m.KeuVendor.nama).join(
        m.KeuVendor, m.KeuVendor.id == f.KeuJurnalBaris.vendor_id).where(f.KeuJurnalBaris.jurnal_id.in_(ids)).distinct())).all()
    details, amounts, account_names, vendor_names = {}, {}, {}, {}
    for line in lines:
        details.setdefault(line.jurnal_id, []).append(svc.record(line))
    for key, amount, name in cash_rows:
        amounts[key] = amounts.get(key, Decimal("0")) + amount
        account_names.setdefault(key, set()).add(name)
    for key, name in vendor_rows:
        vendor_names.setdefault(key, set()).add(name)
    rows = [{**svc.record(journal), "baris": details.get(journal.id, []), "jumlah": str(amounts[journal.id]), "tab": tab,
             "vendor_nama": ", ".join(sorted(vendor_names.get(journal.id, []))),
             "akun_nama": ", ".join(sorted(account_names.get(journal.id, [])))} for journal in journals]
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}
