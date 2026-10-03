# ruff: noqa: F811
"""Fase 2.14/2.15: kolom tambahan per entitas (AB-DM-1..13)."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from tenants.bumi_lestari.modules.bumi_lestari.application import kolom_services as ks
from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as osvc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import TransaksiOut
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_kolom import DefinisiKolomIn, DefinisiKolomPatch, LabelIntiIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import OrderIn, OrderPatch, ProdukIn
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.kolom_konteks import KUNCI_STAF, PERAN
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlTransaksi
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_kolom import BlDefinisiKolom
from tests.test_bumi_lestari_fase1 import _kode, _trx, c  # noqa: F401


async def _def(c, label, tipe="teks", entitas="order", **kw):
    return await ks.create_definisi(c.s, c.admin, DefinisiKolomIn(entitas=entitas, label=label, tipe=tipe, **kw))


_N = iter(range(1, 10_000))


async def _order(c, kt=None):
    return await osvc.create_order(
        c.s, OrderIn(no_order=f"SHP-{next(_N)}", saluran_id=c.shopee.id, produk_id=c.partisi.id, kolom_tambahan=kt),
    )


@pytest.mark.asyncio
async def test_nilai_divalidasi_per_jenis_data(c):
    await _def(c, "Kurir", "pilihan", pilihan=["JNE", "J&T"])
    await _def(c, "Ongkir", "mata_uang")
    await _def(c, "Target", "tanggal")
    await _def(c, "Mendesak", "ya_tidak")
    await _def(c, "Berat", "angka", min="0", maks="1000")
    o = await _order(c, {"kurir": "JNE", "ongkir": "Rp 150.000", "target": "04/10/2026", "mendesak": "ya", "berat": "1,5"})
    assert o.kolom_tambahan == {"kurir": "JNE", "ongkir": "150000.00", "target": "2026-10-04", "mendesak": True, "berat": "1.5"}
    for salah in ({"kurir": "POS"}, {"ongkir": "-5000"}, {"target": "31/02/2026"}, {"mendesak": "mungkin"},
                  {"berat": "2000"}, {"tidak_ada": "x"}):
        assert await _kode(c, _order, c, salah) == 422
    assert await _kode(c, _def, c, "Kosong", "pilihan") == 400  # pilihan minimal satu


@pytest.mark.asyncio
async def test_wajib_hanya_untuk_data_baru_dan_saat_diubah(c):
    lama = await _order(c)
    await _def(c, "No resi", wajib=True)
    assert await _kode(c, _order, c, {}) == 422
    await osvc.update_order(c.s, lama.id, OrderPatch(qty=2))  # AB-DM-5: data lama tidak dipaksa
    assert await _kode(c, osvc.update_order, c.s, lama.id, OrderPatch(kolom_tambahan={"no_resi": ""})) == 422
    o = await osvc.update_order(c.s, lama.id, OrderPatch(kolom_tambahan={"no_resi": "  JX123 "}))
    assert o.kolom_tambahan == {"no_resi": "JX123"}


@pytest.mark.asyncio
async def test_aturan_definisi_kunci_jenis_pilihan_hapus_maks(c):
    d = await _def(c, "No order")  # bentrok kolom inti -> kunci diberi akhiran (AB-DM-1)
    assert d.kunci == "no_order_2"
    k = await _def(c, "Kurir", "pilihan", pilihan=["JNE", "J&T", "SiCepat"])
    o = await _order(c, {"kurir": "J&T"})
    assert await _kode(c, ks.update_definisi, c.s, k.id, DefinisiKolomPatch(tipe="teks")) == 400  # AB-DM-2
    k2 = await ks.update_definisi(c.s, k.id, DefinisiKolomPatch(pilihan=["JNE"]))  # J&T dipakai -> arsip, SiCepat hilang
    assert [(p.nilai, p.arsip) for p in k2.pilihan] == [("JNE", False), ("J&T", True)]
    await osvc.update_order(c.s, o.id, OrderPatch(kolom_tambahan={"kurir": "J&T"}))  # nilai lama tetap sah (AB-DM-3)
    assert await _kode(c, _order, c, {"kurir": "J&T"}) == 422
    assert await _kode(c, ks.hapus, c.s, k.id) == 409  # AB-DM-4
    assert (await ks.nonaktifkan(c.s, k.id)).aktif is False
    await ks.hapus(c.s, d.id)
    for i in range(20):
        await _def(c, f"Kolom {i}")
    assert await _kode(c, _def, c, "Kolom 21") == 400  # AB-DM-10
    assert await _kode(c, ks.update_definisi, c.s, k.id, DefinisiKolomPatch(aktif=True)) == 400
    assert await _kode(c, _def, c, "Staf", entitas="order", tampil_staf=True) == 400


@pytest.mark.asyncio
async def test_staf_hanya_kolom_transaksi_untuk_staf_dan_owner_tidak_melihat(c):
    await _def(c, "No nota", entitas="transaksi", tampil_staf=True)
    await _def(c, "Kode internal", entitas="transaksi")
    with pytest.raises(Exception) as e:
        await _trx(c, c.staf, "KAS_KECIL", "Transport", "keluar", "20000", kolom_tambahan={"kode_internal": "X"}, talangan_oleh="Sari")
    assert getattr(e.value, "status_code", None) == 403
    t = await _trx(c, c.staf, "KAS_KECIL", "Transport", "keluar", "20000", kolom_tambahan={"no_nota": "N-1"}, talangan_oleh="Sari")
    # pecahan talangan ikut membawa nilai kolom tambahan
    semua = (await c.s.execute(select(BlTransaksi).where(BlTransaksi.jumlah == Decimal("20000")))).scalars().all()
    assert all(x.kolom_tambahan == {"no_nota": "N-1"} for x in semua)
    t.kolom_tambahan = {"no_nota": "N-1", "kode_internal": "X"}
    out = TransaksiOut.model_validate(t)
    tok = PERAN.set("owner")
    assert out.model_dump()["kolom_tambahan"] == {}
    PERAN.reset(tok)
    tok, tok2 = PERAN.set("staff"), KUNCI_STAF.set(frozenset({"no_nota"}))
    assert out.model_dump()["kolom_tambahan"] == {"no_nota": "N-1"}
    PERAN.reset(tok)
    KUNCI_STAF.reset(tok2)
    assert out.model_dump()["kolom_tambahan"]["kode_internal"] == "X"
    assert [d.kunci for d in await ks.list_definisi(c.s, c.staf, "transaksi")] == ["no_nota"]
    assert all(d.lapisan == "inti" for d in await ks.list_definisi(c.s, c.owner, "transaksi"))


@pytest.mark.asyncio
async def test_label_inti_bisa_diganti_dan_dikembalikan(c):
    d = await ks.ubah_label_inti(c.s, c.admin, "order", "tgl_diambil", LabelIntiIn(label="Selesai di tukang"))
    assert d.label == "Selesai di tukang" and d.label_bawaan == "Tanggal selesai di tukang"
    daftar = {x.kunci: x for x in await ks.list_definisi(c.s, c.admin, "order")}
    assert daftar["tgl_diambil"].label == "Selesai di tukang" and daftar["tgl_diambil"].lapisan == "inti"
    await ks.reset_label_inti(c.s, "order", "tgl_diambil")
    daftar = {x.kunci: x for x in await ks.list_definisi(c.s, c.admin, "order")}
    assert daftar["tgl_diambil"].label == "Tanggal selesai di tukang"
    assert await _kode(c, ks.ubah_label_inti, c.s, c.admin, "order", "tidak_ada", LabelIntiIn(label="x")) == 404


@pytest.mark.asyncio
async def test_seed_kolom_bawaan_sekali_dan_produk_terima_nilai(c):
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.seeder import DEFAULT_KOLOM_TAMBAHAN, _seed_kolom_tambahan

    await _seed_kolom_tambahan(c.s)
    await _seed_kolom_tambahan(c.s)
    n = len((await c.s.execute(select(BlDefinisiKolom))).scalars().all())
    assert n == len(DEFAULT_KOLOM_TAMBAHAN)
    p = await osvc.create_produk(c.s, ProdukIn(sku="RAK1", nama="Rak", harga_jual=Decimal("1"), kolom_tambahan={"bahan_jenis_kayu": "Jati"}))
    assert p.kolom_tambahan == {"bahan_jenis_kayu": "Jati"}



async def _get(app, path: str, query: str) -> bytes:
    """Panggil aplikasi ASGI langsung (tanpa httpx)."""
    pesan, badan = [{"type": "http.request", "body": b"", "more_body": False}], []

    async def receive():
        return pesan.pop(0) if pesan else {"type": "http.disconnect"}

    async def send(m):
        if m["type"] == "http.response.body":
            badan.append(m.get("body", b""))

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": [], "server": ("t", 80),
             "client": ("t", 1), "root_path": ""}
    await app(scope, receive, send)
    return b"".join(badan)


@pytest.mark.asyncio
async def test_penyaringan_berjalan_lewat_dependency_fastapi():
    """Dependency async mengisi PERAN; serializer respons membacanya (pola _pasang_konteks_kolom di auth)."""
    import json

    from fastapi import Depends, FastAPI
    from pydantic import BaseModel

    from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_kolom import KolomTambahanOut

    class Out(BaseModel):
        kolom_tambahan: KolomTambahanOut = {}

    async def peran(role: str) -> str:
        PERAN.set(role)
        return role

    app = FastAPI()

    @app.get("/x", response_model=Out)
    async def x(_: str = Depends(peran)):
        return {"kolom_tambahan": {"no_resi": "JX1"}}

    assert json.loads(await _get(app, "/x", "role=owner")) == {"kolom_tambahan": {}}
    assert json.loads(await _get(app, "/x", "role=admin")) == {"kolom_tambahan": {"no_resi": "JX1"}}
