import asyncio
import os

import pytest
from sqlalchemy.exc import OperationalError


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL tidak tersedia")
def test_repository_round_trip_and_migration_marker():
    from database import applied_migrations, close_database, db, init_database

    async def scenario():
        await init_database()
        marker = "integration-test"
        try:
            await db.integration_test.remove_many({"id": marker})
            await db.integration_test.create({"id": marker, "amount": "125.10", "scope": "test"})
            row = await db.integration_test.select_one({"id": marker})
            assert row["amount"] == "125.10"
            assert "20260913_02" in await applied_migrations()
        finally:
            await db.integration_test.remove_many({"id": marker})
            await close_database()

    try:
        asyncio.run(scenario())
    except (OperationalError, OSError) as exc:
        pytest.skip(f"PostgreSQL integration unavailable: {exc}")
