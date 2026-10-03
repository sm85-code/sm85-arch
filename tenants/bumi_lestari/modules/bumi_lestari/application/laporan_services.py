"""Dashboard, laporan umum, dan laporan kas kecil (imprest) bulanan."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_laporan import (
    ArusAkunOut,
    AkunSaldoOut,
    BarisKategoriOut,
    DashboardOut,
    ImprestRingkasOut,
    LaporanImprestOut,
    LaporanUmumOut,
    MingguImprestOut,
    PengisianLaporanOut,
    TransaksiLaporanOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import OrderOut
from tenants.bumi_lestari.modules.bumi_lestari.application.services import (
    _akun_by_kode,
    _akun_imprest,
    _hari_ini,
    _is_admin,
    boleh_akses_akun,
    get_proporsi,
    hitung_pengisian,
    list_akun,
    saldo_akun,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.pembayaran_services import (
    _rentang_periode,
    list_piutang_reseller,
    selasa_acuan,
    siap_bayar_pemasok,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    KODE_DANA_CADANGAN,
    BlAkunKas,
    BlKategori,
    BlTransaksi,
    BlTransfer,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlOrder


def _baris(items) -> list[BarisKategoriOut]:
    return [BarisKategoriOut(kategori=b.kategori, jumlah=b.jumlah, jumlah_transaksi=b.jumlah_transaksi) for b in items]


async def _jumlah(session: AsyncSession, model, kolom_jumlah, *kondisi) -> Decimal:
    stmt = select(func.coalesce(func.sum(kolom_jumlah), 0)).where(*kondisi)
    return Decimal(str((await session.execute(stmt)).scalar_one() or 0))


# --- Laporan umum ---------------------------------------------------------------------------


async def laporan_umum(session: AsyncSession, user: BlUser, dari: date | None, sampai: date | None) -> LaporanUmumOut:
    hari_ini = _hari_ini()
    dari = dari or hari_ini.replace(day=1)
    sampai = sampai or hari_ini
    if sampai < dari:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tanggal akhir lebih awal dari tanggal awal")
    ringkas = await ringkasan_laba(session, dari, sampai)

    arus: list[ArusAkunOut] = []
    akuns = (await session.execute(select(BlAkunKas).where(BlAkunKas.aktif.is_(True)).order_by(BlAkunKas.created_at))).scalars()
    for akun in akuns:
        if not boleh_akses_akun(user, akun):
            continue  # kas iklan hanya terlihat admin (biayanya tetap masuk laba di atas)

        async def trx(jenis: str, a=akun):
            return await _jumlah(
                session, BlTransaksi, BlTransaksi.jumlah, BlTransaksi.akun_id == a.id, BlTransaksi.jenis == jenis,
                BlTransaksi.dibatalkan.is_(False), BlTransaksi.tanggal >= dari, BlTransaksi.tanggal <= sampai,
            )

        async def trf(kolom, a=akun):
            return await _jumlah(
                session, BlTransfer, BlTransfer.jumlah, kolom == a.id, BlTransfer.dibatalkan.is_(False),
                BlTransfer.tanggal >= dari, BlTransfer.tanggal <= sampai,
            )

        arus.append(
            ArusAkunOut(
                akun_id=akun.id, kode=akun.kode, nama=akun.nama, jenis=akun.jenis,
                saldo_awal=await saldo_akun(session, akun, dari - timedelta(days=1)),
                masuk=await trx("masuk"), keluar=await trx("keluar"),
                transfer_masuk=await trf(BlTransfer.ke_akun_id), transfer_keluar=await trf(BlTransfer.dari_akun_id),
                saldo_akhir=await saldo_akun(session, akun, sampai),
            )
        )
    return LaporanUmumOut(
        dari=dari, sampai=sampai, pemasukan=_baris(ringkas.pemasukan), total_pemasukan=ringkas.total_pemasukan,
        biaya=_baris(ringkas.biaya), total_biaya=ringkas.total_biaya, laba_bersih=ringkas.laba,
        di_luar_laba=_baris(ringkas.di_luar_laba), arus_kas=arus,
        total_kas_awal=sum((a.saldo_awal for a in arus), Decimal("0")),
        total_kas_akhir=sum((a.saldo_akhir for a in arus), Decimal("0")),
    )


# --- Laporan kas kecil / kas iklan ---------------------------------------------------------


def _minggu_bulan(awal: date, akhir: date) -> list[tuple[int, date, date]]:
    """Minggu Senin-Minggu; minggu ke-1 adalah minggu yang memuat tanggal 1 (dipotong ke dalam bulan)."""
    senin_1 = awal - timedelta(days=awal.weekday())
    hasil = []
    mulai, no = senin_1, 1
    while mulai <= akhir:
        hasil.append((no, max(mulai, awal), min(mulai + timedelta(days=6), akhir)))
        mulai += timedelta(days=7)
        no += 1
    return hasil


async def laporan_imprest(
    session: AsyncSession, jenis: str, periode: str, saldo_fisik: Decimal | None = None
) -> LaporanImprestOut:
    akun = await _akun_imprest(session, jenis)
    awal, akhir = _rentang_periode(periode)
    saldo_awal = await saldo_akun(session, akun, awal - timedelta(days=1))
    saldo_akhir = await saldo_akun(session, akun, akhir)

    rows = (
        await session.execute(
            select(BlTransaksi, BlKategori.nama)
            .join(BlKategori, BlKategori.id == BlTransaksi.kategori_id)
            .where(
                BlTransaksi.akun_id == akun.id, BlTransaksi.jenis == "keluar", BlTransaksi.dibatalkan.is_(False),
                BlTransaksi.tanggal >= awal, BlTransaksi.tanggal <= akhir,
            )
            .order_by(BlTransaksi.tanggal, BlTransaksi.created_at)
        )
    ).all()
    transaksi = [TransaksiLaporanOut(tanggal=t.tanggal, kategori=k, keterangan=t.keterangan, jumlah=t.jumlah) for t, k in rows]
    per_kategori: dict[str, BarisKategoriOut] = {}
    for t in transaksi:
        b = per_kategori.setdefault(t.kategori, BarisKategoriOut(kategori=t.kategori, jumlah=Decimal("0")))
        b.jumlah += Decimal(t.jumlah)
        b.jumlah_transaksi += 1

    isi = (
        await session.execute(
            select(BlTransfer)
            .where(
                BlTransfer.ke_akun_id == akun.id, BlTransfer.dibatalkan.is_(False),
                BlTransfer.tanggal >= awal, BlTransfer.tanggal <= akhir,
            )
            .order_by(BlTransfer.tanggal)
        )
    ).scalars().all()
    pengisian = [PengisianLaporanOut(tanggal=t.tanggal, jumlah=t.jumlah, keterangan=t.keterangan) for t in isi]

    per_minggu = []
    for no, dari, sampai in _minggu_bulan(awal, akhir):
        per_minggu.append(
            MingguImprestOut(
                minggu_ke=no, dari=dari, sampai=sampai,
                pemakaian=sum((Decimal(t.jumlah) for t in transaksi if dari <= t.tanggal <= sampai), Decimal("0")),
                pengisian=sum((Decimal(p.jumlah) for p in pengisian if dari <= p.tanggal <= sampai), Decimal("0")),
                saldo_akhir=await saldo_akun(session, akun, sampai),
            )
        )
    out = LaporanImprestOut(
        periode=periode, akun_id=akun.id, nama=akun.nama, plafon=Decimal(akun.plafon), saldo_awal=saldo_awal,
        total_pemakaian=sum((Decimal(t.jumlah) for t in transaksi), Decimal("0")),
        total_pengisian=sum((Decimal(p.jumlah) for p in pengisian), Decimal("0")),
        saldo_akhir=saldo_akhir, sesuai_plafon=saldo_akhir == Decimal(akun.plafon),
        per_kategori=list(per_kategori.values()), per_minggu=per_minggu, transaksi=transaksi, pengisian=pengisian,
    )
    if saldo_fisik is not None:
        out.saldo_fisik = saldo_fisik
        out.selisih = saldo_fisik - saldo_akhir
        out.status_selisih = "sesuai" if out.selisih == 0 else ("lebih" if out.selisih > 0 else "kurang")
    return out


# --- Dashboard ---------------------------------------------------------------------------------


async def _ringkas_imprest(session: AsyncSession, jenis: str) -> ImprestRingkasOut:
    info = await hitung_pengisian(session, jenis)
    return ImprestRingkasOut(saldo=info["saldo"], plafon=info["plafon"], perlu_diisi=info["perlu_diisi"])


async def dashboard(session: AsyncSession, user: BlUser, periode: str | None = None) -> DashboardOut:
    hari_ini = _hari_ini()
    periode = periode or f"{hari_ini.year}-{hari_ini.month:02d}"
    awal, akhir = _rentang_periode(periode)
    ringkas = await ringkasan_laba(session, awal, akhir)

    akun = [
        AkunSaldoOut(id=a.id, kode=a.kode, nama=a.nama, jenis=a.jenis, saldo=saldo, plafon=a.plafon)
        for a, saldo in await list_akun(session, user)
    ]
    status_hitung = (
        await session.execute(select(BlOrder.status, func.count(BlOrder.id)).group_by(BlOrder.status))
    ).all()
    orders = (
        await session.execute(
            select(BlOrder).where(BlOrder.tanggal_order >= awal, BlOrder.tanggal_order <= akhir, BlOrder.status != "batal")
        )
    ).scalars().all()
    omzet = sum((OrderOut.model_validate(o).total_penjualan - Decimal(o.potongan_marketplace) for o in orders), Decimal("0"))

    siap = await siap_bayar_pemasok(session, hari_ini)
    piutang = sum((g.subtotal for g in await list_piutang_reseller(session)), Decimal("0"))
    dana = await saldo_akun(session, await _akun_by_kode(session, KODE_DANA_CADANGAN))

    kas_iklan = await _ringkas_imprest(session, "kas_iklan") if _is_admin(user) else None
    proporsi = {p.penerima: Decimal(p.persen) for p in await get_proporsi(session)}
    bagian_admin = bagian_owner = None
    if set(proporsi) == {"admin", "owner"}:
        laba = ringkas.laba
        bagian_admin = max((laba * proporsi["admin"] / 100).quantize(Decimal("0.01")), Decimal("0")) if laba > 0 else Decimal("0")
        bagian_owner = laba - bagian_admin if laba > 0 else Decimal("0")

    return DashboardOut(
        periode=periode, selasa=selasa_acuan(hari_ini), akun=akun, total_kas=sum((a.saldo for a in akun), Decimal("0")),
        pemasukan_bulan_ini=ringkas.total_pemasukan, biaya_bulan_ini=ringkas.total_biaya, laba_bulan_ini=ringkas.laba,
        order_per_status={s: int(n) for s, n in status_hitung}, order_bulan_ini=len(orders), omzet_order_bulan_ini=omzet,
        piutang_penjual_lain=piutang, utang_pemasok_siap_bayar=siap.total, dana_cadangan=dana,
        kas_kecil=await _ringkas_imprest(session, "kas_kecil"), kas_iklan=kas_iklan,
        bagian_admin_pratinjau=bagian_admin, bagian_owner_pratinjau=bagian_owner,
    )


