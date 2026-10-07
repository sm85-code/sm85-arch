"""Source-to-ledger bridge, receivables and vendor liabilities. Source databases remain read-only."""
from datetime import timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from . import keu_ledger as ledger, keu_services as svc, kategori_core as categories
from ..infrastructure import models_keu as m, models_keu_finance as f
from ..infrastructure.models import BlKategori

ZERO = Decimal("0")


async def order_balance(session, order_id, code):
    account = await ledger.coa(session, code)
    return (await session.execute(select(func.coalesce(func.sum(f.KeuJurnalBaris.debet-f.KeuJurnalBaris.kredit), 0)).join(
        f.KeuJurnal, f.KeuJurnal.id == f.KeuJurnalBaris.jurnal_id).where(ledger.visible(),
        f.KeuJurnalBaris.pesanan_id == order_id, f.KeuJurnalBaris.coa_id == account))).scalar_one()


async def receivable(session, user, order, position, day, reference, *, historical=False, provenance=None):
    if order.status == "batal" or order.total_sumber == ZERO:
        return None
    shipping = await order_balance(session, order.id, "PIUTANG-KIRIM")
    escrow = await order_balance(session, order.id, "PIUTANG-ESCROW")
    recognized = (await session.execute(select(f.KeuJurnal.id).where(f.KeuJurnal.pesanan_id == order.id,
        f.KeuJurnal.jenis == "penjualan", f.KeuJurnal.status == "terkirim").limit(1))).first()
    info = {"posisi": position, "provenance_tanggal": provenance or "tanggal_pesanan", "status_sumber": order.status_sumber,
            "tanggal_pesanan_asli": order.tanggal.isoformat(), "sumber_updated_at": svc.json_value(order.sumber_updated_at),
            "tanggal_jurnal": day.isoformat()}
    if recognized is None:
        key = reference if reference.startswith("rekonsiliasi:") else f"order:{order.id}:penjualan"
        return await ledger.post(session, user, key, "penjualan", day,
            [ledger.line(await ledger.coa(session, "PIUTANG-KIRIM" if position == "pengiriman" else "PIUTANG-ESCROW"), order.total_sumber, pesanan_id=order.id),
             ledger.line(await ledger.coa(session, "PENDAPATAN"), -order.total_sumber, pesanan_id=order.id)],
            f"Penjualan {order.nomor}", rincian=info, pesanan_id=order.id, historical=historical)
    if position == "escrow" and shipping > ZERO:
        return await ledger.post(session, user, reference, "escrow", day,
            [ledger.line(await ledger.coa(session, "PIUTANG-KIRIM"), -shipping, pesanan_id=order.id),
             ledger.line(await ledger.coa(session, "PIUTANG-ESCROW"), shipping, pesanan_id=order.id)],
            f"Pesanan selesai {order.nomor}", rincian=info, pesanan_id=order.id, historical=historical)
    if position == "pengiriman" and escrow > ZERO:
        svc.bad("Piutang escrow tidak dapat otomatis mundur ke pengiriman", 409)
    return None


async def source_order(session, user, order, *, first=False, historical=False):
    settings = await ledger.book(session, required=False)
    if settings is None or order.status == "batal":
        return
    position = {"shipped": "pengiriman", "dikirim": "pengiriman", "completed": "escrow", "selesai": "escrow"}.get(order.status_sumber)
    if position is None:
        return
    day = order.tanggal
    provenance = "tanggal_pesanan_backfill" if first else "tanggal_pesanan"
    if position == "escrow" and not first and order.sumber_updated_at:
        stamp = order.sumber_updated_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        day = stamp.astimezone(ZoneInfo("Asia/Jakarta")).date()
        provenance = "sumber_updated_at"
    cancelled = (await session.execute(select(f.KeuJurnal.id).where(f.KeuJurnal.sumber_key == f"order:{order.id}:penjualan", f.KeuJurnal.status == "dibatalkan").limit(1))).first()
    if cancelled:
        return
    await receivable(session, user, order, position, day, f"order:{order.id}:escrow", historical=historical, provenance=provenance)


async def cash_transaction(session, user, tx, *, historical=False):
    if await ledger.book(session, required=False) is None or tx.status != "terkirim" or tx.settlement_id:
        return
    await ledger.chart(session)
    mapping = await session.get(f.KeuKategoriCoa, tx.kategori_id)
    category = await svc.get(session, BlKategori, tx.kategori_id)
    defaults = {
        categories.KATEGORI_SETORAN_MODAL: ("MODAL", "pendanaan"),
        categories.KATEGORI_PRIVE: ("DISTRIBUSI", "pendanaan"),
        categories.KATEGORI_BAGI_HASIL: ("DISTRIBUSI", "pendanaan"),
        categories.KATEGORI_GAJI: ("BEBAN-GAJI", "operasional"),
        categories.KATEGORI_BIAYA_IKLAN: ("BEBAN-IKLAN", "operasional"),
        categories.KATEGORI_PRODUKSI: ("HPP-MANUAL", "operasional"),
        categories.KATEGORI_TAGIHAN: ("BEBAN-LANGGANAN", "operasional"),
        categories.KATEGORI_BIAYA_MARKETPLACE: ("BEBAN-MARKETPLACE", "operasional"),
        categories.KATEGORI_PENJUALAN_MARKETPLACE: ("PENDAPATAN", "operasional"),
        categories.KATEGORI_PENJUALAN_WEB: ("PENDAPATAN", "operasional"),
        categories.KATEGORI_RESELLER: ("PENDAPATAN", "operasional"),
    }
    code, default_flow = defaults.get(category.nama, ("PENDAPATAN-LAIN" if tx.jenis == "masuk" else "BEBAN-OPERASIONAL", "operasional"))
    counterpart = mapping.coa_id if mapping else await ledger.coa(session, code)
    arus = mapping.arus if mapping else default_flow
    amount = tx.jumlah if tx.jenis == "masuk" else -tx.jumlah
    await ledger.post(session, user, f"kas:{tx.id}", "kas", tx.tanggal,
        [ledger.line(await ledger.cash_coa(session, tx.akun_id, historical=historical), amount, arus=arus), ledger.line(counterpart, -amount)],
        tx.keterangan, rincian={"kategori_asli": category.nama, "pemetaan": "kategori" if mapping else "kategori_asli" if category.nama in defaults else "default_operasional"},
        transaksi_id=tx.id, historical=historical)


async def settlement(session, user, row, *, historical=False):
    if await ledger.book(session, required=False) is None or row.status != "terkirim":
        return
    allocations = (await session.execute(select(m.KeuAlokasiSettlement.jumlah, m.KeuItem.pesanan_id).join(
        m.KeuItem, m.KeuItem.id == m.KeuAlokasiSettlement.item_id).where(m.KeuAlokasiSettlement.settlement_id == row.id))).all()
    weights = {}
    for amount, order_id in allocations:
        weights[order_id] = weights.get(order_id, ZERO)+abs(amount)
    if not weights:
        svc.bad("Settlement memerlukan alokasi pesanan untuk pelunasan piutang", 409)
    if not sum(weights.values(), ZERO):
        for order_id in weights:
            order = await svc.get(session, m.KeuPesanan, order_id)
            weights[order_id] = order.total_sumber
    if not sum(weights.values(), ZERO) and row.bruto:
        svc.bad("Settlement tidak memiliki dasar pembagian bruto", 409)
    total_weight, allocated = sum(weights.values(), ZERO), ZERO
    lines = []
    for index, order_id in enumerate(sorted(weights)):
        order = await svc.get(session, m.KeuPesanan, order_id, lock=True)
        await receivable(session, user, order, "escrow", order.tanggal, f"order:{order.id}:escrow",
                         historical=historical, provenance="tanggal_pesanan_bukti_settlement")
        amount = row.bruto-allocated if index == len(weights)-1 else (row.bruto*weights[order_id]/total_weight).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if total_weight else ZERO
        if amount > await order_balance(session, order_id, "PIUTANG-ESCROW"):
            svc.bad("Bruto settlement melebihi piutang escrow; periksa alokasi/potongan", 409)
        allocated += amount
        lines.append(ledger.line(await ledger.coa(session, "PIUTANG-ESCROW"), -amount, pesanan_id=order_id))
    tx = (await session.execute(select(m.KeuTransaksi).where(m.KeuTransaksi.settlement_id == row.id))).scalar_one_or_none()
    if row.neto:
        if tx is None or tx.status != "terkirim":
            svc.bad("Transaksi kas settlement belum terposting", 409)
        lines.append(ledger.line(await ledger.cash_coa(session, tx.akun_id, historical=historical), row.neto, arus="operasional"))
    if row.potongan:
        lines.append(ledger.line(await ledger.coa(session, "BEBAN-MARKETPLACE"), row.potongan))
    if row.penyesuaian:
        lines.append(ledger.line(await ledger.coa(session, "PENDAPATAN-LAIN" if row.penyesuaian > ZERO else "BEBAN-MARKETPLACE"), -row.penyesuaian))
    if any(r["debet"] or r["kredit"] for r in lines):
        await ledger.post(session, user, f"settlement:{row.id}", "settlement", row.tanggal_cair, lines,
            f"Settlement {row.sumber_ref}", rincian={"bruto": str(row.bruto), "potongan": str(row.potongan), "penyesuaian": str(row.penyesuaian),
            "pembagian_bruto": "proporsional_alokasi_neto; nol_memakai_total_pesanan"}, settlement_id=row.id, historical=historical)


async def vendor_hpp(session, user, order, *, historical=False):
    if await ledger.book(session, required=False) is None or order.status != "selesai":
        return
    allocations = (await session.execute(select(m.KeuAlokasiVendor).join(m.KeuItem, m.KeuItem.id == m.KeuAlokasiVendor.item_id).where(
        m.KeuItem.pesanan_id == order.id, m.KeuAlokasiVendor.dibatalkan.is_(False)).order_by(m.KeuAlokasiVendor.id))).scalars().all()
    amounts = {}
    for allocation in allocations:
        amounts[allocation.vendor_id] = amounts.get(allocation.vendor_id, ZERO)+allocation.qty*allocation.biaya_satuan
    total = sum(amounts.values(), ZERO)
    if total:
        lines = [ledger.line(await ledger.coa(session, "HPP-VENDOR"), total, pesanan_id=order.id)]
        debt_account = await ledger.coa(session, "UTANG-VENDOR")
        lines.extend(ledger.line(debt_account, -amount, vendor_id=vendor_id, pesanan_id=order.id)
                     for vendor_id, amount in sorted(amounts.items()) if amount)
        await ledger.post(session, user, f"order:{order.id}:hpp-vendor", "hpp_vendor", order.tanggal, lines,
                          f"HPP vendor {order.nomor}", pesanan_id=order.id, historical=historical)


async def vendor_debt(session, vendor_id):
    return -(await session.execute(select(func.coalesce(func.sum(f.KeuJurnalBaris.debet-f.KeuJurnalBaris.kredit), 0)).join(
        f.KeuJurnal, f.KeuJurnal.id == f.KeuJurnalBaris.jurnal_id).where(ledger.visible(),
        f.KeuJurnalBaris.vendor_id == vendor_id, f.KeuJurnalBaris.coa_id == await ledger.coa(session, "UTANG-VENDOR")))).scalar_one()


async def pay_vendor(session, user, payload):
    await ledger.lock(session)
    await ledger.book(session)
    existing = (await session.execute(select(f.KeuJurnal).where(f.KeuJurnal.sumber_key == f"vendor-bayar:{payload.referensi}"))).scalar_one_or_none()
    if existing:
        if existing.rincian != ledger.snapshot(payload):
            svc.bad("Referensi pembayaran vendor sudah digunakan", 409)
        return await ledger.detail(session, existing)
    if payload.jumlah > await vendor_debt(session, payload.vendor_id):
        svc.bad("Pembayaran melebihi utang vendor", 409)
    return await ledger.post(session, user, f"vendor-bayar:{payload.referensi}", "vendor_bayar", payload.tanggal,
        [ledger.line(await ledger.coa(session, "UTANG-VENDOR"), payload.jumlah, vendor_id=payload.vendor_id),
         ledger.line(await ledger.cash_coa(session, payload.akun_kas_id), -payload.jumlah, arus="operasional")],
        payload.keterangan, rincian=ledger.snapshot(payload))


async def initialize(session, user, payload):
    if user.role != "owner" or not user.aktif or user.must_change_password:
        svc.bad("Aktivasi dan migrasi histori hanya untuk owner", 403)
    await ledger.lock(session)
    existing = await ledger.book(session, required=False)
    if existing:
        return svc.record(existing)
    days = []
    for model, field in ((m.KeuPesanan, m.KeuPesanan.tanggal), (m.KeuTransaksi, m.KeuTransaksi.tanggal), (m.KeuSettlement, m.KeuSettlement.tanggal_cair)):
        value = (await session.execute(select(func.min(field)).select_from(model))).scalar_one()
        if value:
            days.append(value)
    first = min(days) if days else payload.tanggal_awal
    settings = f.KeuBuku(id="utama", tanggal_awal=first, metode_stok=payload.metode_stok,
                        tanggal_status=payload.tanggal_status, dibuat_oleh=user.id, status="migrasi")
    session.add(settings)
    await session.flush()
    await ledger.chart(session)
    accounts = (await session.execute(select(m.KeuAkun).order_by(m.KeuAkun.id))).scalars().all()
    opening = [ledger.line(await ledger.cash_coa(session, a.id, historical=True), a.saldo_awal) for a in accounts if a.saldo_awal]
    total = sum((a.saldo_awal for a in accounts), ZERO)
    if total:
        opening.append(ledger.line(await ledger.coa(session, "MODAL"), -total))
    if opening:
        await ledger.post(session, user, "saldo-awal:histori", "pembukaan", first, opening,
            "Saldo awal sebelum transaksi pertama", rincian={"provenance": "KeuAkun.saldo_awal; sebelum transaksi pertama"}, historical=True)
    orders = (await session.execute(select(m.KeuPesanan).where(m.KeuPesanan.status != "batal").order_by(m.KeuPesanan.tanggal, m.KeuPesanan.id))).scalars().all()
    for order in orders:
        await source_order(session, user, order, first=True, historical=True)
        await vendor_hpp(session, user, order, historical=True)
    transactions = (await session.execute(select(m.KeuTransaksi).where(m.KeuTransaksi.status == "terkirim").order_by(m.KeuTransaksi.tanggal, m.KeuTransaksi.id))).scalars().all()
    for tx in transactions:
        await cash_transaction(session, user, tx, historical=True)
    settlements = (await session.execute(select(m.KeuSettlement).where(m.KeuSettlement.status == "terkirim", ~svc.cancelled_settlement())
        .order_by(m.KeuSettlement.tanggal_cair, m.KeuSettlement.id))).scalars().all()
    for row in settlements:
        await settlement(session, user, row, historical=True)
    settings.status = "aktif"
    await session.flush()
    await svc.audit(session, user, settings, "aktivasi-migrasi-histori")
    return svc.record(settings)
