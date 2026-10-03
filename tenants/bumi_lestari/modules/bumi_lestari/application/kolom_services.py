"""Kelola definisi kolom tambahan & label kolom inti (spesifikasi 10.4, AB-DM-1..13)."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.kolom_core import (
    ENTITAS,
    kolom_inti,
    kunci_dari_label,
    validasi_nilai,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_kolom import (
    DefinisiKolomIn,
    DefinisiKolomOut,
    DefinisiKolomPatch,
    LabelIntiIn,
    PilihanOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_kolom import (
    LAPISAN_INTI,
    LAPISAN_TAMBAHAN,
    MAKS_AKTIF,
    TIPE_LAPORAN,
    BlDefinisiKolom,
)


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


LABEL_INTI = {
    "no_order": "Kode pesanan", "tanggal_order": "Tanggal order", "harga_satuan": "Harga jual", "qty": "Qty",
    "biaya_pokok": "Biaya tukang", "tgl_diambil": "Tanggal selesai di tukang", "tgl_dikirim": "Tanggal kirim",
    "saluran_id": "Saluran", "produk_id": "Produk", "pemasok_id": "Tukang/supplier", "pelanggan_id": "Penjual lain",
    "akun_id": "Akun kas", "kategori_id": "Kategori", "sku": "SKU",
}


def _label_bawaan(kunci: str) -> str:
    return LABEL_INTI.get(kunci) or kunci.replace("_", " ").capitalize()


async def _terisi(session: AsyncSession, entitas: str, kunci: str) -> int:
    """Jumlah baris entitas yang mengisi kunci ini (dihitung di Python agar sama di SQLite & PostgreSQL)."""
    model = ENTITAS[entitas]
    rows = (await session.execute(select(model.kolom_tambahan))).scalars()
    return sum(1 for kt in rows if kt and kt.get(kunci) not in (None, ""))


def _out(d: BlDefinisiKolom, terisi: int | None = None) -> DefinisiKolomOut:
    return DefinisiKolomOut(
        id=d.id, entitas=d.entitas, kunci=d.kunci, lapisan=d.lapisan, label=d.label, label_bawaan=d.label_bawaan,
        tipe=d.tipe, wajib=d.wajib, pilihan=[PilihanOut(**p) for p in d.pilihan or []], nilai_bawaan=d.nilai_bawaan,
        min=d.min, maks=d.maks, tampil_form=d.tampil_form, tampil_tabel=d.tampil_tabel, bisa_filter=d.bisa_filter,
        ikut_ekspor=d.ikut_ekspor, untuk_laporan=d.untuk_laporan, tampil_staf=d.tampil_staf, urutan=d.urutan, aktif=d.aktif,
        terisi=terisi,
    )


async def _semua(session: AsyncSession, entitas: str) -> list[BlDefinisiKolom]:
    return list(
        (
            await session.execute(
                select(BlDefinisiKolom).where(BlDefinisiKolom.entitas == entitas).order_by(BlDefinisiKolom.urutan, BlDefinisiKolom.created_at)
            )
        ).scalars()
    )


async def list_definisi(session: AsyncSession, user: BlUser, entitas: str) -> list[DefinisiKolomOut]:
    """Kolom inti (label dari override bila ada) + kolom tambahan. Owner: inti saja; staf: kolom Transaksi untuk staf."""
    peran = (user.role or "").lower()
    rows = await _semua(session, entitas)
    tambahan = [d for d in rows if d.lapisan == LAPISAN_TAMBAHAN]
    if peran == "staff":
        return [_out(d) for d in tambahan if entitas == "transaksi" and d.aktif and d.tampil_staf]
    override = {d.kunci: d for d in rows if d.lapisan == LAPISAN_INTI}
    hasil = []
    for i, (kunci, wajib) in enumerate(kolom_inti(entitas)):
        o = override.get(kunci)
        hasil.append(DefinisiKolomOut(
            id=o.id if o else None, entitas=entitas, kunci=kunci, lapisan=LAPISAN_INTI,
            label=o.label if o else _label_bawaan(kunci), label_bawaan=_label_bawaan(kunci), tipe=None, wajib=wajib,
            tampil_tabel=o.tampil_tabel if o else True, urutan=o.urutan if o else i,
        ))
    if peran == "admin":
        hasil += [_out(d, await _terisi(session, entitas, d.kunci)) for d in tambahan]
    return hasil


def _pilihan(daftar: list[str]) -> list[dict]:
    hasil, lihat = [], set()
    for p in daftar:
        p = p.strip()
        if p and p not in lihat:
            lihat.add(p)
            hasil.append({"nilai": p, "arsip": False})
    return hasil


def _cek_aturan(d: BlDefinisiKolom) -> None:
    if d.tipe == "pilihan" and not [p for p in d.pilihan or [] if not p.get("arsip")]:
        raise _bad("Kolom pilihan butuh minimal satu pilihan")
    if d.untuk_laporan and d.tipe not in TIPE_LAPORAN:
        raise _bad("Pengelompokan laporan hanya untuk teks, pilihan, ya/tidak, atau tanggal")
    if d.tampil_staf and d.entitas != "transaksi":
        raise _bad("Tampil untuk staf hanya untuk kolom Transaksi")
    if d.nilai_bawaan not in (None, ""):
        d.nilai_bawaan = str(validasi_nilai(d, d.nilai_bawaan))


async def _cek_maks_aktif(session: AsyncSession, entitas: str, kecuali: str | None = None) -> None:
    stmt = select(func.count(BlDefinisiKolom.id)).where(
        BlDefinisiKolom.entitas == entitas, BlDefinisiKolom.lapisan == LAPISAN_TAMBAHAN, BlDefinisiKolom.aktif.is_(True),
    )
    if kecuali:
        stmt = stmt.where(BlDefinisiKolom.id != kecuali)
    if (await session.scalar(stmt) or 0) >= MAKS_AKTIF:
        raise _bad(f"Maksimal {MAKS_AKTIF} kolom tambahan aktif per entitas; nonaktifkan kolom lain dulu")


async def create_definisi(session: AsyncSession, user: BlUser | None, payload: DefinisiKolomIn) -> DefinisiKolomOut:
    await _cek_maks_aktif(session, payload.entitas)
    inti = {k for k, _w in kolom_inti(payload.entitas)}
    dasar = kunci_dari_label(payload.label)
    ada = {d.kunci for d in await _semua(session, payload.entitas)}
    kunci, n = dasar, 2
    while kunci in ada or kunci in inti:  # AB-DM-1: unik per entitas, tidak sama dengan kolom inti
        kunci, n = f"{dasar}_{n}", n + 1
    data = payload.model_dump(exclude={"pilihan", "urutan"})
    d = BlDefinisiKolom(
        **data, kunci=kunci, lapisan=LAPISAN_TAMBAHAN, pilihan=_pilihan(payload.pilihan),
        urutan=payload.urutan if payload.urutan is not None else len(ada) + 100, dibuat_oleh=user.id if user else None,
    )
    d.label = d.label.strip()
    _cek_aturan(d)
    session.add(d)
    await session.flush()
    return _out(d, 0)


async def _ambil(session: AsyncSession, definisi_id: str) -> BlDefinisiKolom:
    d = await session.get(BlDefinisiKolom, definisi_id)
    if d is None or d.lapisan != LAPISAN_TAMBAHAN:
        raise _bad("Kolom tambahan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    return d


async def _nilai_terpakai(session: AsyncSession, entitas: str, kunci: str) -> set:
    rows = (await session.execute(select(ENTITAS[entitas].kolom_tambahan))).scalars()
    return {kt.get(kunci) for kt in rows if kt and kt.get(kunci) not in (None, "")}


async def update_definisi(session: AsyncSession, definisi_id: str, payload: DefinisiKolomPatch) -> DefinisiKolomOut:
    d = await _ambil(session, definisi_id)
    data = payload.model_dump(exclude_unset=True)
    if "tipe" in data and data["tipe"] != d.tipe and await _terisi(session, d.entitas, d.kunci):
        raise _bad("Jenis data tidak bisa diubah karena kolom sudah berisi data; buat kolom baru")  # AB-DM-2
    if data.get("aktif") and not d.aktif:
        await _cek_maks_aktif(session, d.entitas, kecuali=d.id)
    if "pilihan" in data:
        baru = _pilihan(data.pop("pilihan") or [])
        nama_baru = {p["nilai"] for p in baru}
        terpakai = await _nilai_terpakai(session, d.entitas, d.kunci)
        # AB-DM-3: pilihan lama yang sudah dipakai diarsipkan, bukan dihapus.
        arsip = [{"nilai": p["nilai"], "arsip": True} for p in d.pilihan or [] if p["nilai"] not in nama_baru and p["nilai"] in terpakai]
        d.pilihan = baru + arsip
    for k, v in data.items():
        setattr(d, k, v.strip() if isinstance(v, str) and k == "label" else v)
    _cek_aturan(d)
    await session.flush()
    return _out(d, await _terisi(session, d.entitas, d.kunci))


async def nonaktifkan(session: AsyncSession, definisi_id: str) -> DefinisiKolomOut:
    d = await _ambil(session, definisi_id)
    d.aktif = False
    await session.flush()
    return _out(d, await _terisi(session, d.entitas, d.kunci))


async def hapus(session: AsyncSession, definisi_id: str) -> None:
    d = await _ambil(session, definisi_id)
    if await _terisi(session, d.entitas, d.kunci):
        raise _bad("Kolom sudah berisi data; hanya bisa dinonaktifkan", status.HTTP_409_CONFLICT)  # AB-DM-4
    await session.delete(d)
    await session.flush()


async def ubah_label_inti(session: AsyncSession, user: BlUser | None, entitas: str, kunci: str, payload: LabelIntiIn) -> DefinisiKolomOut:
    """AB-DM-11: hanya label, urutan, tampil di tabel. Dokumen resmi tetap memakai label bawaan (AB-DM-12)."""
    if entitas not in ENTITAS or kunci not in {k for k, _w in kolom_inti(entitas)}:
        raise _bad("Kolom inti tidak ditemukan", status.HTTP_404_NOT_FOUND)
    d = (
        await session.execute(select(BlDefinisiKolom).where(BlDefinisiKolom.entitas == entitas, BlDefinisiKolom.kunci == kunci))
    ).scalar_one_or_none()
    if d is None:
        urutan = [k for k, _w in kolom_inti(entitas)].index(kunci)
        d = BlDefinisiKolom(
            entitas=entitas, kunci=kunci, lapisan=LAPISAN_INTI, label=_label_bawaan(kunci), label_bawaan=_label_bawaan(kunci),
            tipe="teks", tampil_tabel=True, urutan=urutan, dibuat_oleh=user.id if user else None,
        )
        session.add(d)
    for k, v in payload.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(d, k, v.strip() if isinstance(v, str) else v)
    await session.flush()
    return _out(d)


async def reset_label_inti(session: AsyncSession, entitas: str, kunci: str) -> None:
    """AB-DM-13: kembalikan label bawaan (baris override dihapus)."""
    d = (
        await session.execute(
            select(BlDefinisiKolom).where(
                BlDefinisiKolom.entitas == entitas, BlDefinisiKolom.kunci == kunci, BlDefinisiKolom.lapisan == LAPISAN_INTI,
            )
        )
    ).scalar_one_or_none()
    if d is not None:
        await session.delete(d)
        await session.flush()
