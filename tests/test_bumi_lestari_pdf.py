"""bumi_lestari: PDF invoice/PO dan tombol kirim ke WhatsApp (tautan publik bertoken)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.bumi_lestari.modules.bumi_lestari.application import dokumen_services as dok
from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as osvc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_dokumen import BagikanIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    HargaGrosirIn,
    OrderIn,
    OrderStatusIn,
    PelangganIn,
    PemasokIn,
    ProdukIn,
    SaluranIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_pembayaran  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

SELASA = date(2026, 9, 29)


@pytest_asyncio.fixture
async def ctx():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BumiLestariBase.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        class C:
            pass

        c = C()
        c.session = s
        c.user = BlUser(id="u1", nama="A", email="a@t.com", password_hash="x", role="admin")
        s.add(c.user)
        c.tukang = await osvc.create_pemasok(
            s, PemasokIn(nama="AHMAD NUR ALIM", jenis="tukang_kayu", kode="005", nama_bank="Bank Mandiri", no_rekening="1770022968629", no_wa="0812-3456-7890")
        )
        c.rina = await osvc.create_pelanggan(s, PelangganIn(nama="MANDALAWANGI", kode="002", alamat="Desa Wonoharjo, Kec. Pangandaran", no_wa="+62 857 1111 2222"))
        produk = await osvc.create_produk(s, ProdukIn(sku="P1", nama="Partisi Rak Tengah [2 rak]", ukuran="150x20x200", harga_jual=Decimal("1")))
        await osvc.set_harga_grosir(
            s, HargaGrosirIn(produk_id=produk.id, pelanggan_id=c.rina.id, harga=Decimal("725000"), harga_cat_jasa=Decimal("220000"))
        )
        res = await osvc.create_saluran(s, SaluranIn(nama="Reseller", jenis="reseller"))
        o = await osvc.create_order(
            s, OrderIn(saluran_id=res.id, pelanggan_id=c.rina.id, produk_id=produk.id, pemasok_id=c.tukang.id, no_order="260922PJ9B35EN", biaya_pokok=Decimal("500000"))
        )
        for st in ("dikerjakan", "diambil"):
            await osvc.ubah_status_order(s, o.id, OrderStatusIn(status=st, tanggal=date(2026, 9, 26)))
        yield c
    await engine.dispose()


def test_normalisasi_wa():
    assert dok.normalisasi_wa("0812-3456-7890") == "6281234567890"
    assert dok.normalisasi_wa("+62 857 1111 2222") == "6285711112222"
    assert dok.normalisasi_wa("6281234567890") == "6281234567890"
    assert dok.normalisasi_wa("") is None


@pytest.mark.asyncio
async def test_pdf_rendered_for_invoice_and_po(ctx):
    inv = await dok.invoice_untuk(ctx.session, ctx.rina.id, SELASA)
    po = await dok.po_untuk(ctx.session, ctx.tukang.id, SELASA)
    from tenants.bumi_lestari.modules.bumi_lestari.application.pdf_dokumen import render_invoice, render_po

    for isi in (render_invoice(inv), render_po(po)):
        assert isi.startswith(b"%PDF") and len(isi) > 2000
    with pytest.raises(HTTPException) as exc:
        await dok.invoice_untuk(ctx.session, "tidak-ada", SELASA)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_share_to_whatsapp_and_public_download(ctx):
    out = await dok.bagikan_dokumen(
        ctx.session, ctx.user, BagikanIn(jenis="invoice", pelanggan_id=ctx.rina.id, tanggal=SELASA), "https://api.example.com/"
    )
    assert out.nomor == "INV/MG.4-002/IX/2026" and out.no_wa == "6285711112222"
    assert out.url.startswith("https://api.example.com/api/bumi-lestari/dokumen-publik/")
    wa = urlparse(out.wa_link)
    assert (wa.netloc, wa.path) == ("wa.me", "/6285711112222")
    teks = parse_qs(wa.query)["text"][0]
    assert "INV/MG.4-002/IX/2026" in teks and out.url in teks and "Rp955.000" in teks  # 725rb + 220rb + 10rb
    isi, nama = await dok.pdf_dari_token(ctx.session, out.url.rsplit("/", 1)[1])
    assert isi.startswith(b"%PDF") and nama == "INV-MG.4-002-IX-2026.pdf"

    po = await dok.bagikan_dokumen(ctx.session, ctx.user, BagikanIn(jenis="po", pemasok_id=ctx.tukang.id, tanggal=SELASA), "https://x")
    assert po.nomor == "PO/MG.4-005/IX/2026" and urlparse(po.wa_link).path == "/6281234567890"


@pytest.mark.asyncio
async def test_share_validation_unknown_token_and_expiry(ctx):
    with pytest.raises(HTTPException) as exc:
        await dok.bagikan_dokumen(ctx.session, ctx.user, BagikanIn(jenis="invoice"), "https://x")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await dok.bagikan_dokumen(ctx.session, ctx.user, BagikanIn(jenis="surat"), "https://x")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await dok.pdf_dari_token(ctx.session, "token-ngawur")
    assert exc.value.status_code == 404

    out = await dok.bagikan_dokumen(ctx.session, ctx.user, BagikanIn(jenis="invoice", pelanggan_id=ctx.rina.id, tanggal=SELASA), "https://x")
    token = out.url.rsplit("/", 1)[1]
    row = (await ctx.session.execute(select(models_pembayaran.BlDokumenBagikan))).scalar_one()
    row.kedaluwarsa = datetime.now(timezone.utc) - timedelta(days=1)
    await ctx.session.flush()
    with pytest.raises(HTTPException) as exc:
        await dok.pdf_dari_token(ctx.session, token)
    assert exc.value.status_code == 410
