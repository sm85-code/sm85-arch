"""Manual figures typed straight into the report sheet.

Stored per month as one document: `sel` maps "<row>|<week start>" to a typed amount, `baris` lists rows the user added
and `hapus` the built-in rows the user removed. The sheet applies it on top of the closed books and recalculates totals.
"""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import LaporanKeuanganManual, LaporanManualAudit

# "arus_rinci" holds the breakdown of the cash-flow page (renamed lines and split rows); it never feeds the report totals.
TABEL = ("pemasukan", "pengeluaran", "arus_kas", "neraca", "arus_rinci")
KUNCI_SEL = re.compile(r"^[^|\x00-\x1f]{1,120}\|\d{4}-\d{2}-\d{2}$")  # row keys include shop names, e.g. "shop:Pemasukan Toko [RISKA]"
MAX_BARIS, MAX_SEL = 200, 5000


class Baris(BaseModel):
    id: str = Field(pattern=r"^c:[\w\-]{1,40}$")
    tabel: str
    grup: str = Field(max_length=80)
    label: str = Field(min_length=1, max_length=120)

    @field_validator("tabel")
    @classmethod
    def known_table(cls, v):
        if v not in TABEL:
            raise ValueError("Tabel tidak dikenal")
        return v


class Lembar(BaseModel):
    sel: dict[str, str] = Field(default_factory=dict)
    baris: list[Baris] = Field(default_factory=list, max_length=MAX_BARIS)
    hapus: list[str] = Field(default_factory=list, max_length=MAX_BARIS)

    @field_validator("sel")
    @classmethod
    def valid_cells(cls, cells):
        if len(cells) > MAX_SEL:
            raise ValueError("Terlalu banyak sel")
        for key, value in cells.items():
            if not KUNCI_SEL.match(key):
                raise ValueError(f"Kunci sel tidak valid: {key[:40]}")
            try:
                amount = Decimal(value)
            except InvalidOperation:
                raise ValueError(f"Angka tidak valid: {value[:20]}")
            if not amount.is_finite() or abs(amount) > Decimal("999999999999999"):
                raise ValueError("Angka di luar batas")
        return cells


class Simpan(BaseModel):
    version: int = Field(ge=0)
    lembar: Lembar


async def muat(session: AsyncSession, periode: str):
    row = await session.get(LaporanKeuanganManual, periode)
    if row is None:
        # A month not saved yet takes the rows of the latest earlier month (names, sub-rows, categories, order, hidden
        # rows) without its figures, so the user does not rebuild or reorder the sheet every month.
        lalu = (
            await session.execute(
                select(LaporanKeuanganManual)
                .where(LaporanKeuanganManual.periode < periode)
                .order_by(LaporanKeuanganManual.periode.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if lalu is None:
            return {"version": 0, "lembar": Lembar().model_dump()}
        data = json.loads(lalu.data_json)
        return {"version": 0, "lembar": {"sel": {}, "baris": data.get("baris", []), "hapus": data.get("hapus", [])}}
    return {"version": row.version, "lembar": json.loads(row.data_json)}


async def simpan(session: AsyncSession, periode: str, payload: Simpan, actor):
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy import text as sql_text
        await session.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"manual:{periode}"})
    row = await session.scalar(select(LaporanKeuanganManual).where(LaporanKeuanganManual.periode == periode).with_for_update().execution_options(populate_existing=True))
    current = row.version if row else 0
    if payload.version != current:
        raise HTTPException(409, "Lembar diubah di tempat lain; muat ulang sebelum menyimpan")
    if row is not None:
        session.add(LaporanManualAudit(periode=periode, version=current, data_json=row.data_json, actor_id=actor.id))
    text = payload.lembar.model_dump_json()
    if row is None:
        session.add(LaporanKeuanganManual(periode=periode, data_json=text, version=1, updated_by=actor.id))
    else:
        row.data_json, row.version, row.updated_by = text, current + 1, actor.id
    await session.commit()
    return {"version": current + 1, "lembar": payload.lembar.model_dump()}


KAS_PARENTS = {"k_toko": "Saldo Toko [BUDI]", "k_prod": "Saldo Toko [RISKA]", "k_cat": "Saldo Toko [SAEPUL]"}


def meta_key(prefix, key):
    safe = re.sub(r"[^\w-]", "_", key)
    return f"c:{prefix}-{safe}"


class KasRow(BaseModel):
    id: str = Field(pattern=r"^(k_toko|k_prod|k_cat|c:[\w-]{1,40})$")
    label: str = Field(min_length=1, max_length=120)
    parent_id: str | None = None
    urut: int = Field(ge=0, le=10000)
    aktif: bool = True


class KasStructure(BaseModel):
    version: int = Field(ge=0)
    rows: list[KasRow] = Field(max_length=150)


def kas_rows(data):
    rows = data["baris"]
    categories = {r["id"]: r["label"] for r in rows if r["grup"] == "kategori"}
    parents = dict(KAS_PARENTS)
    for r in rows:
        if r["tabel"] == "arus_rinci" and r["grup"] == "Kas masuk" and categories.get(meta_key("kt", r["id"])) == "saldo_toko":
            parents[r["id"]] = r["label"]
    by_id = {r["id"]: r for r in rows}
    result = []
    for key, label in parents.items():
        result.append(dict(id=key, label=by_id.get(meta_key("nm", key), {}).get("label", label), parent_id=None,
                           urut=int(by_id.get(meta_key("ur", key), {}).get("label", len(result))),
                           aktif=meta_key("archive", key) not in by_id))
        for r in rows:
            if r["tabel"] == "arus_rinci" and r["grup"] == f"anak:{key}":
                result.append(dict(id=r["id"], label=r["label"], parent_id=key,
                                   urut=int(by_id.get(meta_key("ur", r["id"]), {}).get("label", len(result))),
                                   aktif=meta_key("archive", r["id"]) not in by_id))
    return sorted(result, key=lambda r: r["urut"])


async def struktur(session, periode):
    saved = await muat(session, periode)
    return {"version": saved["version"], "rows": kas_rows(saved["lembar"])}


async def simpan_struktur(session, periode, payload, actor):
    # Serialize even the first insert of an unsaved month on PostgreSQL.
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"manual:{periode}"})
    saved = await muat(session, periode)
    if saved["version"] != payload.version:
        raise HTTPException(409, "Struktur berubah; muat ulang")
    current_month = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%Y-%m")
    if periode < current_month:
        raise HTTPException(422, "Struktur periode historis dipertahankan. Pilih bulan berjalan atau berikutnya")
    old = {r["id"]: r for r in kas_rows(saved["lembar"])}
    new = {r.id: r for r in payload.rows}
    occupied = {r["id"] for r in saved["lembar"]["baris"]}
    if any(k in occupied and k not in old for k in new):
        raise HTTPException(422, "ID sudah dipakai baris lain")
    if len(new) != len(payload.rows) or not set(old).issubset(new):
        raise HTTPException(422, "ID duplikat atau baris dihapus; gunakan arsip")
    for r in payload.rows:
        if r.id.startswith("c:") and (r.id.startswith(("c:nm-", "c:kt-", "c:ur-", "c:archive-"))):
            raise HTTPException(422, "ID metadata tidak dapat digunakan")
        if r.parent_id is not None and (r.parent_id not in new or new[r.parent_id].parent_id is not None or r.id == r.parent_id):
            raise HTTPException(422, "Induk tidak valid; hanya dua tingkat diperbolehkan")
        if r.id in KAS_PARENTS and r.parent_id is not None:
            raise HTTPException(422, "Baris induk bawaan tidak dapat dipindahkan")
        if r.id in old and ((old[r.id]["parent_id"] is None) != (r.parent_id is None)):
            raise HTTPException(422, "Jenis baris permanen tidak dapat diubah")
        if r.id in old and (r.parent_id != old[r.id]["parent_id"] or r.aktif != old[r.id]["aktif"]):
            # A month is the immutable reporting boundary for a structural move/archive.
            affected = {r.id} | {x["id"] for x in old.values() if x["parent_id"] == r.id}
            if any(k.split("|")[0] in affected or k.split("|")[0] in {f"rb:{x}" for x in affected} for k in saved["lembar"]["sel"]):
                raise HTTPException(422, "Baris memiliki pencatatan bulan ini. Pindah/arsipkan pada bulan berikutnya agar histori tetap utuh")
    data = saved["lembar"]
    # Preserve an existing standalone manual balance when its first breakdown is added.
    # The source figure stays intact; an explicit legacy component replaces it in the sum.
    for key in old:
        if old[key]["parent_id"] is not None or any(r["parent_id"] == key for r in old.values()):
            continue
        if not any(r.parent_id == key for r in payload.rows):
            continue
        prefix = f"rb:{key}" if key in KAS_PARENTS else key
        amounts = {k.split("|", 1)[1]: v for k, v in data["sel"].items() if k.startswith(prefix + "|")}
        if key in KAS_PARENTS and not amounts:
            from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import LaporanKeuanganTutup
            from sqlalchemy import cast, String
            closed = await session.scalar(select(LaporanKeuanganTutup).where(cast(LaporanKeuanganTutup.tanggal, String).like(periode + "%")).limit(1))
            if closed:
                raise HTTPException(422, "Induk memiliki saldo dari tutup buku. Pertahankan rincian yang ada atau gunakan induk baru")
        if amounts:
            legacy_id = meta_key("legacy", key)
            if legacy_id in new or legacy_id in occupied:
                raise HTTPException(422, "Komponen saldo sebelumnya sudah ada")
            payload.rows.append(KasRow(id=legacy_id, label="Saldo sebelumnya", parent_id=key, urut=0))
            new[legacy_id] = payload.rows[-1]
            data = {**data, "sel": {**data["sel"], **{f"{legacy_id}|{d}": v for d, v in amounts.items()}}}
    # Store permanent row IDs in the existing monthly structure.
    managed = set(new) | {meta_key(p, k) for k in new for p in ("nm", "ur", "archive")}
    rows = [r for r in data["baris"] if r["id"] not in managed]
    for r in sorted(payload.rows, key=lambda x: x.urut):
        def add(key, group, label):
            rows.append(dict(id=key, tabel="arus_rinci", grup=group, label=label))
        if r.id in KAS_PARENTS:
            add(meta_key("nm", r.id), "nama", r.label)
        else:
            add(r.id, f"anak:{r.parent_id}" if r.parent_id else "Kas masuk", r.label)
        if r.parent_id is None and r.id not in KAS_PARENTS:
            rows = [x for x in rows if x["id"] != meta_key("kt", r.id)]
            add(meta_key("kt", r.id), "kategori", "saldo_toko")
        add(meta_key("ur", r.id), "urutan", str(r.urut))
        if not r.aktif:
            add(meta_key("archive", r.id), "arsip", "arsip")
    data = {**data, "baris": rows}
    return await simpan(session, periode, Simpan(version=payload.version, lembar=Lembar(**data)), actor)
