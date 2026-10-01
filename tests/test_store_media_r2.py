"""store: product photo upload to Cloudflare R2, against botocore's Stubber (no network, no credentials)."""
from __future__ import annotations

import boto3
import pytest
from botocore.config import Config
from botocore.stub import ANY, Stubber
from fastapi import HTTPException

from tenants.store.modules.store.infrastructure import media_storage

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"0" * 16


@pytest.fixture
def r2_env(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acc123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret-example")
    monkeypatch.setenv("R2_BUCKET", "ampelkuning-media")


@pytest.fixture
def stubbed(monkeypatch, r2_env):
    """A real boto3 S3 client whose HTTP layer is replaced by a Stubber."""
    client = boto3.client(
        "s3",
        endpoint_url="https://acc123.r2.cloudflarestorage.com",
        aws_access_key_id="AKIDEXAMPLE",
        aws_secret_access_key="secret-example",
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    stubber = Stubber(client)
    monkeypatch.setattr(media_storage, "_client", lambda cfg: client)
    with stubber:
        yield stubber


def test_config_needs_all_four_values(monkeypatch, r2_env):
    assert media_storage.r2_config() == ("acc123", "AKIDEXAMPLE", "secret-example", "ampelkuning-media")
    monkeypatch.setenv("R2_BUCKET", "  ")
    assert media_storage.r2_config() is None
    monkeypatch.delenv("R2_ACCOUNT_ID")
    assert media_storage.r2_config() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "data", "ext"),
    [("image/png", PNG, "png"), ("image/jpeg", JPEG, "jpg"), ("image/webp", WEBP, "webp")],
)
async def test_upload_puts_object_with_long_cache_and_returns_key(stubbed, content_type, data, ext):
    stubbed.add_response(
        "put_object",
        {},
        {"Bucket": "ampelkuning-media", "Key": ANY, "Body": data, "ContentType": content_type, "CacheControl": "public, max-age=31536000, immutable"},
    )

    key = await media_storage.upload_produk_photo(data, content_type)

    stubbed.assert_no_pending_responses()
    assert key.startswith("produk/ampelkuning_") and key.endswith(f".{ext}")


@pytest.mark.asyncio
async def test_each_upload_gets_a_new_unguessable_key(stubbed):
    keys = set()
    for _ in range(3):
        stubbed.add_response("put_object", {})
        keys.add(await media_storage.upload_produk_photo(PNG, "image/png"))
    assert len(keys) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "data"),
    [
        ("image/png", JPEG),  # bytes do not match the declared type
        ("image/jpeg", b"<html><script>alert(1)</script></html>"),
        ("image/webp", b"RIFF0000WAVE" + b"0" * 8),  # RIFF but not WebP
        ("image/png", b""),
    ],
)
async def test_content_is_checked_not_just_the_header(stubbed, content_type, data):
    with pytest.raises(HTTPException) as exc:
        await media_storage.upload_produk_photo(data, content_type)
    assert exc.value.status_code == 400
    stubbed.assert_no_pending_responses()  # nothing was sent to R2


@pytest.mark.asyncio
async def test_r2_failure_becomes_502_without_leaking_details(stubbed):
    stubbed.add_client_error("put_object", service_error_code="AccessDenied", service_message="Invalid key AKIDEXAMPLE", http_status_code=403)

    with pytest.raises(HTTPException) as exc:
        await media_storage.upload_produk_photo(PNG, "image/png")

    assert exc.value.status_code == 502
    assert "AKID" not in exc.value.detail and "AccessDenied" not in exc.value.detail


@pytest.mark.asyncio
async def test_unconfigured_r2_is_503_and_never_builds_a_client(monkeypatch):
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(name, raising=False)

    def boom(cfg):
        raise AssertionError("no client must be created without configuration")

    monkeypatch.setattr(media_storage, "_client", boom)
    with pytest.raises(HTTPException) as exc:
        await media_storage.upload_produk_photo(PNG, "image/png")
    assert exc.value.status_code == 503


def test_real_client_targets_the_account_endpoint_with_sigv4(r2_env):
    media_storage._client.cache_clear()
    client = media_storage._client(media_storage.r2_config())
    assert client.meta.endpoint_url == "https://acc123.r2.cloudflarestorage.com"
    assert client.meta.region_name == "auto"
    assert client.meta.config.signature_version == "s3v4"
    media_storage._client.cache_clear()


def test_media_url_joins_base_and_key(monkeypatch):
    monkeypatch.setenv("MEDIA_BASE_URL", "https://img.ampelkuning.com/")
    assert media_storage.media_url("produk/a.png") == "https://img.ampelkuning.com/produk/a.png"
    monkeypatch.delenv("MEDIA_BASE_URL")
    assert media_storage.media_url("produk/a.png") is None
