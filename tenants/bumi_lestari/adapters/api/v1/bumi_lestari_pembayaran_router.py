"""HTTP surface for bumi_lestari T3/T4 -- pembayaran Selasa, penerimaan reseller, gaji, bagi hasil."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import provisi_services as psvc
from tenants.bumi_lestari.modules.bumi_lestari.application import pembayaran_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import BatalIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pembayaran import (
    BagiHasilIn,
    BagiHasilOut,
    BagiHasilTersimpanOut,
    GajiOut,
    GajiPeriodeIn,
    KaryawanIn,
    KaryawanOut,
    KaryawanPatch,
    LanggananIn,
    LanggananOut,
    LanggananPatch,
    PembayaranPemasokDetailOut,
    PembayaranPemasokIn,
    PembayaranPemasokOut,
    PenerimaanResellerIn,
    PenerimaanResellerOut,
    PiutangPelangganOut,
    SiapBayarOut,
    SisihanIn,
    SisihanOut,
    SisihanTersimpanOut,
    TagihanBayarIn,
    TagihanOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

pembayaran_router = APIRouter()
PERIODE = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM")


def _guard():
    return Depends(require_roles_bumi_lestari("admin", "owner"))


def _db():
    return Depends(get_db_bumi_lestari)


# Pembayaran pemasok (tukang kayu + supplier) tiap Selasa
@pembayaran_router.get("/pembayaran-pemasok/siap", response_model=SiapBayarOut)
async def siap_bayar(tanggal: date | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.siap_bayar_pemasok(session, tanggal)


@pembayaran_router.post("/pembayaran-pemasok", response_model=PembayaranPemasokOut, status_code=status.HTTP_201_CREATED)
async def kirim_ke_laporan(payload: PembayaranPemasokIn, session: AsyncSession = _db(), user: BlUser = _guard()):
    """Tombol "Kirim ke laporan": 1 transaksi bertotal di laporan, rinciannya per order/barang."""
    return await svc.buat_pembayaran_pemasok(session, user, payload)


@pembayaran_router.get("/pembayaran-pemasok", response_model=list[PembayaranPemasokOut])
async def list_pembayaran(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_pembayaran_pemasok(session)


@pembayaran_router.get("/pembayaran-pemasok/{pembayaran_id}", response_model=PembayaranPemasokDetailOut)
async def detail_pembayaran(pembayaran_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.detail_pembayaran_pemasok(session, pembayaran_id)


@pembayaran_router.post("/pembayaran-pemasok/{pembayaran_id}/batal", response_model=PembayaranPemasokOut)
async def batal_pembayaran(pembayaran_id: str, payload: BatalIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.batalkan_pembayaran_pemasok(session, pembayaran_id, payload.alasan)


# Penerimaan dari penjual lain (reseller)
@pembayaran_router.get("/piutang-reseller", response_model=list[PiutangPelangganOut])
async def piutang(pelanggan_id: str | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_piutang_reseller(session, pelanggan_id)


@pembayaran_router.post("/penerimaan-reseller", response_model=PenerimaanResellerOut, status_code=status.HTTP_201_CREATED)
async def terima_reseller(payload: PenerimaanResellerIn, session: AsyncSession = _db(), user: BlUser = _guard()):
    return await svc.buat_penerimaan_reseller(session, user, payload)


@pembayaran_router.get("/penerimaan-reseller", response_model=list[PenerimaanResellerOut])
async def list_penerimaan(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_penerimaan_reseller(session)


@pembayaran_router.post("/penerimaan-reseller/{penerimaan_id}/batal", response_model=PenerimaanResellerOut)
async def batal_penerimaan(penerimaan_id: str, payload: BatalIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.batalkan_penerimaan_reseller(session, penerimaan_id, payload.alasan)


# Karyawan tetap & gaji
@pembayaran_router.get("/karyawan", response_model=list[KaryawanOut])
async def list_karyawan(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_karyawan(session)


@pembayaran_router.post("/karyawan", response_model=KaryawanOut, status_code=status.HTTP_201_CREATED)
async def create_karyawan(payload: KaryawanIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.create_karyawan(session, payload)


@pembayaran_router.patch("/karyawan/{karyawan_id}", response_model=KaryawanOut)
async def update_karyawan(karyawan_id: str, payload: KaryawanPatch, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.update_karyawan(session, karyawan_id, payload)


@pembayaran_router.post("/gaji/siapkan", response_model=list[GajiOut])
async def siapkan_gaji(payload: GajiPeriodeIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.siapkan_gaji(session, payload.periode)


@pembayaran_router.get("/gaji", response_model=list[GajiOut])
async def list_gaji(periode: str = PERIODE, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_gaji(session, periode)


@pembayaran_router.post("/gaji/bayar", response_model=list[GajiOut])
async def bayar_gaji(payload: GajiPeriodeIn, session: AsyncSession = _db(), user: BlUser = _guard()):
    return await svc.bayar_gaji(session, user, payload.periode, payload.tanggal)


@pembayaran_router.post("/gaji/{gaji_id}/batal-bayar", response_model=GajiOut)
async def batal_bayar_gaji(gaji_id: str, payload: BatalIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.batalkan_bayar_gaji(session, gaji_id, payload.alasan)


# Bagi hasil bulanan
@pembayaran_router.get("/bagi-hasil/hitung", response_model=BagiHasilOut)
async def hitung_bagi_hasil(periode: str = PERIODE, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.hitung_bagi_hasil(session, periode)


@pembayaran_router.get("/bagi-hasil", response_model=list[BagiHasilTersimpanOut])
async def list_bagi_hasil(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_bagi_hasil(session)


@pembayaran_router.post("/bagi-hasil", response_model=BagiHasilTersimpanOut, status_code=status.HTTP_201_CREATED)
async def simpan_bagi_hasil(payload: BagiHasilIn, session: AsyncSession = _db(), user: BlUser = _guard()):
    return await svc.simpan_bagi_hasil(session, user, payload.periode)


@pembayaran_router.post("/bagi-hasil/{bagi_hasil_id}/bayar", response_model=BagiHasilTersimpanOut)
async def bayar_bagi_hasil(
    bagi_hasil_id: str, tanggal: date | None = None, session: AsyncSession = _db(), user: BlUser = _guard()
):
    return await svc.bayar_bagi_hasil(session, user, bagi_hasil_id, tanggal)


@pembayaran_router.post("/bagi-hasil/{bagi_hasil_id}/batal", response_model=BagiHasilTersimpanOut)
async def batal_bagi_hasil(bagi_hasil_id: str, payload: BatalIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.batalkan_bagi_hasil(session, bagi_hasil_id, payload.alasan)


# Langganan, sisihan mingguan gaji & langganan (Dana cadangan), pembayaran tagihan awal bulan
@pembayaran_router.get("/langganan", response_model=list[LanggananOut])
async def list_langganan(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await psvc.list_langganan(session)


@pembayaran_router.post("/langganan", response_model=LanggananOut, status_code=status.HTTP_201_CREATED)
async def create_langganan(payload: LanggananIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await psvc.create_langganan(session, payload)


@pembayaran_router.patch("/langganan/{langganan_id}", response_model=LanggananOut)
async def update_langganan(langganan_id: str, payload: LanggananPatch, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await psvc.update_langganan(session, langganan_id, payload)


@pembayaran_router.get("/sisihan/hitung", response_model=SisihanOut)
async def hitung_sisihan(tanggal: date | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    """Cicilan Selasa ini (1/4 gaji + 1/4 langganan) yang akan disisihkan ke Dana cadangan."""
    return await psvc.hitung_sisihan(session, tanggal)


@pembayaran_router.post("/sisihan", response_model=SisihanTersimpanOut, status_code=status.HTTP_201_CREATED)
async def catat_sisihan(payload: SisihanIn, session: AsyncSession = _db(), user: BlUser = _guard()):
    return await psvc.catat_sisihan(session, user, payload.tanggal)


@pembayaran_router.get("/sisihan", response_model=list[SisihanTersimpanOut])
async def list_sisihan(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await psvc.list_sisihan(session)


@pembayaran_router.post("/sisihan/{sisihan_id}/batal", response_model=SisihanTersimpanOut)
async def batal_sisihan(sisihan_id: str, payload: BatalIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await psvc.batalkan_sisihan(session, sisihan_id, payload.alasan)


@pembayaran_router.post("/tagihan/bayar", response_model=list[TagihanOut], status_code=status.HTTP_201_CREATED)
async def bayar_tagihan(payload: TagihanBayarIn, session: AsyncSession = _db(), user: BlUser = _guard()):
    return await psvc.bayar_tagihan(session, user, payload)


@pembayaran_router.get("/tagihan", response_model=list[TagihanOut])
async def list_tagihan(periode: str = PERIODE, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await psvc.list_tagihan(session, periode)


@pembayaran_router.post("/tagihan/{tagihan_id}/batal", response_model=TagihanOut)
async def batal_tagihan(tagihan_id: str, payload: BatalIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await psvc.batalkan_tagihan(session, tagihan_id, payload.alasan)
