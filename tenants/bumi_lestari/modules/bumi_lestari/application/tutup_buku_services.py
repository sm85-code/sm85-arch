"""Tutup buku bulanan (spesifikasi 8.10 AB-TB-1..6, KP-TB-1..4).

- `kesiapan`: daftar butir AB-TB-2 + pratinjau laba rugi final; draf bertanggal di bulan itu adalah penghalang,
  belum cair hanya catatan.
- `tutup`: mengunci bulan (lihat pengaman di `audit_core`) dan menyimpan snapshot (laba rugi, laporan umum,
  saldo akun, belum cair) yang dipakai laporan & bagi hasil selamanya.
- `buka_darurat`: admin, wajib alasan, hanya bila bagi hasil bulan itu belum dibayar; tercatat di log audit.

Dipanggil dari API dan (nanti) job sinkronisasi; tidak bergantung pada request.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.audit_core import catat_audit, nama_bulan
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.laporan_services import (
    _baris,
    belum_cair_sementara,
    laporan_umum,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.pembayaran_services import (
    _rentang_periode,
    order_reseller_belum_dibayar,
    order_sudah_dibayar_pemasok,
    selasa_acuan,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.provisi_services import JUMLAH_CICILAN, periode_dan_minggu
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_tutup_buku import (
    ButirKesiapanOut,
    KesiapanOut,
    PratinjauLabaOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.services import _hari_ini, saldo_akun
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    STATUS_DIBUKA,
    STATUS_DITUTUP,
    STATUS_DRAF,
    BlAkunKas,
    BlTransaksi,
    BlTutupBuku,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlOrder
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pembayaran import (
    BlBagiHasil,
    BlGaji,
    BlKaryawan,
    BlLangganan,
    BlSisihan,
    BlTagihan,
)

STATUS_TERBUKA = "terbuka"


def _bad(detail: str, code: int = status.HTTP_409_CONFLICT) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _cek_periode(periode: str) -> tuple[date, date]:
    try:
        return _rentang_periode(periode)
    except (ValueError, IndexError):
        raise _bad("Periode harus berformat YYYY-MM", 422) from None


async def get_tutup_buku(session: AsyncSession, periode: str) -> BlTutupBuku | None:
    return (await session.execute(select(BlTutupBuku).where(BlTutupBuku.periode == periode))).scalar_one_or_none()


async def list_tutup_buku(session: AsyncSession) -> list[BlTutupBuku]:
    return list((await session.execute(select(BlTutupBuku).order_by(BlTutupBuku.periode.desc()))).scalars())


def _selasa_dalam_bulan(awal: date, akhir: date) -> list[date]:
    d = awal + timedelta(days=(1 - awal.weekday()) % 7)
    hasil = []
    while d <= akhir:
        hasil.append(d)
        d += timedelta(days=7)
    return hasil


async def _butir(session: AsyncSession, periode: str, awal: date, akhir: date, hari_ini: date) -> list[ButirKesiapanOut]:
    butir: list[ButirKesiapanOut] = []

    def tambah(kode: str, label: str, siap: bool, keterangan: str = "", penghalang: bool = True) -> None:
        butir.append(ButirKesiapanOut(kode=kode, label=label, siap=siap, penghalang=penghalang, keterangan=keterangan))

    tambah("bulan_berakhir", "Bulannya sudah berakhir", akhir < hari_ini,
           "" if akhir < hari_ini else f"Tutup buku bisa dilakukan mulai {akhir + timedelta(days=1):%d/%m/%Y}.")

    # Gaji bulan itu sudah dibayar (dibayar awal bulan berikutnya dari Dana cadangan).
    karyawan = (
        await session.execute(select(func.count()).select_from(BlKaryawan).where(BlKaryawan.aktif.is_(True), BlKaryawan.gaji_bulanan > 0))
    ).scalar_one()
    gaji = (await session.execute(select(BlGaji).where(BlGaji.periode == periode))).scalars().all()
    belum_gaji = [g for g in gaji if g.tanggal_bayar is None]
    if karyawan == 0 and not gaji:
        tambah("gaji", "Gaji bulan ini sudah dibayar", True, "Tidak ada karyawan bergaji.")
    elif not gaji:
        tambah("gaji", "Gaji bulan ini sudah dibayar", False, "Gaji bulan ini belum disiapkan & dibayar (Gaji & tagihan rutin).")
    else:
        tambah("gaji", "Gaji bulan ini sudah dibayar", not belum_gaji,
               f"{len(belum_gaji)} gaji belum dibayar." if belum_gaji else "")

    # Tagihan rutin: setiap langganan aktif berperkiraan > 0 punya tagihan periode itu.
    langganan = (
        await session.execute(select(BlLangganan).where(BlLangganan.aktif.is_(True), BlLangganan.jumlah_bulanan > 0))
    ).scalars().all()
    sudah = set(
        (await session.execute(
            select(BlTagihan.langganan_id).where(BlTagihan.periode == periode, BlTagihan.dibatalkan.is_(False))
        )).scalars()
    )
    kurang = [lg.nama for lg in langganan if lg.id not in sudah]
    tambah("tagihan", "Tagihan rutin sudah dibayar", not kurang, ("Belum: " + ", ".join(kurang)) if kurang else "")

    # Sisihan mingguan: Selasa ke-1..4 di bulan itu (bila ada karyawan bergaji).
    if karyawan:
        perlu = [
            periode_dan_minggu(s) for s in _selasa_dalam_bulan(awal, akhir) if periode_dan_minggu(s)[1] <= JUMLAH_CICILAN
        ]
        ada = set(
            (await session.execute(
                select(BlSisihan.minggu_ke).where(BlSisihan.periode == periode, BlSisihan.dibatalkan.is_(False))
            )).scalars()
        )
        hilang = [m for _p, m in perlu if m not in ada]
        tambah("sisihan", "Semua sisihan mingguan sudah dicatat", not hilang,
               f"Selasa ke-{', '.join(map(str, hilang))} belum disisihkan." if hilang else "")
    else:
        tambah("sisihan", "Semua sisihan mingguan sudah dicatat", True, "Tidak ada gaji yang perlu disisihkan.")

    # Cek fisik kas kecil: belum disimpan di server (Fase 2.11) -> pengingat, bukan penghalang.
    tambah("cek_fisik", "Cek fisik kas kecil sudah dilakukan", False,
           "Lakukan cek fisik di Laporan kas kecil (belum tersimpan di server, jadi tidak diperiksa otomatis).",
           penghalang=False)

    # Tagihan penjual lain & pembayaran tukang yang SUDAH jatuh tempo (s.d. Sabtu sebelum Selasa ini).
    batas = selasa_acuan(hari_ini) - timedelta(days=3)
    piutang = [o for o, _p in await order_reseller_belum_dibayar(session) if awal <= o.tgl_dikirim <= akhir]
    telat = [o for o in piutang if o.tgl_dikirim <= batas]
    ket = f"{len(telat)} order penjual lain jatuh tempo belum dibayar." if telat else ""
    if len(piutang) > len(telat):
        ket = (ket + " " if ket else "") + f"{len(piutang) - len(telat)} order baru ditagih Selasa berikutnya (tidak menghalangi)."
    tambah("penjual_lain", "Tagihan penjual lain sudah beres", not telat, ket)

    dibayar = await order_sudah_dibayar_pemasok(session)
    tukang = (
        await session.execute(
            select(BlOrder).where(
                BlOrder.status != "batal", BlOrder.pemasok_id.is_not(None), BlOrder.tgl_diambil.is_not(None),
                BlOrder.tgl_diambil >= awal, BlOrder.tgl_diambil <= akhir, BlOrder.biaya_pokok > 0,
            )
        )
    ).scalars().all()
    belum = [o for o in tukang if o.id not in dibayar]
    telat_tukang = [o for o in belum if o.tgl_diambil <= batas]
    ket = f"{len(telat_tukang)} order belum dibayar ke tukang & supplier." if telat_tukang else ""
    if len(belum) > len(telat_tukang):
        ket = (ket + " " if ket else "") + f"{len(belum) - len(telat_tukang)} order dibayar Selasa berikutnya (tidak menghalangi)."
    tambah("tukang", "Pembayaran tukang & supplier sudah beres", not telat_tukang, ket)

    # Draf bertanggal di bulan itu yang belum dikirim ke laporan keuangan: PENGHALANG (AB-TB-2).
    draf = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(BlTransaksi.jumlah), 0)).where(
                BlTransaksi.status_kirim == STATUS_DRAF, BlTransaksi.dibatalkan.is_(False),
                BlTransaksi.tanggal >= awal, BlTransaksi.tanggal <= akhir,
            )
        )
    ).one()
    tambah("draf", "Tidak ada draf yang belum dikirim ke laporan keuangan", draf[0] == 0,
           f"{draf[0]} catatan draf (Rp{Decimal(draf[1]):,.0f}) belum dikirim.".replace(",", ".") if draf[0] else "")
    return butir


async def _pratinjau(session: AsyncSession, awal: date, akhir: date) -> PratinjauLabaOut:
    r = await ringkasan_laba(session, awal, akhir)
    return PratinjauLabaOut(
        pemasukan=_baris(r.pemasukan), biaya=_baris(r.biaya), di_luar_laba=_baris(r.di_luar_laba),
        total_pemasukan=r.total_pemasukan, total_biaya=r.total_biaya, laba_bersih=r.laba,
    )


async def kesiapan(session: AsyncSession, periode: str, hari_ini: date | None = None) -> KesiapanOut:
    awal, akhir = _cek_periode(periode)
    row = await get_tutup_buku(session, periode)
    butir = await _butir(session, periode, awal, akhir, hari_ini or _hari_ini())
    st = row.status if row else STATUS_TERBUKA
    return KesiapanOut(
        periode=periode, status=st,
        boleh_tutup=st != STATUS_DITUTUP and all(b.siap for b in butir if b.penghalang),
        butir=butir, pratinjau=await _pratinjau(session, awal, akhir), belum_cair=await belum_cair_sementara(session),
    )


async def _snapshot(session: AsyncSession, user: BlUser, periode: str, awal: date, akhir: date) -> dict:
    lap = await laporan_umum(session, user, awal, akhir, pakai_snapshot=False)
    akun = (await session.execute(select(BlAkunKas).order_by(BlAkunKas.created_at))).scalars().all()
    return {
        "periode": periode,
        "laba_rugi": (await _pratinjau(session, awal, akhir)).model_dump(mode="json"),
        "laporan_umum": lap.model_dump(mode="json"),
        "saldo_akun": [
            {"akun_id": a.id, "kode": a.kode, "nama": a.nama, "jenis": a.jenis, "saldo": str(await saldo_akun(session, a, akhir))}
            for a in akun
        ],
        "belum_cair": str(await belum_cair_sementara(session)),
        "dibuat_pada": datetime.now(timezone.utc).isoformat(),
    }


async def tutup(session: AsyncSession, user: BlUser, periode: str, hari_ini: date | None = None) -> BlTutupBuku:
    awal, akhir = _cek_periode(periode)
    k = await kesiapan(session, periode, hari_ini)
    if k.status == STATUS_DITUTUP:
        raise _bad(f"{nama_bulan(periode)} sudah tutup buku")
    kurang = [b.label for b in k.butir if b.penghalang and not b.siap]
    if kurang:
        raise _bad(f"Belum bisa tutup buku {nama_bulan(periode)}. Selesaikan dulu: " + "; ".join(kurang))
    snap = await _snapshot(session, user, periode, awal, akhir)
    row = await get_tutup_buku(session, periode)
    if row is None:
        row = BlTutupBuku(periode=periode, ditutup_oleh=user.id, snapshot=snap)
        session.add(row)
    else:  # ditutup ulang setelah buka darurat
        row.status, row.ditutup_oleh, row.ditutup_pada, row.snapshot = STATUS_DITUTUP, user.id, datetime.now(timezone.utc), snap
    await session.flush()
    await catat_audit(session, user.id, "tutup_buku", "tutup_buku", row.id,
                      sesudah={"periode": periode, "laba_bersih": snap["laba_rugi"]["laba_bersih"]})
    return row


async def buka_darurat(session: AsyncSession, user: BlUser, periode: str, alasan: str) -> BlTutupBuku:
    _cek_periode(periode)
    row = await get_tutup_buku(session, periode)
    if row is None or row.status != STATUS_DITUTUP:
        raise _bad(f"{nama_bulan(periode)} belum tutup buku")
    dibayar = (
        await session.execute(
            select(BlBagiHasil.id).where(
                BlBagiHasil.periode == periode, BlBagiHasil.dibatalkan.is_(False), BlBagiHasil.tanggal_bayar.is_not(None)
            )
        )
    ).first()
    if dibayar:
        raise _bad("Bagi hasil bulan ini sudah dibayar; buka darurat tidak diizinkan (batalkan pembayaran bagi hasil dulu)")
    row.status, row.dibuka_oleh, row.dibuka_pada, row.alasan_buka = STATUS_DIBUKA, user.id, datetime.now(timezone.utc), alasan
    await session.flush()
    await catat_audit(session, user.id, "buka_tutup_buku", "tutup_buku", row.id, sebelum={"status": STATUS_DITUTUP},
                      sesudah={"status": STATUS_DIBUKA, "periode": periode}, alasan=alasan)
    return row
