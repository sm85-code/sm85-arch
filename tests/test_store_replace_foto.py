"""Replacing a product photo removes the old R2 object -- but only after the
new key is committed, and never while another product still uses it."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.store.adapters.api.v1 import store_admin_router as admin_module
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import ProdukStore


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
async def test_old_photo_is_deleted_after_the_new_key_is_committed(session, r2):
    p = await _produk(session, "produk/lama.jpg")
    _count_commits(session, r2)
    out = await _replace(session, p.id)
    assert out["foto_key"] == "produk/baru.jpg"
    assert r2.deleted == ["produk/lama.jpg"]
    assert r2.commits_at_delete == [1]  # the new key was committed before the old object went


@pytest.mark.asyncio
async def test_first_photo_has_nothing_to_delete(session, r2):
    p = await _produk(session, None)
    await _replace(session, p.id)
    assert r2.deleted == []


@pytest.mark.asyncio
async def test_old_photo_still_used_by_another_product_is_kept(session, r2):
    p = await _produk(session, "produk/bersama.jpg")
    await _produk(session, "produk/bersama.jpg")
    await _replace(session, p.id)
    assert r2.deleted == []


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
