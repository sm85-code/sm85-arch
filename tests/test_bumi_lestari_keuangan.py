"""bumi_lestari Tahap 1: akun kas, transaksi, transfer, kas kecil (imprest), peran staf."""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.bumi_lestari.modules.bumi_lestari.application import services
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import (
    AkunKasIn,
    ChangePasswordIn,
    LoginIn,
    TransaksiIn,
    TransferIn,
    UserCreateIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAkunKas, BlKategori
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.seeder import DEFAULT_AKUN, DEFAULT_KATEGORI


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BumiLestariBase.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        for kode, nama, jenis, plafon in DEFAULT_AKUN:
            s.add(BlAkunKas(kode=kode, nama=nama, jenis=jenis, plafon=plafon))
        for nama, jenis in DEFAULT_KATEGORI:
            s.add(BlKategori(nama=nama, jenis=jenis))
        await s.flush()
        yield s
    await engine.dispose()


async def _akun(session, kode):
    return (await session.execute(select(BlAkunKas).where(BlAkunKas.kode == kode))).scalar_one()


async def _kat(session, nama):
    return (await session.execute(select(BlKategori).where(BlKategori.nama == nama))).scalar_one()


async def _user(session, role, email=None):
    return await services.create_user(
        session, UserCreateIn(nama=role, email=email or f"{role}@test.com", password="rahasia123", role=role)
    )


async def _isi_kas_utama(session, owner, jumlah):
    ku = await _akun(session, "KAS_UTAMA")
    await services.create_transaksi(
        session, owner,
        TransaksiIn(akun_id=ku.id, kategori_id=(await _kat(session, "Penjualan")).id, jenis="masuk", jumlah=jumlah),
    )
    return ku


@pytest.mark.asyncio
async def test_login_and_change_password(session):
    await _user(session, "owner")
    user = await services.authenticate_user(session, LoginIn(email="owner@test.com", password="rahasia123"))
    assert user.must_change_password is True
    await services.change_password(session, user, ChangePasswordIn(current_password="rahasia123", new_password="barubaru1"))
    assert user.must_change_password is False
    with pytest.raises(HTTPException) as exc:
        await services.authenticate_user(session, LoginIn(email="owner@test.com", password="salah"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_saldo_ignores_cancelled_and_includes_transfers(session):
    owner = await _user(session, "owner")
    ku = await _isi_kas_utama(session, owner, Decimal("1000000"))
    trx = await services.create_transaksi(
        session, owner,
        TransaksiIn(akun_id=ku.id, kategori_id=(await _kat(session, "Packing")).id, jenis="keluar", jumlah=Decimal("250000")),
    )
    assert await services.saldo_akun(session, ku) == Decimal("750000")
    await services.batalkan_transaksi(session, trx.id, "salah input")
    assert await services.saldo_akun(session, ku) == Decimal("1000000")
    with pytest.raises(HTTPException) as exc:
        await services.batalkan_transaksi(session, trx.id, "lagi")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_transaksi_jenis_must_match_kategori(session):
    owner = await _user(session, "owner")
    ku = await _akun(session, "KAS_UTAMA")
    with pytest.raises(HTTPException) as exc:
        await services.create_transaksi(
            session, owner,
            TransaksiIn(akun_id=ku.id, kategori_id=(await _kat(session, "Penjualan")).id, jenis="keluar", jumlah=Decimal("1")),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_transfer_needs_enough_balance(session):
    owner = await _user(session, "owner")
    ku, shopee = await _akun(session, "KAS_UTAMA"), await _akun(session, "SALDO_SHOPEE")
    with pytest.raises(HTTPException) as exc:
        await services.create_transfer(
            session, owner, TransferIn(dari_akun_id=shopee.id, ke_akun_id=ku.id, jumlah=Decimal("1000"))
        )
    assert exc.value.status_code == 400
    await services.create_transaksi(
        session, owner,
        TransaksiIn(akun_id=shopee.id, kategori_id=(await _kat(session, "Penjualan")).id, jenis="masuk", jumlah=Decimal("500000")),
    )
    await services.create_transfer(session, owner, TransferIn(dari_akun_id=shopee.id, ke_akun_id=ku.id, jumlah=Decimal("500000")))
    assert await services.saldo_akun(session, shopee) == 0
    assert await services.saldo_akun(session, ku) == Decimal("500000")


@pytest.mark.asyncio
async def test_kas_kecil_imprest_refill_to_plafon(session):
    owner, staf = await _user(session, "owner"), await _user(session, "staff")
    ku = await _isi_kas_utama(session, owner, Decimal("5000000"))
    kk = await services.get_kas_kecil(session)
    assert (await services.hitung_pengisian_kas_kecil(session))["perlu_diisi"] == Decimal("3000000")

    await services.catat_pengisian_kas_kecil(session, owner)  # awal: isi sampai plafon
    assert await services.saldo_akun(session, kk) == Decimal("3000000")

    await services.create_transaksi(
        session, staf,
        TransaksiIn(akun_id=kk.id, kategori_id=(await _kat(session, "Operasional")).id, jenis="keluar", jumlah=Decimal("400000")),
    )
    info = await services.hitung_pengisian_kas_kecil(session)
    assert (info["saldo"], info["perlu_diisi"], info["cukup"]) == (Decimal("2600000"), Decimal("400000"), True)

    transfer = await services.catat_pengisian_kas_kecil(session, owner)
    assert transfer.jumlah == Decimal("400000") and transfer.jenis == "pengisian_kas_kecil"
    assert await services.saldo_akun(session, kk) == Decimal("3000000")
    assert await services.saldo_akun(session, ku) == Decimal("1600000")

    with pytest.raises(HTTPException) as exc:  # sudah penuh
        await services.catat_pengisian_kas_kecil(session, owner)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_pengisian_rejected_when_kas_utama_short(session):
    owner = await _user(session, "owner")
    await _isi_kas_utama(session, owner, Decimal("100000"))
    assert (await services.hitung_pengisian_kas_kecil(session))["cukup"] is False
    with pytest.raises(HTTPException) as exc:
        await services.catat_pengisian_kas_kecil(session, owner)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_staf_hanya_pengeluaran_kas_kecil(session):
    owner, staf = await _user(session, "owner"), await _user(session, "staff")
    await _isi_kas_utama(session, owner, Decimal("5000000"))
    await services.catat_pengisian_kas_kecil(session, owner)
    kk, ku = await services.get_kas_kecil(session), await _akun(session, "KAS_UTAMA")
    ops = await _kat(session, "Operasional")

    for payload in (
        TransaksiIn(akun_id=ku.id, kategori_id=ops.id, jenis="keluar", jumlah=Decimal("1000")),
        TransaksiIn(akun_id=kk.id, kategori_id=(await _kat(session, "Penjualan")).id, jenis="masuk", jumlah=Decimal("1000")),
    ):
        with pytest.raises(HTTPException) as exc:
            await services.create_transaksi(session, staf, payload)
        assert exc.value.status_code == 403

    # kas kecil tidak boleh minus
    with pytest.raises(HTTPException) as exc:
        await services.create_transaksi(
            session, staf, TransaksiIn(akun_id=kk.id, kategori_id=ops.id, jenis="keluar", jumlah=Decimal("3000001"))
        )
    assert exc.value.status_code == 400

    # staf hanya melihat akun & transaksi kas kecil
    assert [a.kode for a, _ in await services.list_akun(session, staf)] == ["KAS_KECIL"]
    assert {t.akun_id for t in await services.list_transaksi(session, staf, akun_id=ku.id)} <= {kk.id}


@pytest.mark.asyncio
async def test_kas_kecil_expense_shows_in_general_ledger(session):
    """Laporan umum = semua transaksi semua akun, jadi pengeluaran kas kecil ikut tercatat."""
    owner, staf = await _user(session, "owner"), await _user(session, "staff")
    await _isi_kas_utama(session, owner, Decimal("5000000"))
    await services.catat_pengisian_kas_kecil(session, owner)
    kk = await services.get_kas_kecil(session)
    await services.create_transaksi(
        session, staf,
        TransaksiIn(akun_id=kk.id, kategori_id=(await _kat(session, "Transport")).id, jenis="keluar", jumlah=Decimal("50000")),
    )
    semua = await services.list_transaksi(session, owner)
    assert any(t.akun_id == kk.id and t.jenis == "keluar" for t in semua)


@pytest.mark.asyncio
async def test_create_akun_rules(session):
    with pytest.raises(HTTPException):
        await services.create_akun(session, AkunKasIn(kode="X", nama="X", jenis="kas_kecil"))
    with pytest.raises(HTTPException):
        await services.create_akun(session, AkunKasIn(kode="KAS_UTAMA", nama="Dup"))
    akun = await services.create_akun(session, AkunKasIn(kode="bca", nama="BCA", jenis="bank", saldo_awal=Decimal("10")))
    assert akun.kode == "BCA"
