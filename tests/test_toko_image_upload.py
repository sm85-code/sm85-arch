"""Tests for the standardized product-photo filename convention
(ampelkuning_yyyymmdd_random.ext) used by both upload paths: the manual
admin upload endpoint and the marketplace-copy rehost path."""
import re

import pytest

_PATTERN = re.compile(r"^ampelkuning_\d{8}_[0-9a-f]+\.(jpg|png|webp)$")


def test_build_nama_file_foto_matches_pattern_for_each_allowed_content_type():
    from tenants.toko.modules.toko.infrastructure.image_upload import build_nama_file_foto

    assert _PATTERN.match(build_nama_file_foto("image/jpeg"))
    assert _PATTERN.match(build_nama_file_foto("image/png"))
    assert _PATTERN.match(build_nama_file_foto("image/webp"))


def test_build_nama_file_foto_uses_correct_extension_per_content_type():
    from tenants.toko.modules.toko.infrastructure.image_upload import build_nama_file_foto

    assert build_nama_file_foto("image/jpeg").endswith(".jpg")
    assert build_nama_file_foto("image/png").endswith(".png")
    assert build_nama_file_foto("image/webp").endswith(".webp")


def test_build_nama_file_foto_random_component_differs_between_calls():
    from tenants.toko.modules.toko.infrastructure.image_upload import build_nama_file_foto

    names = {build_nama_file_foto("image/jpeg") for _ in range(10)}
    # 10 quick successive calls should not collide.
    assert len(names) == 10


@pytest.mark.asyncio
async def test_upload_produk_photo_uses_standardized_filename(monkeypatch):
    import tenants.toko.modules.toko.infrastructure.image_upload as image_upload

    monkeypatch.setattr(image_upload, "GDRIVE_FOLDER_ID_TOKO", "folder-123")
    monkeypatch.setenv("GDRIVE_SERVICE_ACCOUNT_JSON", '{"fake": true}')

    captured = {}

    async def _fake_upload_file_to_gdrive(file_bytes, file_name, content_type, folder_id):
        captured["file_name"] = file_name
        captured["content_type"] = content_type
        captured["folder_id"] = folder_id
        return {"id": "abc123"}

    async def _fake_set_public(file_id):
        captured["set_public_id"] = file_id

    monkeypatch.setattr(image_upload, "upload_file_to_gdrive", _fake_upload_file_to_gdrive)
    monkeypatch.setattr(image_upload, "set_gdrive_file_public", _fake_set_public)

    # Caller passes an arbitrary/untrustworthy original filename via
    # content_type only now -- there is no file_name parameter anymore,
    # proving the uploaded name is never taken from the caller.
    url = await image_upload.upload_produk_photo(b"fake-bytes", "image/jpeg")

    assert url == "https://drive.google.com/uc?export=view&id=abc123"
    assert _PATTERN.match(captured["file_name"])
    assert captured["file_name"].endswith(".jpg")
    assert captured["folder_id"] == "folder-123"
