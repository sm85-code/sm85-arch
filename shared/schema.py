"""Create missing tables and widen short varchar columns on boot.

App Platform has no one-off alembic job. Neon starts empty, so seed
used to hit `relation \"users\" does not exist`.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from shared.database import Base, engine

# Register metadata.
from modules.identity.infrastructure import models as _identity_models  # noqa: F401
from modules.siabumdes.infrastructure import models as _siabumdes_models  # noqa: F401
from modules.uu05_inventory.infrastructure import models as _inventory_models  # noqa: F401

logger = logging.getLogger("sm85.schema")

_WIDEN = [
    ("users", "password_hash", "VARCHAR(255)"),
    ("users", "id", "VARCHAR(64)"),
    ("users", "role", "VARCHAR(64)"),
    ("users", "unit_usaha_id", "VARCHAR(64)"),
    ("accounts", "code", "VARCHAR(64)"),
    ("accounts", "category", "VARCHAR(64)"),
    ("accounts", "subcategory", "VARCHAR(128)"),
]


async def ensure_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, column, new_type in _WIDEN:
            await conn.execute(
                text(
                    f"""
                    DO $$ BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name = :table_name
                              AND column_name = :column_name
                        ) THEN
                            EXECUTE 'ALTER TABLE {table} ALTER COLUMN {column} TYPE {new_type}';
                        END IF;
                    END $$;
                    """
                ),
                {"table_name": table, "column_name": column},
            )
    logger.info("schema ready")
