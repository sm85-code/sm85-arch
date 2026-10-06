"""Use an ephemeral token-encryption key for isolated test databases only."""
import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def isolated_marketplace_token_key(monkeypatch):
    monkeypatch.setenv("SHOPEE_TOKEN_ENCRYPTION_KEYS", Fernet.generate_key().decode())
