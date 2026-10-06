"""Authenticated encryption at the token SQL boundary, with legacy-row migration.

SHOPEE_TOKEN_ENCRYPTION_KEYS is a comma-separated Fernet keyring: first key
writes, remaining keys decrypt during rotation. Never derive keys from JWT or
Shopee partner credentials. Missing/invalid keys fail closed on token access.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text, text
from sqlalchemy.types import TypeDecorator

PREFIX = "fernet:v1:"


def keyring() -> MultiFernet:
    keys = [k.strip() for k in os.getenv("SHOPEE_TOKEN_ENCRYPTION_KEYS", "").split(",") if k.strip()]
    if not keys:
        raise RuntimeError("SHOPEE_TOKEN_ENCRYPTION_KEYS must be configured")
    try:
        return MultiFernet([Fernet(k.encode("ascii")) for k in keys])
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("Invalid SHOPEE_TOKEN_ENCRYPTION_KEYS configuration") from exc


def encrypt_token(value: str | None) -> str | None:
    if value is None:
        return None
    return PREFIX + keyring().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_token(value: str | None) -> str | None:
    if value is None:
        return None
    cipher = keyring()
    if not value.startswith(PREFIX):
        raise RuntimeError("Legacy marketplace token requires storage migration")
    try:
        return cipher.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError) as exc:
        raise RuntimeError("Stored marketplace token cannot be authenticated") from exc


class EncryptedToken(TypeDecorator):
    """ORM/API adapter sees plaintext; database writes always receive ciphertext."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_token(value)

    def process_result_value(self, value, dialect):
        return decrypt_token(value)


async def migrate_token_storage(conn) -> int:
    """Encrypt legacy rows and rewrap ciphertext under the primary key, atomically.

    Use SQL text to avoid ORM decryption and double encryption. Row locks also
    serialize this startup migration against token refresh in another process.
    """
    query = "SELECT id, access_token, refresh_token FROM mpe_akun_marketplace WHERE access_token IS NOT NULL OR refresh_token IS NOT NULL"
    if conn.dialect.name == "postgresql":
        query += " FOR UPDATE"
    rows = (await conn.execute(text(query))).mappings().all()
    if not rows:
        return 0
    cipher = keyring()
    changed = 0
    for row in rows:
        values = {}
        for field in ("access_token", "refresh_token"):
            value = row[field]
            if value is None:
                values[field] = None
            elif value.startswith(PREFIX):
                try:
                    values[field] = PREFIX + cipher.rotate(value[len(PREFIX):].encode("ascii")).decode("ascii")
                except (InvalidToken, UnicodeError) as exc:
                    raise RuntimeError("Stored marketplace token cannot be authenticated") from exc
            else:
                values[field] = encrypt_token(value)
        await conn.execute(
            text("UPDATE mpe_akun_marketplace SET access_token=:access_token, refresh_token=:refresh_token WHERE id=:id"),
            {"id": row["id"], **values},
        )
        changed += 1
    return changed
