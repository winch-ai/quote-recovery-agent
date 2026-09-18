"""The LangGraph checkpointer needs an autocommit pool.

`AsyncPostgresSaver.setup()` issues `CREATE INDEX CONCURRENTLY`, which Postgres
refuses inside a transaction block. psycopg pools are transactional by default,
so the app gives the checkpointer its own autocommit pool rather than making
every application query autocommit.

This surfaced only on deploy — the container built fine and then crashed on
startup. Pinned here so it fails in CI instead.
"""
import os
import uuid

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:winchtest@localhost:55432/winch_test"
)


async def _schema_pool(autocommit: bool):
    """Each run gets its own schema so concurrent suites cannot collide."""
    schema = f"cp_{uuid.uuid4().hex[:10]}"
    boot = AsyncConnectionPool(DSN, min_size=1, max_size=1, open=False,
                              kwargs={"autocommit": True})
    await boot.open(); await boot.wait()
    async with boot.connection() as conn:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    await boot.close()

    kwargs = {"autocommit": True} if autocommit else {}
    pool = AsyncConnectionPool(
        DSN + f"?options=-csearch_path%3D{schema}",
        min_size=1, max_size=2, open=False, kwargs=kwargs,
    )
    await pool.open(); await pool.wait()
    return pool


async def test_autocommit_pool_can_set_up_the_checkpointer():
    """This is how Runtime.start builds it. If this fails, the service will not boot."""
    pool = await _schema_pool(autocommit=True)
    try:
        await AsyncPostgresSaver(pool).setup()
    finally:
        await pool.close()


async def test_transactional_pool_cannot():
    """The control. If this ever stops raising, the test above proves nothing.

    Guards against someone 'simplifying' the two pools back into one.
    """
    import psycopg

    pool = await _schema_pool(autocommit=False)
    try:
        with pytest.raises(psycopg.errors.ActiveSqlTransaction):
            await AsyncPostgresSaver(pool).setup()
    finally:
        await pool.close()
