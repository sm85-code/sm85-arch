"""Product gallery: uploads append (max 7), deleting removes the R2 object only after\ncommit and never while something else still uses it."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.store.adapters.api.v1 import store_admin_router as admin_module
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.application import services
from tenants.store.modules.store.infrastructure.models import FotoProduk, ProdukStore


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def r2(monkeypatch):
    """Fake upload (returns produk/baru.jpg) and delete (records calls + commit state)."""
    calls = SimpleNamespace(deleted=[], commits_at_delete=[], commits=0, session=None)

    async def fake_upload(data, content_type):
        return "produk/baru.jpg"

    async def fake_delete(key):
        calls.deleted.append(key)
        calls.commits_at_delete.append(calls.commits)

    monkeypatch.setattr(admin_module, "upload_produk_photo", fake_upload)
    monkeypatch.setattr(admin_module, "delete_foto", fake_delete)
    return calls


async def _produk(session, foto_key):
    p = ProdukStore(nama="Beras", harga=1, stok=1, foto_key=foto_key)
    session.add(p)
    await session.commit()
    return p


def _count_commits(session, calls):
    real_commit = session.commit

    async def counting():
        calls.commits += 1
        await real_commit()

    session.commit = counting


async def _replace(session, produk_id):
    file = SimpleNamespace(read=_read, content_type="image/jpeg")
    return await admin_module.upload_foto_produk(produk_id, file=file, session=session, _user=SimpleNamespace())


async def _read():
    return b"\xff\xd8\xff" + b"0" * 10


@pytest.mark.asyncio
async def test_upload_appends_and_keeps_the_legacy_cover(session, r2):
    p = await _produk(session, "produk/lama.jpg")
    out = await _replace(session, p.id)
    assert len(out["foto"]) == 2
    assert out["foto_key"] == "produk/lama.jpg"  # first photo stays the cover
    assert r2.deleted == []


@pytest.mark.asyncio
async def test_first_photo_becomes_the_cover(session, r2):
    p = await _produk(session, None)
    out = await _replace(session, p.id)
    assert out["foto_key"] == "produk/baru.jpg"
    assert r2.deleted == []


@pytest.mark.asyncio
async def test_gallery_is_capped_at_seven_without_uploading(session, r2, monkeypatch):
    p = await _produk(session, None)
    for i in range(7):
        session.add(FotoProduk(produk_id=p.id, foto_key=f"produk/{i}.jpg", urutan=i))
    await session.commit()

    async def boom(*a, **k):
        raise AssertionError("must not upload when the gallery is full")

    monkeypatch.setattr(admin_module, "upload_produk_photo", boom)
    with pytest.raises(HTTPException) as exc:
        await _replace(session, p.id)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_deleting_a_photo_removes_the_object_after_commit_and_moves_the_cover(session, r2):
    p = await _produk(session, None)
    await _replace(session, p.id)
    out = await services.tambah_foto(session, p.id, "produk/dua.jpg")
    first, second = out["foto"] if isinstance(out, dict) else services.produk_out(out)["foto"]
    _count_commits(session, r2)
    hasil = await admin_module.hapus_foto_produk(p.id, first["id"], session=session, _user=SimpleNamespace())
    assert r2.deleted == ["produk/baru.jpg"]
    assert r2.commits_at_delete == [1]
    assert hasil["foto_key"] == "produk/dua.jpg"


@pytest.mark.asyncio
async def test_unknown_product_uploads_nothing(session, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not upload for a missing product")

    monkeypatch.setattr(admin_module, "upload_produk_photo", boom)
    with pytest.raises(HTTPException) as exc:
        await _replace(session, "tidak-ada")
    assert exc.value.status_code == 404


# --- deleting a whole product removes its photo too ---------------------------


async def _remove(session, produk_id):
    return await admin_module.remove_produk(produk_id, session=session, _user=SimpleNamespace())


@pytest.mark.asyncio
async def test_deleting_a_product_removes_its_photo_after_commit(session, r2):
    p = await _produk(session, "produk/foto.jpg")
    _count_commits(session, r2)
    assert await _remove(session, p.id) == {"ok": True}
    assert r2.deleted == ["produk/foto.jpg"]
    assert r2.commits_at_delete == [1]
    assert await session.get(ProdukStore, p.id) is None


@pytest.mark.asyncio
async def test_deleting_a_product_without_photo_deletes_nothing(session, r2):
    p = await _produk(session, None)
    await _remove(session, p.id)
    assert r2.deleted == []


@pytest.mark.asyncio
async def test_deleting_keeps_a_photo_another_product_still_uses(session, r2):
    p = await _produk(session, "produk/bersama.jpg")
    await _produk(session, "produk/bersama.jpg")
    await _remove(session, p.id)
    assert r2.deleted == []


@pytest.mark.asyncio
async def test_deleting_an_unknown_product_is_404_and_touches_no_photo(session, r2):
    with pytest.raises(HTTPException) as exc:
        await _remove(session, "tidak-ada")
    assert exc.value.status_code == 404
    assert r2.deleted == []
