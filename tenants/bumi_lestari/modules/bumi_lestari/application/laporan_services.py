"""Dashboard, laporan umum, dan laporan kas kecil (imprest) bulanan."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.kiriman_services import ringkasan_draf
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_laporan import (
    BelumCairOut,
    OrderBelumCairOut,
    SaluranBelumCairOut,
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
    minggu_tagihan,
    order_reseller_belum_dibayar,
    selasa_acuan,
    siap_bayar_pemasok,
    tagihan_order,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    KODE_DANA_CADANGAN,
    STATUS_DRAF,
    STATUS_DITUTUP,
    STATUS_TERKIRIM,
    BlAkunKas,
    BlKategori,
    BlTransaksi,
    BlTransfer,
    BlTutupBuku,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import (
    JENIS_SALURAN_CAIR,
    STATUS_BATAL,
    STATUS_CAIR_CAIR,
    STATUS_RETUR,
    BlOrder,
    BlSaluran,
)


def _baris(items) -> list[BarisKategoriOut]:
    return [BarisKategoriOut(kategori=b.kategori, jumlah=b.jumlah, jumlah_transaksi=b.jumlah_transaksi) for b in items]


async def _jumlah(session: AsyncSession, model, kolom_jumlah, *kondisi) -> Decimal:
    stmt = select(func.coalesce(func.sum(kolom_jumlah), 0)).where(*kondisi)
    return Decimal(str((await session.execute(stmt)).scalar_one() or 0))


# --- Laporan umum ---------------------------------------------------------------------------


async def belum_cair(session: AsyncSession, per_tanggal: date | None = None) -> BelumCairOut:
    """Order marketplace/Toko web yang per tanggal itu sudah dikirim tetapi belum cair dan belum retur (AB-BC-1/2).
    Dihitung ulang dari tanggal di order, jadi bisa dipakai untuk tanggal lampau (laporan, snapshot tutup buku)."""
    per = per_tanggal or _hari_ini()
    rows = (
        await session.execute(
            select(BlOrder, BlSaluran)
            .join(BlSaluran, BlSaluran.id == BlOrder.saluran_id)
            .where(
                BlSaluran.jenis.in_(JENIS_SALURAN_CAIR), BlOrder.status != STATUS_BATAL,
                BlOrder.tgl_dikirim.is_not(None), BlOrder.tgl_dikirim <= per,
                or_(BlOrder.status_cair != STATUS_CAIR_CAIR, BlOrder.tgl_cair.is_(None), BlOrder.tgl_cair > per),
                or_(BlOrder.status != STATUS_RETUR, BlOrder.tgl_retur.is_(None), BlOrder.tgl_retur > per),
            )
            .order_by(BlSaluran.nama, BlOrder.tgl_dikirim, BlOrder.no_order)
        )
    ).all()
    grup: dict[str, SaluranBelumCairOut] = {}
    for o, sal in rows:
        jual = OrderOut.model_validate(o).total_penjualan
        potong = Decimal(o.potongan_aktual if o.potongan_aktual is not None else o.potongan_marketplace)
        g = grup.setdefault(sal.id, SaluranBelumCairOut(
            saluran_id=sal.id, nama=sal.nama, akun_id=sal.akun_id, jumlah_order=0, total_penjualan=Decimal("0"),
            total_perkiraan_cair=Decimal("0"), tgl_kirim_tertua=None, order=[],
        ))
        g.order.append(OrderBelumCairOut(
            order_id=o.id, no_order=o.no_order, tgl_dikirim=o.tgl_dikirim, penjualan=jual, potongan=potong,
            perkiraan_cair=jual - potong, status=o.status,
        ))
        g.jumlah_order += 1
        g.total_penjualan += jual
        g.total_perkiraan_cair += jual - potong
        g.tgl_kirim_tertua = min(filter(None, (g.tgl_kirim_tertua, o.tgl_dikirim)))
    per_saluran = list(grup.values())
    tertua = [g.tgl_kirim_tertua for g in per_saluran if g.tgl_kirim_tertua]
    return BelumCairOut(
        per_tanggal=per, jumlah_order=sum(g.jumlah_order for g in per_saluran),
        total_penjualan=sum((g.total_penjualan for g in per_saluran), Decimal("0")),
        total_perkiraan_cair=sum((g.total_perkiraan_cair for g in per_saluran), Decimal("0")),
        tgl_kirim_tertua=min(tertua) if tertua else None, per_saluran=per_saluran,
    )


async def total_belum_cair(session: AsyncSession, per_tanggal: date | None = None) -> Decimal:
    return (await belum_cair(session, per_tanggal)).total_perkiraan_cair


async def _laporan_dari_snapshot(session: AsyncSession, user: BlUser, dari: date, sampai: date) -> LaporanUmumOut | None:
    """Bulan yang sudah tutup buku: angka dari snapshot agar tetap sama selamanya (KP-TB-3)."""
    if dari.day != 1 or (sampai + timedelta(days=1)).day != 1 or (dari.year, dari.month) != (sampai.year, sampai.month):
        return None
    row = (
        await session.execute(
            select(BlTutupBuku).where(BlTutupBuku.periode == f"{dari:%Y-%m}", BlTutupBuku.status == STATUS_DITUTUP)
        )
    ).scalar_one_or_none()
    if row is None or not row.snapshot.get("laporan_umum"):
        return None
    out = LaporanUmumOut.model_validate(row.snapshot["laporan_umum"])
    akun = {a.id: a for a in (await session.execute(select(BlAkunKas))).scalars()}
    out.arus_kas = [a for a in out.arus_kas if a.akun_id in akun and boleh_akses_akun(user, akun[a.akun_id])]
    out.total_kas_awal = sum((a.saldo_awal for a in out.arus_kas), Decimal("0"))
    out.total_kas_akhir = sum((a.saldo_akhir for a in out.arus_kas), Decimal("0"))
    out.draf_belum_dikirim = []
    out.dari_snapshot = True
    out.ditutup_pada = row.ditutup_pada
    return out


async def laporan_umum(
    session: AsyncSession, user: BlUser, dari: date | None, sampai: date | None, *, pakai_snapshot: bool = True
) -> LaporanUmumOut:
    hari_ini = _hari_ini()
    dari = dari or hari_ini.replace(day=1)
    sampai = sampai or hari_ini
    if sampai < dari:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tanggal akhir lebih awal dari tanggal awal")
    if pakai_snapshot and (snap := await _laporan_dari_snapshot(session, user, dari, sampai)) is not None:
        return snap
    ringkas = await ringkasan_laba(session, dari, sampai)

    arus: list[ArusAkunOut] = []
    akuns = (await session.execute(select(BlAkunKas).where(BlAkunKas.aktif.is_(True)).order_by(BlAkunKas.created_at))).scalars()
    for akun in akuns:
        if not boleh_akses_akun(user, akun):
            continue  # kas iklan hanya terlihat admin (biayanya tetap masuk laba di atas)

        async def trx(jenis: str, a=akun):
            return await _jumlah(
                session, BlTransaksi, BlTransaksi.jumlah, BlTransaksi.akun_id == a.id, BlTransaksi.jenis == jenis,
                BlTransaksi.dibatalkan.is_(False), BlTransaksi.status_kirim == STATUS_TERKIRIM,
                BlTransaksi.tanggal >= dari, BlTransaksi.tanggal <= sampai,
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
    bc = await total_belum_cair(session, sampai)
    return LaporanUmumOut(
        belum_cair=bc, perkiraan_laba_jika_cair=ringkas.laba + bc,
        dari=dari, sampai=sampai, pemasukan=_baris(ringkas.pemasukan), total_pemasukan=ringkas.total_pemasukan,
        biaya=_baris(ringkas.biaya), total_biaya=ringkas.total_biaya, laba_bersih=ringkas.laba,
        di_luar_laba=_baris(ringkas.di_luar_laba), arus_kas=arus,
        total_kas_awal=sum((a.saldo_awal for a in arus), Decimal("0")),
        total_kas_akhir=sum((a.saldo_akhir for a in arus), Decimal("0")),
        draf_belum_dikirim=[d for d in await ringkasan_draf(session, user, sampai=sampai, rinci=False) if d.jumlah_entri],
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
                BlTransaksi.status_kirim == STATUS_TERKIRIM, BlTransaksi.tanggal >= awal, BlTransaksi.tanggal <= akhir,
            )
            .order_by(BlTransaksi.tanggal, BlTransaksi.created_at)
        )
    ).all()
    total_draf = await _jumlah(
        session, BlTransaksi, BlTransaksi.jumlah, BlTransaksi.akun_id == akun.id, BlTransaksi.jenis == "keluar",
        BlTransaksi.dibatalkan.is_(False), BlTransaksi.status_kirim == STATUS_DRAF,
        BlTransaksi.tanggal >= awal, BlTransaksi.tanggal <= akhir,
    )
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
        total_draf_belum_dikirim=total_draf,
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
        for a, saldo, _fisik in await list_akun(session, user)
    ]
    status_hitung = (
        await session.execute(
            select(BlOrder.status, func.count(BlOrder.id))
            .where(BlOrder.tanggal_order >= awal, BlOrder.tanggal_order <= akhir)
            .group_by(BlOrder.status)
        )
    ).all()
    aktif_hitung = (
        await session.execute(
            select(BlOrder.status, func.count(BlOrder.id))
            .where(BlOrder.status.not_in(("selesai", "batal")))
            .group_by(BlOrder.status)
        )
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
    _, sabtu_lalu = minggu_tagihan(selasa_acuan(hari_ini))
    tagihan_minggu = sum(
        (tagihan_order(o) for o, _p in await order_reseller_belum_dibayar(session, sampai_kirim=sabtu_lalu)), Decimal("0")
    )
    belum_cair_kini = await total_belum_cair(session, hari_ini)
    draf = [d for d in await ringkasan_draf(session, user, rinci=False) if d.jumlah_entri]

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
        order_aktif_per_status={s: int(n) for s, n in aktif_hitung},
        tagihan_penjual_lain_minggu_ini=tagihan_minggu, belum_cair=belum_cair_kini, belum_cair_sementara=belum_cair_kini,
        draf_belum_dikirim=draf,
        piutang_penjual_lain=piutang, utang_pemasok_siap_bayar=siap.total, dana_cadangan=dana,
        kas_kecil=await _ringkas_imprest(session, "kas_kecil"), kas_iklan=kas_iklan,
        bagian_admin_pratinjau=bagian_admin, bagian_owner_pratinjau=bagian_owner,
    )


