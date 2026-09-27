"""HTTP surface for the isolated multi-role madrasah module.

Mounted in main.py as prefix=/api/madrasah only. Does not touch BUMDes routers.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import (
    AbsenBulkRequest,
    AbsenMapelBulkRequest,
    BukuKasIn,
    GuruIn,
    JadwalIn,
    KegiatanIn,
    LoginRequest,
    MapelIn,
    MapelPatch,
    MateriIn,
    MateriPatch,
    PendaftaranIn,
    PendaftaranPatch,
    PenugasanIn,
    KenaikanKelasRequest,
    MadrasahUnitIn,
    MadrasahUnitPatch,
    PengaturanPatch,
    PengumumanIn,
    PesanIn,
    PlacementIn,
    ProgresCreateRequest,
    ProgresPatch,
    RombelIn,
    RombelPatch,
    SantriIn,
    SantriPatch,
    SantriStatusIn,
    SemesterIn,
    TahunAjaranIn,
    TingkatIn,
    TingkatPatch,
    UserPatch,
    YayasanPatch,
)
from tenants.madrasah.modules.madrasah.infrastructure.auth import (
    check_login_rate_limit,
    check_public_submission_rate_limit,
    clear_madrasah_cookie,
    issue_madrasah_token,
    record_failed_login,
    require_roles_madrasah,
    reset_login_attempts,
    set_madrasah_cookie,
)
from tenants.madrasah.modules.madrasah.infrastructure.database import get_db_madrasah
from tenants.madrasah.modules.madrasah.infrastructure.models import UserMadrasah
from tenants.madrasah.modules.madrasah.infrastructure.seeder import reset_madrasah, seed_madrasah
from tenants.madrasah.scripts.seed_demo_data import seed_demo_data

madrasah_router = APIRouter()
router = madrasah_router
admin_r = APIRouter(prefix="/admin", tags=["Madrasah Admin"])
kurikulum_r = APIRouter(prefix="/kurikulum", tags=["Madrasah Kurikulum"])
bendahara_r = APIRouter(prefix="/bendahara", tags=["Madrasah Bendahara"])
wali_kelas_r = APIRouter(prefix="/wali-kelas", tags=["Madrasah Wali Kelas"])
wali_santri_r = APIRouter(prefix="/wali-santri", tags=["Madrasah Wali Santri"])
guru_mapel_r = APIRouter(prefix="/guru-mapel", tags=["Madrasah Guru Mapel"])
keuangan_r = APIRouter(prefix="/keuangan", tags=["Madrasah Keuangan"])

# Role groups mirror src/App.jsx <Guard roles={[...]}> exactly, so a request that
# would be blocked from reaching a page in the frontend is also rejected by the
# backend, instead of relying only on client-side route guarding.
ADMIN_ROLES = ("kepala_sekolah", "admin")
KURIKULUM_ROLES = ("kurikulum", "kepala_sekolah", "admin")
BENDAHARA_ROLES = ("bendahara", "kepala_sekolah", "admin")
WALI_KELAS_ROLES = ("wali_kelas", "guru", "kepala_sekolah", "admin")
WALI_SANTRI_ROLES = ("wali_santri", "kepala_sekolah", "admin")
# Wali kelas "mewarisi" semua tugas guru mapel (lihat pembagian peran), jadi
# role wali_kelas ikut disertakan di sini, bukan cuma "guru".
GURU_MAPEL_ROLES = ("guru", "wali_kelas", "kepala_sekolah", "admin")
ANY_AUTHENTICATED = ADMIN_ROLES + KURIKULUM_ROLES + BENDAHARA_ROLES + WALI_KELAS_ROLES + WALI_SANTRI_ROLES
# yayasan_admin melihat rekap lintas-unit read-only (Fase 3) -- tidak ikut
# CRUD operasional harian satu unit, itu tetap wewenang ADMIN_ROLES di unit
# masing-masing.
YAYASAN_ROLES = ("yayasan_admin",) + ADMIN_ROLES


def _user_out(user) -> dict:
    return services.user_out(user)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@madrasah_router.get("/seed-now")
async def seed_now(session: AsyncSession = Depends(get_db_madrasah)):
    try:
        ids = await seed_madrasah(session)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {"status": "Database madrasah berhasil diisi data awal JWT", "ids": ids}


@admin_r.get("/seed-demo-data")
async def seed_demo_data_now(
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    # Hanya untuk database trial/demo -- lihat docstring seed_demo_data().
    # Login admin wajib (bukan endpoint publik seperti /seed-now) karena ini
    # menulis puluhan baris data contoh, bukan sekadar 2 akun default.
    # Idempotent: no-op kalau sudah pernah dipanggil sebelumnya (lihat
    # TingkatMadrasah guard di seed_demo_data()).
    try:
        result = await seed_demo_data(session)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if result.get("status") == "created":
        await services.record_audit(
            session, aktor=user, aksi="seed_demo_data", entitas="madrasah_*",
            keterangan="Mengisi database dengan data demo (santri, rombel, guru, dst).",
        )
        await session.commit()
    return result


@madrasah_router.post("/reset-now")
async def reset_now(
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    # DESTRUCTIVE -- lihat docstring reset_madrasah(). Dibuat sebagai
    # endpoint terpisah dari /seed-now yang idempotent, supaya redeploy
    # rutin tidak bisa tidak sengaja menghapus data pelanggan.
    # Wajib login sebagai admin/kepala sekolah: sebelumnya endpoint ini bisa
    # dipanggil siapa pun tanpa autentikasi dan langsung men-drop seluruh
    # tabel madrasah_* -- lihat audit modul, temuan P0.
    try:
        ids = await reset_madrasah(session)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    # reset_madrasah men-drop+membuat ulang tabel lewat koneksi engine
    # terpisah (engine.begin()), jadi baris audit ini aman ditulis lewat
    # `session` milik request seperti biasa -- ditulis SETELAH reset. aktor
    # dikirim None (bukan objek `user`): baris madrasah_users milik admin
    # yang memicu reset ini sendiri sudah ikut di-drop, jadi FK aktor_id
    # tidak lagi merujuk baris yang ada -- nama/role disimpan sebagai teks
    # di keterangan saja.
    await services.record_audit(
        session,
        aktor=None,
        aksi="reset_database",
        entitas="madrasah_*",
        keterangan=f"Dipicu oleh {user.nama} ({user.role}) -- reset total ke 2 akun default",
        ip=_client_ip(request),
    )
    return {"status": "Database madrasah di-reset total ke 2 akun default (admin, guru)", "ids": ids}


@madrasah_router.post("/auth/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_madrasah),
):
    # Throttle sebelum menyentuh DB: batasi percobaan login per pasangan
    # ip+no_hp supaya brute force terhadap satu akun (termasuk password
    # default hasil seed) tidak bisa dicoba tanpa batas -- lihat audit
    # modul, temuan P0.
    check_login_rate_limit(request, payload.no_hp)
    try:
        user = await services.login_by_phone(session, payload)
    except services.MadrasahAuthError as exc:
        record_failed_login(request, payload.no_hp)
        await services.record_audit(
            session,
            aktor=None,
            aksi="login_gagal",
            entitas="madrasah_users",
            keterangan=f"no_hp={payload.no_hp.strip()}",
            ip=_client_ip(request),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    reset_login_attempts(request, payload.no_hp)
    await services.record_audit(
        session, aktor=user, aksi="login_sukses", entitas="madrasah_users", entitas_id=user.id, ip=_client_ip(request)
    )
    token = issue_madrasah_token(user)
    set_madrasah_cookie(response, token)
    return {"user": _user_out(user)}


@madrasah_router.post("/auth/logout")
async def logout(response: Response):
    clear_madrasah_cookie(response)
    return {"status": "ok"}


@madrasah_router.get("/kelas")
async def get_kelas(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ANY_AUTHENTICATED)),
):
    rows = await services.list_kelas(session)
    return [{"id": row.id, "nama_kelas": row.nama_kelas} for row in rows]


@madrasah_router.get("/santri")
async def get_santri(
    kelas_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ANY_AUTHENTICATED)),
):
    # Untuk role wali_kelas, kelas_id yang dikirim client DIABAIKAN --
    # list_santri_for_caller memaksa hasil ke rombel milik wali kelas itu
    # sendiri, menutup celah client mengganti kelas_id untuk lihat kelas lain.
    rows = await services.list_santri_for_caller(session, user, kelas_id)
    return [{"id": row.id, "nama": row.nama, "kelas_id": row.kelas_id, "rombel_id": getattr(row, "rombel_id", None), "orang_tua_id": row.orang_tua_id} for row in rows]


@madrasah_router.post("/absensi/bulk", status_code=status.HTTP_201_CREATED)
async def absensi_bulk(
    payload: AbsenBulkRequest,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    rows = await services.bulk_insert_absensi(session, payload)
    return {"inserted": len(rows), "ids": [getattr(row, "id", None) for row in rows]}


@madrasah_router.post("/progres", status_code=status.HTTP_201_CREATED)
async def create_progres(
    payload: ProgresCreateRequest,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    try:
        row = await services.create_progres(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"id": row.id, "tanggal": row.tanggal.isoformat(), "santri_id": row.santri_id, "tipe": row.tipe, "capaian": row.capaian, "catatan_guru": row.catatan_guru, "mapel_id": row.mapel_id, "materi_id": row.materi_id}


@madrasah_router.get("/tagihan/{santri_id}")
async def get_tagihan(
    santri_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ANY_AUTHENTICATED)),
):
    return [services.tagihan_out(row) for row in await services.list_tagihan(session, santri_id)]


@madrasah_router.post("/spp/generate", status_code=status.HTTP_201_CREATED)
async def spp_generate(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*BENDAHARA_ROLES)),
):
    return [services.tagihan_out(row) for row in await services.generate_spp_massal(session)]


@madrasah_router.post("/spp/pay/{id}")
async def spp_pay(
    id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*BENDAHARA_ROLES)),
):
    try:
        row = await services.pay_spp_manual(session, id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await services.record_audit(
        session, aktor=user, aksi="spp_lunas", entitas="madrasah_tagihan_syahriyah", entitas_id=row.id, ip=_client_ip(request)
    )
    return services.tagihan_out(row)


@madrasah_router.get("/pengaturan")
async def get_pengaturan(session: AsyncSession = Depends(get_db_madrasah)):
    # Intentionally left WITHOUT an auth dependency: Landing.jsx dan Login.jsx
    # (halaman publik, sebelum login) menampilkan nama & logo madrasah dari
    # sini. Sama seperti /pengumuman di bawah.
    row = await services.get_pengaturan(session)
    return services.pengaturan_out(row)


@admin_r.patch("/pengaturan")
async def admin_pengaturan_patch(
    payload: PengaturanPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    row = await services.update_pengaturan(session, payload)
    return services.pengaturan_out(row)


@madrasah_router.get("/pengumuman")
async def get_pengumuman(session: AsyncSession = Depends(get_db_madrasah)):
    # Intentionally left WITHOUT an auth dependency: Landing.jsx (the public
    # homepage, route "/") calls this before any login. Adding auth here would
    # break the public announcements shown to visitors who haven't logged in.
    rows = await services.list_pengumuman(session)
    return [{"id": row.id, "judul": row.judul, "isi": row.isi, "tanggal": row.tanggal.isoformat(), "dibuat_by": row.dibuat_by} for row in rows]


@madrasah_router.get("/kegiatan")
async def get_kegiatan(session: AsyncSession = Depends(get_db_madrasah)):
    # Publik juga (landing page, section "Kegiatan & Program"), sama
    # alasannya dengan /pengumuman dan /pengaturan di atas.
    rows = await services.list_kegiatan(session)
    return [services.kegiatan_out(row) for row in rows]


@madrasah_router.post("/pendaftaran", status_code=status.HTTP_201_CREATED)
async def public_pendaftaran_create(
    payload: PendaftaranIn,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
):
    # Form PSB publik -- calon wali santri belum punya akun. Di-rate-limit
    # per IP (lihat check_public_submission_rate_limit) karena endpoint POST
    # tanpa auth adalah target spam. Hanya balas id+status, bukan seluruh
    # data pribadi yang baru saja dikirim, seperlunya untuk konfirmasi UI.
    check_public_submission_rate_limit(request, "pendaftaran")
    row = await services.create_pendaftaran(session, payload)
    return {"id": row.id, "status": row.status}


@admin_r.get("/tingkat")
async def admin_tingkat(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return [{"id": r.id, "nama": r.nama, "urutan": r.urutan} for r in await services.list_tingkat(session)]


@admin_r.post("/tingkat", status_code=status.HTTP_201_CREATED)
async def admin_tingkat_create(
    payload: TingkatIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    row = await services.create_tingkat(session, payload)
    return {"id": row.id, "nama": row.nama, "urutan": row.urutan}


@admin_r.patch("/tingkat/{tingkat_id}")
async def admin_tingkat_patch(
    tingkat_id: str,
    payload: TingkatPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.patch_tingkat(session, tingkat_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "nama": row.nama, "urutan": row.urutan}


@admin_r.delete("/tingkat/{tingkat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_tingkat_delete(
    tingkat_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        await services.delete_tingkat(session, tingkat_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@admin_r.get("/rombel")
async def admin_rombel(
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ANY_AUTHENTICATED)),
):
    # Kept accessible to any authenticated role (not just ADMIN_ROLES) because
    # WaliKelasPortal.jsx and KurikulumPortal.jsx also call this endpoint to
    # populate rombel dropdowns. list_rombel_for_caller now scopes the result
    # to the caller's own rombel when role == "wali_kelas"; other roles
    # (admin, kepala_sekolah, kurikulum, guru) still see the full list, which
    # they legitimately need for their own screens.
    return [{"id": r.id, "nama": r.nama, "tingkat_id": r.tingkat_id, "wali_kelas_id": r.wali_kelas_id, "wali_kelas": r.wali_kelas.nama if r.wali_kelas else None} for r in await services.list_rombel_for_caller(session, user)]


@admin_r.post("/rombel", status_code=status.HTTP_201_CREATED)
async def admin_rombel_create(
    payload: RombelIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    row = await services.create_rombel(session, payload)
    return {"id": row.id, "nama": row.nama, "tingkat_id": row.tingkat_id, "wali_kelas_id": row.wali_kelas_id}


@admin_r.delete("/rombel/{rombel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_rombel_delete(
    rombel_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        await services.delete_rombel(session, rombel_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@admin_r.get("/guru")
async def admin_guru(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES, *KURIKULUM_ROLES)),
):
    # GET dilonggarkan untuk KURIKULUM_ROLES juga: KurikulumPortal.jsx perlu
    # daftar guru untuk UI penugasan guru<->mapel<->rombel. POST di bawah
    # (buat akun guru baru) tetap eksklusif ADMIN_ROLES.
    return [services.user_out(r) for r in await services.list_guru(session)]


@admin_r.get("/wali-santri")
async def admin_wali_santri(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return [services.user_out(r) for r in await services.list_wali_santri(session)]


@admin_r.post("/guru", status_code=status.HTTP_201_CREATED)
async def admin_guru_create(
    payload: GuruIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return services.user_out(await services.create_guru(session, payload))


@admin_r.patch("/guru/{user_id}")
async def admin_guru_patch(
    user_id: str,
    payload: UserPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.patch_guru(session, user_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return services.user_out(row)


@admin_r.delete("/guru/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_guru_delete(
    user_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        await services.delete_guru(session, user_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await services.record_audit(
        session, aktor=user, aksi="hapus_akun", entitas="madrasah_users", entitas_id=user_id, ip=_client_ip(request)
    )


@admin_r.post("/wali-santri", status_code=status.HTTP_201_CREATED)
async def admin_wali_santri_create(
    payload: GuruIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return services.user_out(await services.create_wali_santri(session, payload))


@admin_r.patch("/wali-santri/{user_id}")
async def admin_wali_santri_patch(
    user_id: str,
    payload: UserPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.patch_guru(session, user_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return services.user_out(row)


@admin_r.delete("/wali-santri/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_wali_santri_delete(
    user_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        await services.delete_guru(session, user_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await services.record_audit(
        session, aktor=user, aksi="hapus_akun", entitas="madrasah_users", entitas_id=user_id, ip=_client_ip(request)
    )


@admin_r.post("/santri", status_code=status.HTTP_201_CREATED)
async def admin_santri_create(
    payload: SantriIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    row = await services.create_santri(session, payload)
    return {"id": row.id, "nama": row.nama, "rombel_id": row.rombel_id}


@admin_r.get("/santri")
async def admin_santri_list(
    status: str | None = Query(default="semua"),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    # Beda dari GET /santri (root, di-scope per-role): ini khusus admin untuk
    # keperluan kelola data lengkap -- default "semua" (termasuk lulus/keluar/
    # pindah), beda dari list_santri() lain yang default cuma santri aktif.
    rows = await services.list_santri(session, None, status=status)
    return [
        {"id": r.id, "nama": r.nama, "rombel_id": r.rombel_id, "orang_tua_id": r.orang_tua_id, "status": r.status}
        for r in rows
    ]


@admin_r.patch("/santri/{santri_id}")
async def admin_santri_patch(
    santri_id: str,
    payload: SantriPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.patch_santri(session, santri_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "nama": row.nama, "rombel_id": row.rombel_id, "orang_tua_id": row.orang_tua_id}


@admin_r.delete("/santri/{santri_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_santri_delete(
    santri_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        await services.delete_santri(session, santri_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await services.record_audit(
        session, aktor=user, aksi="hapus_santri", entitas="madrasah_santri", entitas_id=santri_id, ip=_client_ip(request)
    )


@admin_r.post("/pengumuman", status_code=status.HTTP_201_CREATED)
async def admin_pengumuman_create(
    payload: PengumumanIn,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    row = await services.create_pengumuman(session, payload, user.id)
    return {"id": row.id, "judul": row.judul, "isi": row.isi, "tanggal": row.tanggal.isoformat()}


@admin_r.post("/kegiatan", status_code=status.HTTP_201_CREATED)
async def admin_kegiatan_create(
    payload: KegiatanIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    row = await services.create_kegiatan(session, payload)
    return services.kegiatan_out(row)


@admin_r.delete("/kegiatan/{kegiatan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_kegiatan_delete(
    kegiatan_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        await services.delete_kegiatan(session, kegiatan_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@admin_r.get("/pendaftaran")
async def admin_pendaftaran_list(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    rows = await services.list_pendaftaran(session)
    return [services.pendaftaran_out(row) for row in rows]


@admin_r.patch("/pendaftaran/{pendaftaran_id}")
async def admin_pendaftaran_patch(
    pendaftaran_id: str,
    payload: PendaftaranPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.patch_pendaftaran(session, pendaftaran_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return services.pendaftaran_out(row)


@admin_r.get("/rekap")
async def admin_rekap(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return await services.rekap_umum(session)


@admin_r.post("/penempatan")
async def admin_place(
    payload: PlacementIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.place_santri(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "nama": row.nama, "rombel_id": row.rombel_id}


@admin_r.get("/santri/{santri_id}/riwayat-kelas")
async def admin_santri_riwayat_kelas(
    santri_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES, *KURIKULUM_ROLES)),
):
    return await services.riwayat_kelas_santri(session, santri_id)


@admin_r.post("/santri/{santri_id}/status")
async def admin_santri_status(
    santri_id: str,
    payload: SantriStatusIn,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.set_status_santri(session, santri_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await services.record_audit(
        session,
        aktor=user,
        aksi="ubah_status_santri",
        entitas="madrasah_santri",
        entitas_id=santri_id,
        keterangan=f"status -> {payload.status}",
        ip=_client_ip(request),
    )
    return {"id": row.id, "nama": row.nama, "status": row.status, "tanggal_status": row.tanggal_status.isoformat() if row.tanggal_status else None}


@admin_r.post("/kenaikan-kelas", status_code=status.HTTP_201_CREATED)
async def admin_kenaikan_kelas(
    payload: KenaikanKelasRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        hasil = await services.kenaikan_kelas_massal(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await services.record_audit(
        session,
        aktor=user,
        aksi="kenaikan_kelas_massal",
        entitas="madrasah_santri",
        keterangan=f"dipindah={len(hasil['dipindah'])} diluluskan={len(hasil['diluluskan'])}",
        ip=_client_ip(request),
    )
    return hasil


@kurikulum_r.get("/mapel")
async def kur_mapel(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ANY_AUTHENTICATED)),
):
    # Accessible to any authenticated role: WaliKelasPortal.jsx also reads
    # mapel (via services/waliKelas.js -> /wali-kelas/kurikulum, a separate
    # endpoint below) but KurikulumPortal's own createMateri/createMapel flow
    # needs this GET too. Mutations below remain restricted to KURIKULUM_ROLES.
    return [{"id": r.id, "kode": r.kode, "nama": r.nama, "materi": [{"id": m.id, "judul": m.judul, "urutan": m.urutan, "aktif": m.aktif} for m in r.materi]} for r in await services.list_mapel(session)]


@kurikulum_r.post("/mapel", status_code=status.HTTP_201_CREATED)
async def kur_mapel_create(
    payload: MapelIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    row = await services.create_mapel(session, payload)
    return {"id": row.id, "kode": row.kode, "nama": row.nama}


@kurikulum_r.patch("/mapel/{mapel_id}")
async def kur_mapel_patch(
    mapel_id: str,
    payload: MapelPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    try:
        row = await services.patch_mapel(session, mapel_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "kode": row.kode, "nama": row.nama}


@kurikulum_r.delete("/mapel/{mapel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def kur_mapel_delete(
    mapel_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    try:
        await services.delete_mapel(session, mapel_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@kurikulum_r.post("/materi", status_code=status.HTTP_201_CREATED)
async def kur_materi_create(
    payload: MateriIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    try:
        row = await services.create_materi(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "mapel_id": row.mapel_id, "judul": row.judul, "urutan": row.urutan, "aktif": row.aktif}


@kurikulum_r.patch("/materi/{materi_id}")
async def kur_materi_patch(
    materi_id: str,
    payload: MateriPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    try:
        row = await services.patch_materi(session, materi_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "judul": row.judul, "urutan": row.urutan, "aktif": row.aktif}


@kurikulum_r.delete("/materi/{materi_id}", status_code=status.HTTP_204_NO_CONTENT)
async def kur_materi_delete(
    materi_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    try:
        await services.delete_materi(session, materi_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@kurikulum_r.get("/jadwal")
async def kur_jadwal(
    rombel_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    return [{"id": r.id, "rombel_id": r.rombel_id, "mapel_id": r.mapel_id, "mapel": r.mapel.nama if r.mapel else None, "hari": r.hari, "jam_mulai": r.jam_mulai, "jam_selesai": r.jam_selesai} for r in await services.list_jadwal(session, rombel_id)]


@kurikulum_r.post("/jadwal", status_code=status.HTTP_201_CREATED)
async def kur_jadwal_create(
    payload: JadwalIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    row = await services.create_jadwal(session, payload)
    return {"id": row.id, "rombel_id": row.rombel_id, "mapel_id": row.mapel_id, "hari": row.hari}


@kurikulum_r.delete("/jadwal/{jadwal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def kur_jadwal_delete(
    jadwal_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    try:
        await services.delete_jadwal(session, jadwal_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@kurikulum_r.patch("/rombel/{rombel_id}")
async def kur_rombel_patch(
    rombel_id: str,
    payload: RombelPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    try:
        row = await services.patch_rombel(session, rombel_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "nama": row.nama, "tingkat_id": row.tingkat_id, "wali_kelas_id": row.wali_kelas_id}


@bendahara_r.post("/spp/generate", status_code=status.HTTP_201_CREATED)
async def ben_generate(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*BENDAHARA_ROLES)),
):
    return [services.tagihan_out(r) for r in await services.generate_spp_massal(session)]


@bendahara_r.get("/spp/menunggu")
async def ben_menunggu(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*BENDAHARA_ROLES)),
):
    return [services.tagihan_out(r) for r in await services.list_tagihan_menunggu(session)]


@bendahara_r.get("/spp")
async def ben_spp_semua(
    bulan_tahun: str | None = None,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*BENDAHARA_ROLES)),
):
    return [services.tagihan_out(r) for r in await services.list_tagihan_semua(session, bulan_tahun)]


@bendahara_r.delete("/spp/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def ben_delete_spp(
    id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*BENDAHARA_ROLES)),
):
    try:
        await services.delete_tagihan(session, id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    await services.record_audit(
        session, aktor=user, aksi="hapus_tagihan", entitas="madrasah_tagihan_syahriyah", entitas_id=id, ip=_client_ip(request)
    )


@bendahara_r.post("/spp/pay/{id}")
async def ben_pay(
    id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*BENDAHARA_ROLES)),
):
    try:
        row = await services.pay_spp_manual(session, id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await services.record_audit(
        session, aktor=user, aksi="spp_lunas", entitas="madrasah_tagihan_syahriyah", entitas_id=row.id, ip=_client_ip(request)
    )
    return services.tagihan_out(row)


@wali_kelas_r.get("/kurikulum")
async def wk_kurikulum(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    return [{"id": r.id, "kode": r.kode, "nama": r.nama, "materi": [{"id": m.id, "judul": m.judul, "urutan": m.urutan} for m in r.materi if m.aktif]} for r in await services.list_mapel(session)]


@wali_kelas_r.post("/absensi", status_code=status.HTTP_201_CREATED)
async def wk_absen(
    payload: AbsenBulkRequest,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    try:
        rows = await services.bulk_insert_absensi(session, payload, guru=user)
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"inserted": len(rows)}


@wali_kelas_r.post("/progres", status_code=status.HTTP_201_CREATED)
async def wk_progres(
    payload: ProgresCreateRequest,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    try:
        row = await services.create_progres(session, payload, guru=user)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"id": row.id, "santri_id": row.santri_id, "capaian": row.capaian, "materi_id": row.materi_id}


@wali_kelas_r.patch("/progres/{progres_id}")
async def wk_progres_patch(
    progres_id: str,
    payload: ProgresPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    try:
        row = await services.patch_progres(session, user, progres_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"id": row.id, "santri_id": row.santri_id, "capaian": row.capaian, "catatan_guru": row.catatan_guru}


@wali_kelas_r.get("/tagihan")
async def wk_tagihan(
    rombel_id: str = Query(...),
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    try:
        await services.assert_own_rombel(session, user, rombel_id)
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return [services.tagihan_out(r) for r in await services.list_tagihan_rombel(session, rombel_id)]


@wali_kelas_r.post("/tagihan/ajukan/{tagihan_id}")
async def wk_tagihan_ajukan(
    tagihan_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    try:
        row = await services.ajukan_pembayaran(session, user, tagihan_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return services.tagihan_out(row)


@wali_kelas_r.patch("/santri/{santri_id}")
async def wk_santri_patch(
    santri_id: str,
    payload: SantriPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    try:
        row = await services.patch_santri_wali_kelas(session, user, santri_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"id": row.id, "nama": row.nama, "orang_tua_id": row.orang_tua_id}


@wali_kelas_r.post("/pengumuman", status_code=status.HTTP_201_CREATED)
async def wk_pengumuman_create(
    payload: PengumumanIn,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    row = await services.create_pengumuman(session, payload, user.id)
    return {"id": row.id, "judul": row.judul, "isi": row.isi, "tanggal": row.tanggal.isoformat()}


@wali_kelas_r.get("/pesan/{santri_id}")
async def wk_pesan_list(
    santri_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    try:
        await services.assert_own_rombel_santri(session, user, [santri_id])
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return await services.list_pesan(session, santri_id)


@wali_kelas_r.post("/pesan", status_code=status.HTTP_201_CREATED)
async def wk_pesan_create(
    payload: PesanIn,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_KELAS_ROLES)),
):
    try:
        row = await services.kirim_pesan(session, user, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"id": row.id, "isi": row.isi}


@wali_santri_r.get("/pesan/{santri_id}")
async def ws_pesan_list(
    santri_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_SANTRI_ROLES)),
):
    try:
        await services.assert_own_child(session, user, santri_id)
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return await services.list_pesan(session, santri_id)


@wali_santri_r.post("/pesan", status_code=status.HTTP_201_CREATED)
async def ws_pesan_create(
    payload: PesanIn,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_SANTRI_ROLES)),
):
    try:
        row = await services.kirim_pesan(session, user, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"id": row.id, "isi": row.isi}


@wali_santri_r.get("/progres/{santri_id}")
async def ws_progres(
    santri_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_SANTRI_ROLES)),
):
    try:
        await services.assert_own_child(session, user, santri_id)
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return await services.progres_series(session, santri_id)


@wali_santri_r.get("/tagihan/{santri_id}")
async def ws_tagihan(
    santri_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*WALI_SANTRI_ROLES)),
):
    try:
        await services.assert_own_child(session, user, santri_id)
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return [services.tagihan_out(r) for r in await services.list_tagihan(session, santri_id)]


# --- Kurikulum: kelola penugasan guru mapel (fondasi baru) ---

@kurikulum_r.get("/guru-mapel")
async def kur_guru_mapel_list(
    guru_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    rows = await services.list_penugasan(session, guru_id)
    return [
        {
            "id": r.id,
            "guru_id": r.guru_id,
            "guru": r.guru.nama if r.guru else None,
            "mapel_id": r.mapel_id,
            "mapel": r.mapel.nama if r.mapel else None,
            "rombel_id": r.rombel_id,
            "rombel": r.rombel.nama if r.rombel else None,
        }
        for r in rows
    ]


@kurikulum_r.post("/guru-mapel", status_code=status.HTTP_201_CREATED)
async def kur_guru_mapel_create(
    payload: PenugasanIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    row = await services.assign_guru_mapel(session, payload)
    return {"id": row.id, "guru_id": row.guru_id, "mapel_id": row.mapel_id, "rombel_id": row.rombel_id}


@kurikulum_r.delete("/guru-mapel/{penugasan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def kur_guru_mapel_delete(
    penugasan_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*KURIKULUM_ROLES)),
):
    try:
        await services.remove_penugasan(session, penugasan_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- Guru Mapel: portal operasional (fondasi baru) ---

@guru_mapel_r.get("/penugasan-saya")
async def gm_penugasan_saya(
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*GURU_MAPEL_ROLES)),
):
    rows = await services.list_penugasan(session, user.id)
    return [
        {"id": r.id, "mapel_id": r.mapel_id, "mapel": r.mapel.nama if r.mapel else None, "rombel_id": r.rombel_id, "rombel": r.rombel.nama if r.rombel else None}
        for r in rows
    ]


@guru_mapel_r.get("/santri")
async def gm_santri(
    rombel_id: str = Query(...),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*GURU_MAPEL_ROLES)),
):
    rows = await services.list_santri(session, rombel_id)
    return [{"id": r.id, "nama": r.nama} for r in rows]


@guru_mapel_r.post("/absensi", status_code=status.HTTP_201_CREATED)
async def gm_absensi(
    payload: AbsenMapelBulkRequest,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*GURU_MAPEL_ROLES)),
):
    try:
        rows = await services.bulk_insert_absensi_mapel(session, user.id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"inserted": len(rows)}


@guru_mapel_r.get("/rekap-absensi")
async def gm_rekap_absensi(
    rombel_id: str = Query(...),
    mapel_id: str = Query(...),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*GURU_MAPEL_ROLES)),
):
    return await services.rekap_absensi_mapel(session, rombel_id, mapel_id)


@guru_mapel_r.post("/progres", status_code=status.HTTP_201_CREATED)
async def gm_progres(
    payload: ProgresCreateRequest,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*GURU_MAPEL_ROLES)),
):
    try:
        if payload.mapel_id:
            await services.assert_guru_mengajar_santri(session, user, payload.mapel_id, payload.santri_id)
        row = await services.create_progres(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"id": row.id, "santri_id": row.santri_id, "capaian": row.capaian, "mapel_id": row.mapel_id, "materi_id": row.materi_id}


@guru_mapel_r.get("/rapor/{santri_id}")
async def gm_rapor(
    santri_id: str,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*GURU_MAPEL_ROLES)),
):
    try:
        return await services.rapor_santri(session, user, santri_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@keuangan_r.get("/buku-kas")
async def keuangan_buku_kas_list(
    bulan: str | None = Query(None, description="Filter YYYY-MM"),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    rows = await services.list_buku_kas(session, bulan)
    return [services.buku_kas_out(r) for r in rows]


@keuangan_r.post("/buku-kas", status_code=status.HTTP_201_CREATED)
async def keuangan_buku_kas_create(
    payload: BukuKasIn,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    row = await services.create_buku_kas_entry(session, payload, dicatat_oleh=user.id)
    await services.record_audit(
        session,
        aktor=user,
        aksi="buku_kas_catat",
        entitas="madrasah_buku_kas",
        entitas_id=row.id,
        keterangan=f"{row.tipe} {row.kategori} {row.jumlah}",
        ip=_client_ip(request),
    )
    return services.buku_kas_out(row)


@keuangan_r.get("/laporan")
async def keuangan_laporan(
    bulan: str | None = Query(None, description="Filter YYYY-MM"),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return await services.laporan_keuangan(session, bulan)


# --- COA & jurnal double-entry (Fase 2.1) ---

@keuangan_r.get("/akun")
async def keuangan_akun_list(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return [services.akun_out(r) for r in await services.list_akun(session)]


@keuangan_r.get("/jurnal")
async def keuangan_jurnal_list(
    bulan: str | None = Query(None, description="Filter YYYY-MM"),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return [services.jurnal_out(r) for r in await services.list_jurnal(session, bulan)]


@keuangan_r.get("/laba-rugi")
async def keuangan_laba_rugi(
    bulan: str | None = Query(None, description="Filter YYYY-MM"),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return await services.laba_rugi(session, bulan)


# --- Honor mengajar / payroll (Fase 2.2) ---

@keuangan_r.get("/honor")
async def keuangan_honor_list(
    bulan: str | None = Query(None, description="Filter YYYY-MM"),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return [services.honor_out(r) for r in await services.list_honor(session, bulan)]


@keuangan_r.post("/honor/generate", status_code=status.HTTP_201_CREATED)
async def keuangan_honor_generate(
    bulan: str | None = Query(None, description="Default bulan berjalan (YYYY-MM)"),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return [services.honor_out(r) for r in await services.generate_honor_massal(session, bulan)]


@keuangan_r.post("/honor/pay/{honor_id}")
async def keuangan_honor_pay(
    honor_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.pay_honor(session, honor_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MadrasahForbiddenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await services.record_audit(
        session,
        aktor=user,
        aksi="honor_dibayar",
        entitas="madrasah_honor_mengajar",
        entitas_id=row.id,
        keterangan=f"{row.guru.nama if row.guru else '-'} {row.bulan_tahun} = {row.total}",
        ip=_client_ip(request),
    )
    return services.honor_out(row)


@admin_r.get("/audit-log")
async def admin_audit_log(
    limit: int = Query(default=200, le=500),
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return [services.audit_out(r) for r in await services.list_audit_log(session, limit)]


# --- Yayasan & MadrasahUnit (Fase 3: multi-madrasah dalam satu database) ---

@admin_r.get("/yayasan")
async def admin_yayasan_get(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return services.yayasan_out(await services.get_or_create_yayasan(session))


@admin_r.patch("/yayasan")
async def admin_yayasan_patch(
    payload: YayasanPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return services.yayasan_out(await services.update_yayasan(session, payload))


@admin_r.get("/unit")
async def admin_unit_list(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*YAYASAN_ROLES)),
):
    return [services.unit_out(r) for r in await services.list_unit(session)]


@admin_r.post("/unit", status_code=status.HTTP_201_CREATED)
async def admin_unit_create(
    payload: MadrasahUnitIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return services.unit_out(await services.create_unit(session, payload))


@admin_r.patch("/unit/{unit_id}")
async def admin_unit_patch(
    unit_id: str,
    payload: MadrasahUnitPatch,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.patch_unit(session, unit_id, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return services.unit_out(row)


@admin_r.get("/yayasan/rekap")
async def admin_yayasan_rekap(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*YAYASAN_ROLES)),
):
    return await services.rekap_yayasan(session)


# --- Tahun Ajaran & Semester (fondasi periode akademik) ---

@admin_r.get("/tahun-ajaran")
async def admin_tahun_ajaran_list(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES, *KURIKULUM_ROLES)),
):
    return [services.tahun_ajaran_out(r) for r in await services.list_tahun_ajaran(session)]


@admin_r.post("/tahun-ajaran", status_code=status.HTTP_201_CREATED)
async def admin_tahun_ajaran_create(
    payload: TahunAjaranIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    return services.tahun_ajaran_out(await services.create_tahun_ajaran(session, payload))


@admin_r.get("/semester")
async def admin_semester_list(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES, *KURIKULUM_ROLES)),
):
    return [services.semester_out(r) for r in await services.list_semester(session)]


@admin_r.post("/semester", status_code=status.HTTP_201_CREATED)
async def admin_semester_create(
    payload: SemesterIn,
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.create_semester(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return services.semester_out(row)


@admin_r.post("/semester/{semester_id}/aktifkan")
async def admin_semester_aktifkan(
    semester_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.aktifkan_semester(session, semester_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await services.record_audit(
        session,
        aktor=user,
        aksi="semester_aktifkan",
        entitas="madrasah_semester",
        entitas_id=row.id,
        keterangan=f"{row.tahun_ajaran.kode if row.tahun_ajaran else '-'} {row.nama}",
        ip=_client_ip(request),
    )
    return services.semester_out(row)


@admin_r.post("/semester/{semester_id}/tutup")
async def admin_semester_tutup(
    semester_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
    user: UserMadrasah = Depends(require_roles_madrasah(*ADMIN_ROLES)),
):
    try:
        row = await services.tutup_semester(session, semester_id)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await services.record_audit(
        session,
        aktor=user,
        aksi="semester_tutup",
        entitas="madrasah_semester",
        entitas_id=row.id,
        keterangan=f"{row.tahun_ajaran.kode if row.tahun_ajaran else '-'} {row.nama}",
        ip=_client_ip(request),
    )
    return services.semester_out(row)


@madrasah_router.get("/semester/aktif")
async def get_semester_aktif(
    session: AsyncSession = Depends(get_db_madrasah),
    _: UserMadrasah = Depends(require_roles_madrasah(*ANY_AUTHENTICATED)),
):
    # Dibaca setiap portal (bukan cuma admin) supaya frontend bisa
    # menampilkan periode akademik yang sedang berjalan di header, dan
    # menonaktifkan form input kalau belum ada semester yang diaktifkan.
    row = await services.get_semester_aktif(session)
    return services.semester_out(row) if row else None


madrasah_router.include_router(admin_r)
madrasah_router.include_router(kurikulum_r)
madrasah_router.include_router(bendahara_r)
madrasah_router.include_router(wali_kelas_r)
madrasah_router.include_router(wali_santri_r)
madrasah_router.include_router(guru_mapel_r)
madrasah_router.include_router(keuangan_r)
