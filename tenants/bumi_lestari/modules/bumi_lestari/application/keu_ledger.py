"""Double-entry core. All mutations use the caller's tenant-local transaction."""
from datetime import datetime, timezone
from hashlib import sha256
from decimal import Decimal

from pydantic import TypeAdapter, ValidationError

from .schemas_keu import NonNegativeMoney
from sqlalchemy import and_, func, or_, select, text

from . import keu_services as svc
from .services import pastikan_bulan_terbuka
from ..infrastructure import models_keu as m, models_keu_finance as f

ZERO = Decimal("0")
CHART = (
    ("PIUTANG-KIRIM", "Piutang Dalam Pengiriman", "aset", "piutang_pengiriman"),
    ("PIUTANG-ESCROW", "Piutang Saldo Marketplace / Escrow", "aset", "piutang_escrow"),
    ("PERSEDIAAN", "Persediaan Barang Dagangan", "aset", "persediaan"),
    ("UTANG-VENDOR", "Utang Vendor", "kewajiban", "utang_vendor"),
    ("MODAL", "Modal BUMDes", "ekuitas", "modal"),
    ("DISTRIBUSI", "Prive / Distribusi Hasil", "ekuitas", "ekuitas_lain"),
    ("LABA-DITAHAN", "Laba Ditahan", "ekuitas", "laba_ditahan"),
    ("PENDAPATAN", "Pendapatan Penjualan", "pendapatan", "pendapatan"),
    ("PENDAPATAN-LAIN", "Pendapatan Lain", "pendapatan", "pendapatan_lain"),
    ("HPP-VENDOR", "HPP Vendor", "beban", "hpp_vendor"),
    ("HPP-MANUAL", "HPP Produksi / Pembelian Manual", "beban", "hpp_manual"),
    ("HPP-STOK", "HPP Stok Ready", "beban", "hpp_stok"),
    ("BEBAN-OPERASIONAL", "Beban Operasional", "beban", "beban_operasional"),
    ("BEBAN-LANGGANAN", "Beban Langganan", "beban", "beban_langganan"),
    ("BEBAN-SEWA", "Beban Sewa", "beban", "beban_sewa"),
    ("BEBAN-PEMELIHARAAN", "Beban Pemeliharaan & Perbaikan Aset", "beban", "beban_pemeliharaan"),
    ("BEBAN-ADMIN-BANK", "Beban Administrasi Bank", "beban", "beban_admin"),
    ("BEBAN-MARKETPLACE", "Beban Admin / Layanan Marketplace", "beban", "beban_admin"),
)


def snapshot(payload):
    def canonical(value):
        if isinstance(value, Decimal):
            return format(value.quantize(Decimal("0.01")), "f")
        if isinstance(value, dict):
            return {key: canonical(val) for key, val in value.items()}
        if isinstance(value, list):
            return [canonical(val) for val in value]
        return svc.json_value(value)
    return canonical(payload.model_dump())


async def lock(session):
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(61062027)"))


async def book(session, *, required=True):
    result = await session.get(f.KeuBuku, "utama")
    if result is None and required:
        svc.bad("Aktifkan buku besar keu dan verifikasi saldo awal terlebih dahulu", 409)
    return result


async def chart(session):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    for code, name, kind, group in CHART:
        await session.execute(insert(f.KeuCoa).values(id=f"coa:{code}", kode=code, nama=name, jenis=kind,
            kelompok=group, sistem=True).on_conflict_do_nothing(index_elements=["kode"]))
    accounts = (await session.execute(select(m.KeuAkun).order_by(m.KeuAkun.id))).scalars().all()
    for account in accounts:
        await session.execute(insert(f.KeuCoa).values(id="kas:"+sha256(account.id.encode()).hexdigest()[:40], kode=f"KAS:{account.kode[:40]}:"+sha256(account.id.encode()).hexdigest()[:12],
            nama=account.nama, jenis="aset", kelompok="kas", kas_akun_id=account.id, sistem=True)
            .on_conflict_do_update(index_elements=["kas_akun_id"], set_={"nama": account.nama}))
    await session.flush()


async def coa(session, code):
    row = (await session.execute(select(f.KeuCoa).where(f.KeuCoa.kode == code))).scalar_one_or_none()
    if row is None or not row.aktif:
        svc.bad("Akun bagan akun tidak tersedia", 409)
    return row.id


async def cash_coa(session, key, *, historical=False):
    account = await svc.get(session, m.KeuAkun, key, lock=True)
    if not account.aktif and not historical:
        svc.bad("Akun kas tidak aktif", 409)
    await chart(session)
    return (await session.execute(select(f.KeuCoa.id).where(f.KeuCoa.kas_akun_id == key))).scalar_one()


def line(coa_id, amount, *, arus=None, pesanan_id=None, vendor_id=None, produk_id=None):
    return dict(coa_id=coa_id, debet=max(amount, ZERO), kredit=max(-amount, ZERO), arus=arus,
                pesanan_id=pesanan_id, vendor_id=vendor_id, produk_id=produk_id)


def visible():
    # Exclude the whole balanced entry, never one leg alone, when an external keu record is cancelled.
    return and_(f.KeuJurnal.status == "terkirim",
        or_(f.KeuJurnal.transaksi_id.is_(None), f.KeuJurnal.transaksi_id.in_(select(m.KeuTransaksi.id).where(m.KeuTransaksi.status == "terkirim"))),
        or_(f.KeuJurnal.settlement_id.is_(None), f.KeuJurnal.settlement_id.in_(select(m.KeuSettlement.id).where(m.KeuSettlement.status == "terkirim", ~svc.cancelled_settlement()))),
        or_(f.KeuJurnal.pesanan_id.is_(None), f.KeuJurnal.pesanan_id.in_(select(m.KeuPesanan.id).where(m.KeuPesanan.status != "batal"))))


async def detail(session, journal):
    rows = (await session.execute(select(f.KeuJurnalBaris).where(f.KeuJurnalBaris.jurnal_id == journal.id)
        .order_by(f.KeuJurnalBaris.nomor))).scalars().all()
    return {**svc.record(journal), "baris": [svc.record(row) for row in rows]}


async def post(session, user, key, kind, day, lines, note="", *, rincian=None, transaksi_id=None, settlement_id=None, pesanan_id=None, historical=False):
    await lock(session)
    settings = await book(session)
    if day < settings.tanggal_awal:
        svc.bad("Tanggal jurnal mendahului tanggal awal buku besar", 409)
    if historical and (settings.status != "migrasi" or user.role != "owner"):
        svc.bad("Migrasi histori hanya dapat dilakukan saat aktivasi owner", 403)
    if not historical:
        await pastikan_bulan_terbuka(session, day)
    values = [row for row in lines if row["debet"] or row["kredit"]]
    try:
        validator = TypeAdapter(NonNegativeMoney)
        for row in values:
            validator.validate_python(row["debet"])
            validator.validate_python(row["kredit"])
    except ValidationError:
        svc.bad("Nilai jurnal melampaui presisi nominal yang didukung", 422)
    if (len(values) < 2 and not (kind == "stok_keluar" and not values)) or sum(row["debet"] for row in values) != sum(row["kredit"] for row in values):
        svc.bad("Jurnal tidak seimbang atau kosong", 409)
    existing = (await session.execute(select(f.KeuJurnal).where(f.KeuJurnal.sumber_key == key).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if existing:
        current = await detail(session, existing)
        expected = [dict(nomor=i, **row) for i, row in enumerate(values, 1)]
        actual = [{k: row[k] for k in expected[0]} for row in current["baris"]] if expected else []
        for row in actual:
            row["debet"], row["kredit"] = Decimal(row["debet"]), Decimal(row["kredit"])
        if (existing.tanggal, existing.jenis, existing.keterangan, existing.rincian) != (day, kind, note, rincian or {}) or actual != expected:
            svc.bad("Referensi jurnal sudah digunakan dengan data berbeda", 409)
        return current  # Cancelled references never resurrect on a retry.
    journal = f.KeuJurnal(sumber_key=key, jenis=kind, tanggal=day, keterangan=note, rincian=rincian or {},
        transaksi_id=transaksi_id, settlement_id=settlement_id, pesanan_id=pesanan_id, dibuat_oleh=user.id, created_at=datetime.now(timezone.utc))
    session.add(journal)
    await session.flush()
    for number, values in enumerate(values, 1):
        account = await svc.get(session, f.KeuCoa, values["coa_id"])
        if not account.aktif and not historical:
            svc.bad("Bagan akun tidak aktif", 409)
        if account.kas_akun_id:
            await cash_coa(session, account.kas_akun_id, historical=historical)
            if kind != "pembukaan" and values["arus"] is None:
                svc.bad("Baris kas harus memiliki kelompok arus kas")
        elif values["arus"] is not None:
            svc.bad("Kelompok arus kas hanya untuk baris kas/bank")
        if account.kelompok == "utang_vendor":
            if not values["vendor_id"]:
                svc.bad("Akun utang vendor memerlukan vendor")
            vendor = await svc.get(session, m.KeuVendor, values["vendor_id"])
            if not vendor.aktif and not historical:
                svc.bad("Vendor tidak aktif")
        elif values["vendor_id"]:
            svc.bad("Vendor hanya diperbolehkan pada akun utang vendor")
        if account.kelompok == "persediaan" and not values["produk_id"]:
            svc.bad("Akun persediaan memerlukan produk dan mutasi stok")
        session.add(f.KeuJurnalBaris(jurnal_id=journal.id, nomor=number, **values))
    await session.flush()
    journal.status = "terkirim"
    await session.flush()
    await svc.audit(session, user, journal, "posting-buku-besar")
    return await detail(session, journal)


async def transfer(session, user, payload):
    await lock(session)
    await book(session)
    for key in sorted([payload.akun_asal_id, payload.akun_tujuan_id]):
        await svc.get(session, m.KeuAkun, key, lock=True)
    source = await cash_coa(session, payload.akun_asal_id)
    target = await cash_coa(session, payload.akun_tujuan_id)
    lines = [line(source, -payload.nominal, arus="mutasi"), line(target, payload.nominal, arus="mutasi")]
    if payload.biaya_admin:
        lines.extend([line(source, -payload.biaya_admin, arus="operasional"),
                      line(await coa(session, "BEBAN-ADMIN-BANK"), payload.biaya_admin)])
    return await post(session, user, f"mutasi:{payload.referensi}", "mutasi", payload.tanggal, lines,
                      payload.keterangan, rincian=snapshot(payload))


async def manual(session, user, payload):
    rows = []
    for values in payload.baris:
        account = await svc.get(session, f.KeuCoa, values.coa_id)
        if account.kelompok in {"persediaan", "piutang_pengiriman", "piutang_escrow", "hpp_stok", "hpp_vendor", "utang_vendor"}:
            svc.bad("Gunakan modul piutang/produksi/persediaan untuk akun tersebut")
        rows.append(line(values.coa_id, values.debet-values.kredit, arus=values.arus, vendor_id=values.vendor_id))
    return await post(session, user, f"manual:{payload.referensi}", "manual", payload.tanggal, rows, payload.keterangan)


async def balances(session, until, *, before=False):
    query = select(f.KeuJurnalBaris.coa_id, func.sum(f.KeuJurnalBaris.debet-f.KeuJurnalBaris.kredit)).join(
        f.KeuJurnal, f.KeuJurnal.id == f.KeuJurnalBaris.jurnal_id).where(visible(),
        f.KeuJurnal.tanggal < until if before else f.KeuJurnal.tanggal <= until).group_by(f.KeuJurnalBaris.coa_id)
    return dict((await session.execute(query)).all())


async def reports(session, start, end):
    await book(session)
    if start > end:
        svc.bad("Tanggal awal tidak boleh melebihi tanggal akhir")
    chart_rows = (await session.execute(select(f.KeuCoa).order_by(f.KeuCoa.kode))).scalars().all()
    opening, closing = await balances(session, start, before=True), await balances(session, end)
    def rows(kinds, values):
        result = []
        for a in chart_rows:
            if a.jenis in kinds:
                value = values.get(a.id, ZERO) * (-1 if a.jenis in {"kewajiban", "ekuitas", "pendapatan"} else 1)
                result.append({"id": a.id, "kode": a.kode, "nama": a.nama, "jenis": a.jenis, "kelompok": a.kelompok, "nilai": str(value)})
        return result
    movement = {a.id: closing.get(a.id, ZERO)-opening.get(a.id, ZERO) for a in chart_rows}
    income, expenses = rows({"pendapatan"}, movement), rows({"beban"}, movement)
    revenue = sum((Decimal(r["nilai"]) for r in income), ZERO)
    hpp = sum((Decimal(r["nilai"]) for r in expenses if r["kelompok"].startswith("hpp_")), ZERO)
    operating = sum((Decimal(r["nilai"]) for r in expenses if not r["kelompok"].startswith("hpp_")), ZERO)
    assets, debts, equity = rows({"aset"}, closing), rows({"kewajiban"}, closing), rows({"ekuitas"}, closing)
    retained = -sum((closing.get(a.id, ZERO) for a in chart_rows if a.jenis in {"pendapatan", "beban"}), ZERO)
    previous_profit = -sum((opening.get(a.id, ZERO) for a in chart_rows if a.jenis in {"pendapatan", "beban"}), ZERO)
    equity.extend([{"nama": "SHU periode", "kelompok": "shu", "nilai": str(revenue-hpp-operating)},
                   {"nama": "SHU periode sebelumnya", "kelompok": "laba_ditahan", "nilai": str(previous_profit)}])
    total_assets = sum((Decimal(r["nilai"]) for r in assets), ZERO)
    total_debts = sum((Decimal(r["nilai"]) for r in debts), ZERO)
    total_equity = sum((Decimal(r["nilai"]) for r in equity), ZERO)
    difference = total_assets-total_debts-total_equity
    if difference != ZERO or retained != previous_profit+revenue-hpp-operating:
        svc.bad("Buku besar tidak seimbang; periksa integritas jurnal sebelum menerbitkan laporan", 409)
    cash_rows = (await session.execute(select(f.KeuJurnalBaris.arus,
        func.sum(f.KeuJurnalBaris.debet), func.sum(f.KeuJurnalBaris.kredit)).join(f.KeuJurnal,
        f.KeuJurnal.id == f.KeuJurnalBaris.jurnal_id).join(f.KeuCoa, f.KeuCoa.id == f.KeuJurnalBaris.coa_id).where(
        visible(), f.KeuCoa.kas_akun_id.is_not(None), f.KeuJurnal.jenis != "pembukaan",
        f.KeuJurnal.tanggal >= start, f.KeuJurnal.tanggal <= end).group_by(f.KeuJurnalBaris.arus))).all()
    flows = {kind: {"masuk": str(debit), "keluar": str(credit), "neto": str(debit-credit)} for kind, debit, credit in cash_rows}
    for kind in ("operasional", "investasi", "pendanaan", "mutasi"):
        flows.setdefault(kind, {"masuk": "0", "keluar": "0", "neto": "0"})
    opening_cash = sum((opening.get(a.id, ZERO) for a in chart_rows if a.kas_akun_id), ZERO)
    closing_cash = sum((closing.get(a.id, ZERO) for a in chart_rows if a.kas_akun_id), ZERO)
    opening_in_range = (await session.execute(select(func.coalesce(func.sum(f.KeuJurnalBaris.debet-f.KeuJurnalBaris.kredit), 0)).join(
        f.KeuJurnal, f.KeuJurnal.id == f.KeuJurnalBaris.jurnal_id).join(f.KeuCoa, f.KeuCoa.id == f.KeuJurnalBaris.coa_id).where(
        visible(), f.KeuCoa.kas_akun_id.is_not(None), f.KeuJurnal.jenis == "pembukaan", f.KeuJurnal.tanggal >= start,
        f.KeuJurnal.tanggal <= end))).scalar_one()
    opening_cash += opening_in_range
    if closing_cash-opening_cash != sum((Decimal(value["neto"]) for value in flows.values()), ZERO):
        svc.bad("Arus kas tidak merekonsiliasi saldo kas", 409)
    return {"tanggal_awal": start.isoformat(), "tanggal_akhir": end.isoformat(),
        "laba_rugi": {"pendapatan": income, "beban": expenses, "pendapatan_bersih": str(revenue), "hpp": str(hpp), "beban_operasional": str(operating), "shu": str(revenue-hpp-operating)},
        "neraca": {"aset": assets, "kewajiban": debts, "ekuitas": equity, "total_aset": str(total_assets), "total_kewajiban": str(total_debts), "total_ekuitas": str(total_equity), "selisih": str(difference), "saldo_awal": svc.json_value(opening)},
        "arus_kas": {"kelompok": flows, "saldo_awal": str(opening_cash), "saldo_akhir": str(closing_cash)}}


async def cancel(session, user, journal, reason):
    if journal.status == "dibatalkan":
        return await detail(session, journal)
    if journal.status != "terkirim":
        svc.bad("Hanya jurnal terposting yang dapat dibatalkan", 409)
    await pastikan_bulan_terbuka(session, journal.tanggal)
    before = svc.record(journal)
    journal.status, journal.alasan_batal = "dibatalkan", reason
    journal.dibatalkan_oleh, journal.dibatalkan_at = user.id, datetime.now(timezone.utc)
    await session.flush()
    await svc.audit(session, user, journal, "unpost-buku-besar", before, reason)
    return await detail(session, journal)


async def guard_cancel(session, journal):
    from . import keu_inventory, keu_accounting
    await keu_inventory.cancellation_guard(session, journal)
    rows = (await session.execute(select(f.KeuJurnalBaris).where(f.KeuJurnalBaris.jurnal_id == journal.id))).scalars().all()
    vendor_credits = {}
    for row in rows:
        if row.vendor_id:
            vendor_credits[row.vendor_id] = vendor_credits.get(row.vendor_id, ZERO)+row.kredit-row.debet
    for vendor_id, credit in vendor_credits.items():
        if credit > await keu_accounting.vendor_debt(session, vendor_id):
            svc.bad("Utang sudah dibayar; batalkan pembayaran vendor terkait terlebih dahulu", 409)
    if journal.jenis in {"penjualan", "escrow"}:
        settled = (await session.execute(select(f.KeuJurnal.id).join(f.KeuJurnalBaris,
            f.KeuJurnalBaris.jurnal_id == f.KeuJurnal.id).where(f.KeuJurnal.status == "terkirim",
            f.KeuJurnal.jenis == "settlement", f.KeuJurnalBaris.pesanan_id == journal.pesanan_id).limit(1))).first()
        if settled:
            svc.bad("Piutang sudah dicairkan; batalkan settlement terkait terlebih dahulu", 409)


async def cancel_references(session, user, row, reason):
    column = f.KeuJurnal.transaksi_id if isinstance(row, m.KeuTransaksi) else f.KeuJurnal.settlement_id
    journals = (await session.execute(select(f.KeuJurnal).where(column == row.id).with_for_update()
        .execution_options(populate_existing=True))).scalars().all()
    for journal in journals:
        if journal.status == "terkirim":
            await cancel(session, user, journal, reason)


async def cancel_order(session, user, key, reason):
    journals = (await session.execute(select(f.KeuJurnal).where(f.KeuJurnal.pesanan_id == key,
        f.KeuJurnal.status == "terkirim").order_by(f.KeuJurnal.id).with_for_update())).scalars().all()
    for journal in journals:
        await guard_cancel(session, journal)
        await pastikan_bulan_terbuka(session, journal.tanggal)
    for journal in journals:
        await cancel(session, user, journal, reason)


async def unpost(session, user, key, payload):
    await lock(session)
    journal = await svc.get(session, f.KeuJurnal, key, lock=True)
    if journal.status == "dibatalkan":
        return await detail(session, journal)
    if journal.jenis == "pembukaan":
        svc.bad("Saldo awal tidak dapat di-unpost; gunakan jurnal koreksi modal yang seimbang", 409)
    if journal.transaksi_id:
        await svc.unpost_transaction(session, user, journal.transaksi_id, payload)
    elif journal.settlement_id:
        await svc.unpost_settlement(session, user, journal.settlement_id, payload)
    elif journal.jenis == "penjualan":
        await cancel_order(session, user, journal.pesanan_id, payload.alasan)
    else:
        await guard_cancel(session, journal)
        await cancel(session, user, journal, payload.alasan)
    return await detail(session, journal)
