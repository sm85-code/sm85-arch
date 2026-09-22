"""Exercises pengiriman (shipping) status transitions, sales reporting
queries, and the payment/shipping adapters' fail-loud behavior when not
configured -- against a real (SQLite, in-memory) async session.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.toko.modules.toko.application import services
from tenants.toko.modules.toko.application.schemas import PengirimanIn
from tenants.toko.modules.toko.infrastructure.database import TokoBase
from tenants.toko.modules.toko.infrastructure.models import ProdukToko, UserToko


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _pesanan_baru(session, *, harga=Decimal("50000.00"), qty=2) -> str:
    user = UserToko(nama="Pembeli", email=f"p{id(object())}@test.com", password_hash="x", role="pembeli")
    session.add(user)
    await session.flush()
    produk = ProdukToko(nama="Produk A", harga=harga, stok=100)
    session.add(produk)
    await session.flush()
    await services.tambah_ke_keranjang(session, user.id, produk.id, qty)
    pesanan = await services.checkout(session, user.id)
    return pesanan.id


@pytest.mark.asyncio
async def test_pengiriman_status_transitions_and_auto_completes_pesanan(session):
    pesanan_id = await _pesanan_baru(session)
    await services.ubah_status_pesanan(session, pesanan_id, "dibayar")
    await services.ubah_status_pesanan(session, pesanan_id, "diproses")

    pengiriman = await services.buat_pengiriman_lokal(
        session,
        pesanan_id,
        PengirimanIn(
            kurir="jne",
            layanan="reg",
            nama_penerima="Budi",
            telepon_penerima="0812xxxx",
            alamat_tujuan="Jl. Contoh No. 1",
        ),
    )
    assert pengiriman.status == "menunggu_pickup"

    await services.ubah_status_pengiriman(session, pesanan_id, "dikirim")
    pengiriman = await services.ubah_status_pengiriman(session, pesanan_id, "diterima", tracking_id="JNE123")
    assert pengiriman.status == "diterima"
    assert pengiriman.tracking_id == "JNE123"

    pesanan = await services.get_pesanan(session, pesanan_id)
    assert pesanan.status == "selesai"


@pytest.mark.asyncio
async def test_pengiriman_rejects_invalid_transition(session):
    pesanan_id = await _pesanan_baru(session)
    await services.buat_pengiriman_lokal(
        session,
        pesanan_id,
        PengirimanIn(kurir="jne", layanan="reg", nama_penerima="Budi", telepon_penerima="0812", alamat_tujuan="Jl. X"),
    )

    with pytest.raises(Exception) as exc_info:
        await services.ubah_status_pengiriman(session, pesanan_id, "diterima")
    assert "Tidak bisa ubah status pengiriman" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_pengiriman_rejects_duplicate_for_same_pesanan(session):
    pesanan_id = await _pesanan_baru(session)
    payload = PengirimanIn(kurir="jne", layanan="reg", nama_penerima="Budi", telepon_penerima="0812", alamat_tujuan="Jl. X")
    await services.buat_pengiriman_lokal(session, pesanan_id, payload)

    with pytest.raises(Exception) as exc_info:
        await services.buat_pengiriman_lokal(session, pesanan_id, payload)
    assert "sudah punya data pengiriman" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_laporan_penjualan_sums_only_countable_statuses(session):
    today = date.today()
    pesanan_dibayar = await _pesanan_baru(session, harga=Decimal("10000.00"), qty=1)
    await services.ubah_status_pesanan(session, pesanan_dibayar, "dibayar")

    # Pesanan yang masih menunggu_pembayaran tidak boleh ikut terhitung.
    await _pesanan_baru(session, harga=Decimal("999999.00"), qty=1)

    laporan = await services.laporan_penjualan(session, today - timedelta(days=1), today + timedelta(days=1))

    assert laporan["grand_total"] == "10000.00"
    assert sum(h["jumlah_pesanan"] for h in laporan["harian"]) == 1


@pytest.mark.asyncio
async def test_laporan_produk_terlaris_ranks_by_qty(session):
    today = date.today()
    p1 = await _pesanan_baru(session, harga=Decimal("10000.00"), qty=5)
    await services.ubah_status_pesanan(session, p1, "dibayar")

    terlaris = await services.laporan_produk_terlaris(session, today - timedelta(days=1), today + timedelta(days=1))
    assert len(terlaris) == 1
    assert terlaris[0]["total_qty"] == 5
    assert terlaris[0]["total_omzet"] == "50000.00"


@pytest.mark.asyncio
async def test_ipaymu_adapter_fails_loud_when_not_configured(monkeypatch):
    import tenants.toko.modules.toko.infrastructure.payment_ipaymu as ipaymu

    monkeypatch.setattr(ipaymu, "IPAYMU_VA", None)
    monkeypatch.setattr(ipaymu, "IPAYMU_API_KEY", None)

    with pytest.raises(ipaymu.IpaymuNotConfigured):
        await ipaymu.create_payment(
            pesanan_id="x",
            total="1000",
            nama_pembeli="x",
            email_pembeli="x@x.com",
            notify_url="https://example.com",
            return_url="https://example.com",
        )


@pytest.mark.asyncio
async def test_biteship_adapter_fails_loud_when_not_configured(monkeypatch):
    import tenants.toko.modules.toko.infrastructure.shipping_biteship as biteship

    monkeypatch.setattr(biteship, "BITESHIP_API_KEY", None)

    with pytest.raises(biteship.BiteshipNotConfigured):
        await biteship.cek_ongkir(kode_pos_asal="12345", kode_pos_tujuan="54321", berat_gram=1000, nilai_barang="0")
