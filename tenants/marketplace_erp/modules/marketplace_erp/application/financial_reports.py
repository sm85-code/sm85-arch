"""Monday–Sunday reports published Tuesday; calendar-month financial projection. Reads ERP; writes only its own baseline/snapshots/audit."""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..infrastructure.models import (
    AkunMarketplace,
    IklanHarianToko,
    Pesanan,
    Produk,
    Settlement,
    StokReservasi,
    LaporanKeuanganAwal,
    LaporanKeuanganAsal,
    LaporanKeuanganTutup,
    LaporanKeuanganAudit,
)

WIB = ZoneInfo("Asia/Jakarta")
ZERO = Decimal("0")


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class Balances(StrictInput):
    kas_utama: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    stok: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    kas_operasional: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    iklan_prabayar: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    piutang_pengiriman: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    saldo_toko: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    kas_gaji: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    utang_gaji: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    utang_usaha: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    modal: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    saldo_laba: Decimal = Field(max_digits=20, decimal_places=2)


class Opening(StrictInput):
    tanggal: date
    saldo: Balances


CASH_FIELDS = (
    "penarikan_lain",
    "pendapatan_produk",
    "jasa_proses",
    "jasa_cat_packing",
    "jasa_cat",
    "bayar_tukang",
    "pembelian",
    "operasional",
    "langganan",
    "gaji",
    "topup_iklan",
    "transfer_kas_ops",
    "transfer_kas_gaji",
    "bayar_gaji",
    "transfer_ke_ops_packing",
    "tambahan_modal",
)



class DatedTransaction(StrictInput):
    tanggal: date
    jenis: Literal[
        "penarikan_lain",
        "pendapatan_produk",
        "jasa_proses",
        "jasa_cat_packing",
        "jasa_cat",
        "bayar_tukang",
        "pembelian",
        "operasional",
        "langganan",
        "gaji",
        "topup_iklan",
        "transfer_kas_ops",
        "transfer_kas_gaji",
        "bayar_gaji",
            "transfer_ke_ops_packing",
        "tambahan_modal",
        "biaya_iklan",
    ]
    nominal: Decimal = Field(ge=0, max_digits=20, decimal_places=2)


class CloseInput(StrictInput):
    tanggal: date
    transaksi: list[DatedTransaction] = Field(default_factory=list, max_length=5000)
    tanggal_settlement: dict[str, date] = Field(default_factory=dict)
    settlement_ids: list[str] = Field(default_factory=list, max_length=1000)
    penarikan_lain: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    pendapatan_produk: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    jasa_proses: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    jasa_cat_packing: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    jasa_cat: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    bayar_tukang: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    pembelian: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    operasional: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    langganan: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    gaji: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    biaya_iklan: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    topup_iklan: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    transfer_kas_ops: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    transfer_kas_gaji: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    bayar_gaji: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    transfer_ke_ops_packing: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    tambahan_modal: Decimal = Field(default=0, ge=0, max_digits=20, decimal_places=2)
    piutang_pengiriman: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    saldo_toko: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    stok: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    utang_usaha: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    catatan: str = Field(min_length=3, max_length=4000)
    sumber_diperiksa: bool = False
    preview_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ReopenInput(StrictInput):
    alasan: str = Field(min_length=5, max_length=2000)


def month_bounds(month: str):
    try:
        year, number = map(int, month.split("-"))
        if len(month) != 7 or not 2000 <= year <= 2100:
            raise ValueError
        last = calendar.monthrange(year, number)[1]
    except (ValueError, TypeError):
        raise HTTPException(422, "Periode harus YYYY-MM (2000–2100)")
    return date(year, number, 1), date(year, number, last)


def week_bounds(day):
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def next_presentation(day):
    # Publication follows the completed 23:59 cut-off, never earlier on the same day.
    return day + timedelta(days=(1 - day.weekday()) % 7 or 7)


def next_cutoff(day):
    sunday = day + timedelta(days=(6 - day.weekday()) % 7 or 7)
    month_end = month_bounds(day.strftime("%Y-%m"))[1]
    if day == month_end:
        month_end = month_bounds((day + timedelta(days=1)).strftime("%Y-%m"))[1]
    return min(sunday, month_end)


def normalize_transactions(p, previous):
    sums = {k: ZERO for k in CASH_FIELDS}
    ad_override = None
    for t in p.transaksi:
        if not previous < t.tanggal <= p.tanggal:
            raise HTTPException(422, "Tanggal transaksi harus berada dalam rentang cut-off yang ditutup")
        if t.jenis == "biaya_iklan":
            ad_override = (ad_override or ZERO) + t.nominal
        else:
            sums[t.jenis] += t.nominal
    for k, value in sums.items():
        if getattr(p, k) not in (ZERO, value):
            raise HTTPException(422, f"Nominal {k} harus berasal dari transaksi bertanggal")
    if p.biaya_iklan is not None and p.biaya_iklan != ad_override:
        raise HTTPException(422, "Koreksi iklan harus berasal dari transaksi bertanggal")
    if set(p.tanggal_settlement) != set(p.settlement_ids):
        raise HTTPException(422, "Isi tanggal kas masuk aktual untuk setiap settlement terpilih")
    if any(not previous < day <= p.tanggal for day in p.tanggal_settlement.values()):
        raise HTTPException(422, "Tanggal penarikan settlement di luar rentang cut-off")
    return p.model_copy(update={**sums, "biaya_iklan": ad_override})


def totals(b):
    assets = sum(
        (
            b.get(k, ZERO)
            for k in (
                "kas_utama",
                "stok",
                "kas_operasional",
                "iklan_prabayar",
                "piutang_pengiriman",
                "saldo_toko",
                "kas_gaji",
            )
        ),
        ZERO,
    )
    debts = b["utang_gaji"] + b["utang_usaha"]
    equity = b["modal"] + b["saldo_laba"]
    return {"aset": assets, "utang": debts, "ekuitas": equity, "selisih": assets - debts - equity}


def calculate(before: Balances, p: CloseInput, payouts: Decimal, ads: Decimal, legacy: bool = False):
    """One period's figures. The salary set aside into the salary cash account leaves the main cash, so it is a cash-out
    (`kas_keluar_gaji`); the packing funds are moved by transfer to the operational cash account (the packing one). The main
    cash keeps the general operating funds and the profit. `legacy` keeps the older way of listing the
    set-aside salary (a transfer between cash accounts, not a cash-out), which the Drive import still follows; balances are the same."""
    b = before.model_dump()
    withdrawals = payouts + p.penarikan_lain
    other_income = p.pendapatan_produk + p.jasa_proses + p.jasa_cat_packing + p.jasa_cat
    delta = p.piutang_pengiriman + p.saldo_toko - b["piutang_pengiriman"] - b["saldo_toko"]
    # Inventory/credit purchases cannot be expensed twice or disappear from the balance sheet.
    stock_adjustment = b["stok"] - p.stok
    payable_adjustment = p.utang_usaha - b["utang_usaha"]
    hpp = p.bayar_tukang + p.pembelian + stock_adjustment + payable_adjustment
    expense_ads = p.biaya_iklan if p.biaya_iklan is not None else ads
    income = withdrawals + other_income + delta
    expenses = hpp + p.operasional + p.langganan + p.gaji + expense_ads
    cash_out_other = p.bayar_tukang + p.pembelian + p.operasional + p.langganan + p.topup_iklan
    set_aside = p.transfer_kas_gaji
    salary_cash = ZERO if legacy else set_aside
    cash_out = cash_out_other + salary_cash
    to_packing = p.transfer_ke_ops_packing
    cash_in = withdrawals + other_income
    profit = income - expenses
    end = {
        "kas_utama": b["kas_utama"] + cash_in - cash_out_other - set_aside + p.transfer_kas_ops - to_packing + p.tambahan_modal,
        "stok": p.stok,
        "kas_operasional": b["kas_operasional"] - p.transfer_kas_ops + to_packing,
        "iklan_prabayar": b["iklan_prabayar"] + p.topup_iklan - expense_ads,
        "piutang_pengiriman": p.piutang_pengiriman,
        "saldo_toko": p.saldo_toko,
        "kas_gaji": b["kas_gaji"] + set_aside - p.bayar_gaji,
        "utang_gaji": b["utang_gaji"] + p.gaji - p.bayar_gaji,
        "utang_usaha": p.utang_usaha,
        "modal": b["modal"] + p.tambahan_modal,
        "saldo_laba": b["saldo_laba"] + profit,
    }
    return {
        "pemasukan_toko": withdrawals,
        "pendapatan_lain": other_income,
        "perubahan_piutang": delta,
        "pemasukan": income,
        "hpp": hpp,
        "penyesuaian_stok": stock_adjustment,
        "penyesuaian_utang": payable_adjustment,
        "biaya_iklan": expense_ads,
        "pengeluaran": expenses,
        "laba": profit,
        "kas_masuk": cash_in,
        "kas_keluar": cash_out,
        "kas_keluar_gaji": salary_cash,
        "transfer_antar_kas": p.transfer_kas_ops - (set_aside - salary_cash) - to_packing,
        "saldo_awal": b,
        "saldo_akhir": end,
        "neraca": totals(end),
    }


def serialize(value):
    return json.loads(json.dumps(value, default=lambda x: str(x) if isinstance(x, Decimal) else x.isoformat()))


async def baseline(session, cutoff):
    opening = await session.get(LaporanKeuanganAwal, "global")
    if not opening:
        raise HTTPException(409, "Isi saldo awal laporan terlebih dahulu")
    latest = await session.scalar(select(LaporanKeuanganTutup).order_by(LaporanKeuanganTutup.tanggal.desc()).limit(1))
    previous = latest.tanggal if latest else opening.tanggal
    if latest and json.loads(latest.snapshot_json).get("calendar_version") != 2:
        raise HTTPException(
            409, "Laporan lama memakai cut-off Selasa. Buka kembali laporan lama sebelum memakai kalender baru."
        )
    if cutoff != next_cutoff(previous):
        raise HTTPException(409, f"Cut-off berikutnya harus {next_cutoff(previous).isoformat()}")
    data = json.loads(latest.snapshot_json)["hasil"]["saldo_akhir"] if latest else json.loads(opening.saldo_json)
    return previous, Balances(**data)


async def sources(session, start, end):
    shops = dict((await session.execute(select(AkunMarketplace.id, AkunMarketplace.nama_toko))).all())
    settlements = (
        await session.scalars(
            select(Settlement).where(
                Settlement.status == "paid",
            )
        )
    ).all()
    ads = (
        await session.scalars(
            select(IklanHarianToko).where(IklanHarianToko.tanggal >= start, IklanHarianToko.tanggal <= end)
        )
    ).all()
    orders = (
        await session.execute(
            select(Pesanan.id, Pesanan.id_eksternal, Pesanan.akun_id, Pesanan.total).where(Pesanan.status == "shipped")
        )
    ).all()
    # Current operational data is evidence, never a fabricated historical balance/cost.
    reserved = (
        select(StokReservasi.produk_id, func.sum(StokReservasi.qty).label("qty"))
        .where(StokReservasi.status == "aktif")
        .group_by(StokReservasi.produk_id)
        .subquery()
    )
    inventory = (
        await session.execute(
            select(Produk.id, Produk.nama, Produk.stok, func.coalesce(reserved.c.qty, 0))
            .outerjoin(reserved, reserved.c.produk_id == Produk.id)
            .where(Produk.aktif.is_(True))
        )
    ).all()
    return {
        "settlements": [
            {
                "id": s.id,
                "toko": shops.get(s.akun_id, s.akun_id),
                "net": str(s.net),
                "periode_selesai": s.periode_selesai.isoformat(),
            }
            for s in settlements
        ],
        "iklan": [
            {
                "id": a.id,
                "toko": shops.get(a.akun_id, a.akun_id),
                "tanggal": a.tanggal.isoformat(),
                "expense": str(a.expense),
                "diambil_at": a.diambil_at.isoformat(),
            }
            for a in ads
        ],
        "biaya_iklan": str(sum((a.expense for a in ads), ZERO)),
        "pesanan_pengiriman": [
            {"id": o.id, "nomor": o.id_eksternal, "total_bruto": str(o.total), "toko": shops.get(o.akun_id, "—")}
            for o in orders
        ],
        "stok": [{"id": i, "nama": name, "tersedia": qty, "fisik": qty + active} for i, name, qty, active in inventory],
        "peringatan": [
            "Settlement paid harus dipastikan benar-benar masuk kas pada periode ini sebelum dipilih.",
            "Pesanan pengiriman dan stok adalah posisi ERP saat dibaca, bukan saldo historis. Konfirmasi nilai bersih dan biaya stok.",
            "Data iklan hanya mencakup hari yang sudah tersinkron; periksa kelengkapan sebelum tutup buku.",
        ],
    }


async def preview(session, p):
    previous, before = await baseline(session, p.tanggal)
    p = normalize_transactions(p, previous)
    origin = await session.get(LaporanKeuanganAsal, "global")
    if origin and not any(t.jenis == "biaya_iklan" for t in p.transaksi):
        raise HTTPException(422, "Isi pemakaian iklan buku utama sebagai transaksi bertanggal, termasuk 0 jika nihil")
    if origin and p.settlement_ids:
        raise HTTPException(422, "ERP hanya pembanding; isi penarikan dari buku utama sebagai transaksi bertanggal")
    source = await sources(session, previous + timedelta(days=1), p.tanggal)
    selected = set(p.settlement_ids)
    if len(selected) != len(p.settlement_ids) or not selected.issubset({s["id"] for s in source["settlements"]}):
        raise HTTPException(422, "Settlement duplikat atau tidak termasuk sumber periode ini")
    used = set()
    for raw in (await session.scalars(select(LaporanKeuanganTutup.snapshot_json))).all():
        used.update(json.loads(raw)["input"]["settlement_ids"])
    if selected & used:
        raise HTTPException(409, "Settlement sudah dipakai dalam laporan sebelumnya")
    payouts = sum((money(s["net"]) for s in source["settlements"] if s["id"] in selected), ZERO)
    source["settlements"] = [
        {**s, "tanggal_kas_masuk": p.tanggal_settlement[s["id"]].isoformat()}
        for s in source["settlements"]
        if s["id"] in selected
    ]
    result = calculate(before, p, payouts, ZERO if origin else money(source["biaya_iklan"]))
    if origin:
        source["utama"] = "google_drive"
        source["drive"] = json.loads(origin.asal_json)
        source["pembanding_erp"] = {
            "biaya_iklan": source["biaya_iklan"],
            "piutang_pengiriman_bruto": str(sum((money(o["total_bruto"]) for o in source["pesanan_pengiriman"]), ZERO)),
            "saldo_toko": None,
            "piutang_belum_ditarik": None,
            "catatan": "ERP tidak memisahkan saldo tertahan dan belum memiliki saldo gabungan historis terverifikasi. Total pengiriman adalah bruto, bukan estimasi dana bersih.",
        }
    else:
        source["utama"] = "erp"
    token = hashlib.sha256(
        json.dumps(
            serialize(
                {"before": before.model_dump(), "input": p.model_dump(exclude={"preview_token"}), "source": source}
            ),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return serialize(
        {
            "preview_token": token,
            "calendar_version": 2,
            "jadwal_laporan": next_presentation(p.tanggal),
            "tanggal": p.tanggal,
            "mulai": previous + timedelta(days=1),
            "tutup_bulanan": p.tanggal == month_bounds(p.tanggal.strftime("%Y-%m"))[1],
            "input": p.model_dump(),
            "sumber": source,
            "hasil": result,
        }
    )


async def close(session, p, actor):
    # Serialize all report writes on the baseline; leaves operational ERP records unlocked.
    await session.scalar(select(LaporanKeuanganAwal).where(LaporanKeuanganAwal.id == "global").with_for_update())
    if p.tanggal >= datetime.now(WIB).date():
        raise HTTPException(422, "Cut-off selesai pukul 23:59 WIB; simpan setelah tanggal cut-off berakhir")
    if not p.sumber_diperiksa:
        raise HTTPException(422, "Konfirmasi pemeriksaan sumber ERP dan saldo akhir")
    snapshot = await preview(session, p)
    if p.preview_token != snapshot["preview_token"]:
        raise HTTPException(409, "Data sumber atau input berubah; buat pratinjau lagi sebelum tutup buku")
    result = snapshot["hasil"]
    if any(money(v) < 0 for k, v in result["saldo_akhir"].items() if k != "saldo_laba"):
        raise HTTPException(422, "Saldo kas, aset atau utang menjadi negatif; periksa input")
    if money(result["neraca"]["selisih"]) != 0:
        raise HTTPException(422, "Neraca tidak seimbang")
    snapshot["created_by"] = actor.id
    snapshot["created_at"] = datetime.now(WIB).isoformat()
    text = json.dumps(snapshot)
    session.add(LaporanKeuanganTutup(tanggal=p.tanggal, snapshot_json=text, created_by=actor.id))
    session.add(
        LaporanKeuanganAudit(
            tanggal=p.tanggal, action="close", reason=p.catatan, snapshot_json=text, created_by=actor.id
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Tanggal ini sudah ditutup; muat ulang laporan")
    return snapshot


async def reopen(session, cutoff, reason, actor):
    await session.scalar(select(LaporanKeuanganAwal).where(LaporanKeuanganAwal.id == "global").with_for_update())
    latest = await session.scalar(select(LaporanKeuanganTutup).order_by(LaporanKeuanganTutup.tanggal.desc()).limit(1))
    if not latest or latest.tanggal != cutoff:
        raise HTTPException(409, "Hanya tutup buku paling akhir yang dapat dibuka kembali")
    session.add(
        LaporanKeuanganAudit(
            tanggal=cutoff, action="reopen", reason=reason, snapshot_json=latest.snapshot_json, created_by=actor.id
        )
    )
    await session.delete(latest)
    await session.commit()
    return {"tanggal": cutoff.isoformat(), "dibuka": True}


def aggregate(parts, start, end):
    """Combine disjoint snapshots; never prorate weekly money across months."""
    if not parts:
        return None
    parts = sorted(parts, key=lambda r: r["mulai"])
    result = {
        k: sum((money(p["hasil"].get(k, 0)) for p in parts), ZERO)
        for k in {key for p in parts for key in p["hasil"]}
        if k not in ("saldo_awal", "saldo_akhir", "neraca")
    }
    result["saldo_awal"] = parts[0]["hasil"]["saldo_awal"]
    result["saldo_akhir"] = parts[-1]["hasil"]["saldo_akhir"]
    result["neraca"] = parts[-1]["hasil"]["neraca"]
    inputs = dict(parts[-1]["input"])
    for k in CASH_FIELDS:
        inputs[k] = sum((money(p["input"].get(k, 0)) for p in parts), ZERO)
    inputs["tanggal"] = end.isoformat()
    inputs["settlement_ids"] = [sid for p in parts for sid in p["input"]["settlement_ids"]]
    inputs["tanggal_settlement"] = {
        sid: day for p in parts for sid, day in p["input"].get("tanggal_settlement", {}).items()
    }
    inputs["catatan"] = " | ".join(p["input"]["catatan"] for p in parts)
    inputs["transaksi"] = [t for p in parts for t in p["input"].get("transaksi", [])]
    src = dict(parts[-1]["sumber"])
    for k in ("settlements", "iklan"):
        src[k] = list({x["id"]: x for p in parts for x in p["sumber"][k]}.values())
    src["biaya_iklan"] = str(sum((money(p["sumber"]["biaya_iklan"]) for p in parts), ZERO))
    complete = parts[0]["mulai"] == start.isoformat() and parts[-1]["tanggal"] == end.isoformat()
    return serialize(
        {
            "calendar_version": 2,
            "tanggal": end,
            "mulai": start,
            "jadwal_laporan": next_presentation(end),
            "tutup_bulanan": end == month_bounds(end.strftime("%Y-%m"))[1],
            "lengkap": complete,
            "preview_token": "",
            "input": inputs,
            "sumber": src,
            "hasil": result,
            "created_at": parts[-1].get("created_at"),
            "bagian": [{"mulai": p["mulai"], "tanggal": p["tanggal"]} for p in parts],
        }
    )


async def overview(session, month):
    start, end = month_bounds(month)
    opening = await session.get(LaporanKeuanganAwal, "global")
    origin = await session.get(LaporanKeuanganAsal, "global")
    rows = (await session.scalars(select(LaporanKeuanganTutup).order_by(LaporanKeuanganTutup.tanggal))).all()
    valid = [json.loads(r.snapshot_json) for r in rows if json.loads(r.snapshot_json).get("calendar_version") == 2]
    monthly = [r for r in valid if start.isoformat() <= r["mulai"] and r["tanggal"] <= end.isoformat()]
    monthly_report = aggregate(monthly, start, end)
    weeks = []
    day = week_bounds(start)[0]
    while day <= end:
        last = day + timedelta(days=6)
        parts = [r for r in valid if day.isoformat() <= r["mulai"] and r["tanggal"] <= last.isoformat()]
        weeks.append(
            {
                "mulai": day.isoformat(),
                "tanggal": last.isoformat(),
                "jadwal_laporan": (last + timedelta(days=2)).isoformat(),
                "laporan": aggregate(parts, day, last),
            }
        )
        day += timedelta(days=7)
    previous = rows[-1].tanggal if rows else opening.tanggal if opening else None
    return {
        "sumber_utama": "google_drive" if origin else "erp",
        "asal": json.loads(origin.asal_json) if origin else None,
        "akhir_bulan": end.isoformat(),
        "jadwal_bulanan": next_presentation(end).isoformat(),
        "mingguan": weeks,
        "saldo_awal": {"tanggal": opening.tanggal.isoformat(), "saldo": json.loads(opening.saldo_json)}
        if opening
        else None,
        "laporan": monthly,
        "laporan_bulanan": monthly_report,
        "legacy": [
            json.loads(r.snapshot_json) for r in rows if json.loads(r.snapshot_json).get("calendar_version") != 2
        ],
        "berikutnya": next_cutoff(previous).isoformat() if previous else None,
        "terakhir": previous.isoformat() if rows else None,
        "bulanan": {
            "final": bool(monthly_report and monthly_report["lengkap"]),
            "pemasukan": str(monthly_report["hasil"]["pemasukan"]) if monthly_report else "0",
            "pengeluaran": str(monthly_report["hasil"]["pengeluaran"]) if monthly_report else "0",
            "laba": str(monthly_report["hasil"]["laba"]) if monthly_report else "0",
            "saldo_akhir": monthly_report["hasil"]["saldo_akhir"] if monthly_report else None,
        },
    }
