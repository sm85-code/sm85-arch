"""Structured buyer address: province > city > district > village + zip, with the Kemendagri village code."""
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import AlamatIn, AlamatPatch, PengirimanIn, RegisterRequest
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import PesananStore


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def _alamat(**over):
    base = dict(
        nama_penerima="Budi", telepon_penerima="081234567890", alamat_lengkap="Jl. Cipaganti No. 5",
        provinsi="Jawa Barat", kota="Kota Bandung", kecamatan="Sukasari", kelurahan="Sukarasa",
        kode_pos="40152", kode_wilayah="32.73.01.1001",
    )
    base.update(over)
    return AlamatIn(**base)


@pytest.mark.asyncio
async def test_address_keeps_every_level_and_the_village_code(session):
    user = await services.register(session, RegisterRequest(nama="Budi", email="b@x.com", password="rahasia123"))
    out = services.alamat_out(await services.create_alamat(session, user.id, _alamat()))
    assert (out["provinsi"], out["kota"], out["kecamatan"], out["kelurahan"], out["kode_pos"], out["kode_wilayah"]) == (
        "Jawa Barat", "Kota Bandung", "Sukasari", "Sukarasa", "40152", "32.73.01.1001",
    )


@pytest.mark.parametrize("kode", ["36.01.01.2001", "31.71.01.1001", "34.04.01.2001", "35.78.01.1001", "51.71.01.1001", "18.71.01.1001"])
def test_every_served_province_is_accepted(kode):
    assert _alamat(kode_wilayah=kode).kode_wilayah == kode


@pytest.mark.parametrize("kode", ["11.01.01.2001", "64.71.01.1001", "91.01.01.2001"])
def test_provinces_outside_java_bali_lampung_are_refused(kode):
    with pytest.raises(ValidationError) as exc:
        _alamat(kode_wilayah=kode)
    assert "Jawa, Bali, dan Lampung" in str(exc.value)


@pytest.mark.parametrize("kode", ["32.73", "32.73.01", "abc", "32-73-01-1001", "32.73.01.10011"])
def test_malformed_village_codes_are_refused(kode):
    with pytest.raises(ValidationError):
        _alamat(kode_wilayah=kode)


def test_old_clients_without_a_village_code_still_work_and_patch_is_validated():
    assert _alamat(kode_wilayah="", kecamatan="", kelurahan="").kode_wilayah == ""
    assert AlamatPatch(kode_wilayah="32.73.01.1001").kode_wilayah == "32.73.01.1001"
    with pytest.raises(ValidationError):
        AlamatPatch(kode_wilayah="11.01.01.2001")


@pytest.mark.asyncio
async def test_shipment_snapshots_the_structured_address(session):
    user = await services.register(session, RegisterRequest(nama="Budi", email="b@x.com", password="rahasia123"))
    pesanan = PesananStore(user_id=user.id, status="menunggu_pembayaran", total=Decimal("1000"))
    session.add(pesanan)
    await session.flush()
    payload = PengirimanIn(
        kurir="jne", layanan="reg", nama_penerima="Budi", telepon_penerima="081234567890",
        alamat_tujuan="Jl. Cipaganti No. 5", provinsi_tujuan="Jawa Barat", kota_tujuan="Kota Bandung",
        kecamatan_tujuan="Sukasari", kelurahan_tujuan="Sukarasa", kode_pos_tujuan="40152", kode_wilayah_tujuan="32.73.01.1001",
    )
    out = services.pengiriman_out(await services.buat_pengiriman_lokal(session, pesanan.id, payload))
    assert (out["provinsi_tujuan"], out["kota_tujuan"], out["kecamatan_tujuan"], out["kelurahan_tujuan"]) == (
        "Jawa Barat", "Kota Bandung", "Sukasari", "Sukarasa",
    )
    assert (out["kode_pos_tujuan"], out["kode_wilayah_tujuan"]) == ("40152", "32.73.01.1001")


def test_shipment_refuses_a_destination_outside_the_service_area():
    with pytest.raises(ValidationError):
        PengirimanIn(
            kurir="jne", layanan="reg", nama_penerima="Budi", telepon_penerima="081234567890",
            alamat_tujuan="x", kode_wilayah_tujuan="11.01.01.2001",
        )
