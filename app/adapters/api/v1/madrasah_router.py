"""HTTP surface for the isolated multi-role madrasah module.

Mounted in main.py as prefix=/api/madrasah only. Does not touch BUMDes routers.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.madrasah.application import services
from app.modules.madrasah.application.schemas import (
    AbsenBulkRequest,
    GuruIn,
    JadwalIn,
    LoginRequest,
    MapelIn,
    MateriIn,
    MateriPatch,
    PlacementIn,
    ProgresCreateRequest,
    RombelIn,
    SantriIn,
    TingkatIn,
)
from app.modules.madrasah.infrastructure.database import get_db_madrasah
from app.modules.madrasah.infrastructure.seeder import seed_madrasah

madrasah_router = APIRouter()
router = madrasah_router
admin_r = APIRouter(prefix="/admin", tags=["Madrasah Admin"])
kurikulum_r = APIRouter(prefix="/kurikulum", tags=["Madrasah Kurikulum"])
bendahara_r = APIRouter(prefix="/bendahara", tags=["Madrasah Bendahara"])
wali_kelas_r = APIRouter(prefix="/wali-kelas", tags=["Madrasah Wali Kelas"])
wali_santri_r = APIRouter(prefix="/wali-santri", tags=["Madrasah Wali Santri"])


def _user_out(user) -> dict:
    return services.user_out(user)


@madrasah_router.get("/seed-now")
async def seed_now(session: AsyncSession = Depends(get_db_madrasah)):
    try:
        ids = await seed_madrasah(session)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {"status": "Database madrasah berhasil diisi data awal JWT", "ids": ids}


@madrasah_router.post("/auth/login")
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        user = await services.login_by_phone(session, payload)
    except services.MadrasahAuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return {"user": _user_out(user)}


@madrasah_router.get("/kelas")
async def get_kelas(session: AsyncSession = Depends(get_db_madrasah)):
    rows = await services.list_kelas(session)
    return [{"id": row.id, "nama_kelas": row.nama_kelas} for row in rows]


@madrasah_router.get("/santri")
async def get_santri(kelas_id: str | None = Query(default=None), session: AsyncSession = Depends(get_db_madrasah)):
    rows = await services.list_santri(session, kelas_id)
    return [{"id": row.id, "nama": row.nama, "kelas_id": row.kelas_id, "rombel_id": getattr(row, "rombel_id", None), "orang_tua_id": row.orang_tua_id} for row in rows]


@madrasah_router.post("/absensi/bulk", status_code=status.HTTP_201_CREATED)
async def absensi_bulk(payload: AbsenBulkRequest, session: AsyncSession = Depends(get_db_madrasah)):
    rows = await services.bulk_insert_absensi(session, payload)
    return {"inserted": len(rows), "ids": [getattr(row, "id", None) for row in rows]}


@madrasah_router.post("/progres", status_code=status.HTTP_201_CREATED)
async def create_progres(payload: ProgresCreateRequest, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        row = await services.create_progres(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"id": row.id, "tanggal": row.tanggal.isoformat(), "santri_id": row.santri_id, "tipe": row.tipe, "capaian": row.capaian, "catatan_guru": row.catatan_guru, "mapel_id": row.mapel_id, "materi_id": row.materi_id}


@madrasah_router.get("/tagihan/{santri_id}")
async def get_tagihan(santri_id: str, session: AsyncSession = Depends(get_db_madrasah)):
    return [services.tagihan_out(row) for row in await services.list_tagihan(session, santri_id)]


@madrasah_router.post("/spp/generate", status_code=status.HTTP_201_CREATED)
async def spp_generate(session: AsyncSession = Depends(get_db_madrasah)):
    return [services.tagihan_out(row) for row in await services.generate_spp_massal(session)]


@madrasah_router.post("/spp/pay/{id}")
async def spp_pay(id: str, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        return services.tagihan_out(await services.pay_spp_manual(session, id))
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@madrasah_router.get("/pengumuman")
async def get_pengumuman(session: AsyncSession = Depends(get_db_madrasah)):
    rows = await services.list_pengumuman(session)
    return [{"id": row.id, "judul": row.judul, "isi": row.isi, "tanggal": row.tanggal.isoformat(), "dibuat_by": row.dibuat_by} for row in rows]


@admin_r.get("/tingkat")
async def admin_tingkat(session: AsyncSession = Depends(get_db_madrasah)):
    return [{"id": r.id, "nama": r.nama, "urutan": r.urutan} for r in await services.list_tingkat(session)]


@admin_r.post("/tingkat", status_code=status.HTTP_201_CREATED)
async def admin_tingkat_create(payload: TingkatIn, session: AsyncSession = Depends(get_db_madrasah)):
    row = await services.create_tingkat(session, payload)
    return {"id": row.id, "nama": row.nama, "urutan": row.urutan}


@admin_r.get("/rombel")
async def admin_rombel(session: AsyncSession = Depends(get_db_madrasah)):
    return [{"id": r.id, "nama": r.nama, "tingkat_id": r.tingkat_id, "wali_kelas_id": r.wali_kelas_id, "wali_kelas": r.wali_kelas.nama if r.wali_kelas else None} for r in await services.list_rombel(session)]


@admin_r.post("/rombel", status_code=status.HTTP_201_CREATED)
async def admin_rombel_create(payload: RombelIn, session: AsyncSession = Depends(get_db_madrasah)):
    row = await services.create_rombel(session, payload)
    return {"id": row.id, "nama": row.nama, "tingkat_id": row.tingkat_id, "wali_kelas_id": row.wali_kelas_id}


@admin_r.get("/guru")
async def admin_guru(session: AsyncSession = Depends(get_db_madrasah)):
    return [services.user_out(r) for r in await services.list_guru(session)]


@admin_r.post("/guru", status_code=status.HTTP_201_CREATED)
async def admin_guru_create(payload: GuruIn, session: AsyncSession = Depends(get_db_madrasah)):
    return services.user_out(await services.create_guru(session, payload))


@admin_r.post("/santri", status_code=status.HTTP_201_CREATED)
async def admin_santri_create(payload: SantriIn, session: AsyncSession = Depends(get_db_madrasah)):
    row = await services.create_santri(session, payload)
    return {"id": row.id, "nama": row.nama, "rombel_id": row.rombel_id}


@admin_r.post("/penempatan")
async def admin_place(payload: PlacementIn, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        row = await services.place_santri(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "nama": row.nama, "rombel_id": row.rombel_id}


@kurikulum_r.get("/mapel")
async def kur_mapel(session: AsyncSession = Depends(get_db_madrasah)):
    return [{"id": r.id, "kode": r.kode, "nama": r.nama, "materi": [{"id": m.id, "judul": m.judul, "urutan": m.urutan, "aktif": m.aktif} for m in r.materi]} for r in await services.list_mapel(session)]


@kurikulum_r.post("/mapel", status_code=status.HTTP_201_CREATED)
async def kur_mapel_create(payload: MapelIn, session: AsyncSession = Depends(get_db_madrasah)):
    row = await services.create_mapel(session, payload)
    return {"id": row.id, "kode": row.kode, "nama": row.nama}


@kurikulum_r.post("/materi", status_code=status.HTTP_201_CREATED)
async def kur_materi_create(payload: MateriIn, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        row = await services.create_materi(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "mapel_id": row.mapel_id, "judul": row.judul, "urutan": row.urutan, "aktif": row.aktif}


@kurikulum_r.patch("/materi/{materi_id}")
async def kur_materi_patch(materi_id: str, payload: MateriPatch, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        row = await services.patch_materi(session, materi_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "judul": row.judul, "urutan": row.urutan, "aktif": row.aktif}


@kurikulum_r.get("/jadwal")
async def kur_jadwal(rombel_id: str | None = Query(default=None), session: AsyncSession = Depends(get_db_madrasah)):
    return [{"id": r.id, "rombel_id": r.rombel_id, "mapel_id": r.mapel_id, "mapel": r.mapel.nama if r.mapel else None, "hari": r.hari, "jam_mulai": r.jam_mulai, "jam_selesai": r.jam_selesai} for r in await services.list_jadwal(session, rombel_id)]


@kurikulum_r.post("/jadwal", status_code=status.HTTP_201_CREATED)
async def kur_jadwal_create(payload: JadwalIn, session: AsyncSession = Depends(get_db_madrasah)):
    row = await services.create_jadwal(session, payload)
    return {"id": row.id, "rombel_id": row.rombel_id, "mapel_id": row.mapel_id, "hari": row.hari}


@bendahara_r.post("/spp/generate", status_code=status.HTTP_201_CREATED)
async def ben_generate(session: AsyncSession = Depends(get_db_madrasah)):
    return [services.tagihan_out(r) for r in await services.generate_spp_massal(session)]


@bendahara_r.post("/spp/pay/{id}")
async def ben_pay(id: str, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        return services.tagihan_out(await services.pay_spp_manual(session, id))
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@wali_kelas_r.get("/kurikulum")
async def wk_kurikulum(session: AsyncSession = Depends(get_db_madrasah)):
    return [{"id": r.id, "kode": r.kode, "nama": r.nama, "materi": [{"id": m.id, "judul": m.judul, "urutan": m.urutan} for m in r.materi if m.aktif]} for r in await services.list_mapel(session)]


@wali_kelas_r.post("/absensi", status_code=status.HTTP_201_CREATED)
async def wk_absen(payload: AbsenBulkRequest, session: AsyncSession = Depends(get_db_madrasah)):
    rows = await services.bulk_insert_absensi(session, payload)
    return {"inserted": len(rows)}


@wali_kelas_r.post("/progres", status_code=status.HTTP_201_CREATED)
async def wk_progres(payload: ProgresCreateRequest, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        row = await services.create_progres(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "santri_id": row.santri_id, "capaian": row.capaian, "materi_id": row.materi_id}


@wali_santri_r.get("/progres/{santri_id}")
async def ws_progres(santri_id: str, session: AsyncSession = Depends(get_db_madrasah)):
    return await services.progres_series(session, santri_id)


@wali_santri_r.get("/tagihan/{santri_id}")
async def ws_tagihan(santri_id: str, session: AsyncSession = Depends(get_db_madrasah)):
    return [services.tagihan_out(r) for r in await services.list_tagihan(session, santri_id)]


madrasah_router.include_router(admin_r)
madrasah_router.include_router(kurikulum_r)
madrasah_router.include_router(bendahara_r)
madrasah_router.include_router(wali_kelas_r)
madrasah_router.include_router(wali_santri_r)
