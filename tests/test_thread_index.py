"""Integration tests for PostgresThreadIndex."""
from __future__ import annotations

import asyncio
import os
import pytest

from winch.db import init_schema, make_pool
from winch.repository import PostgresThreadIndex

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://postgres:winchtest@localhost:55432/winch_test",
)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def pool():
    p = await make_pool(TEST_DATABASE_URL, min_size=2, max_size=10)
    await init_schema(p)
    try:
        yield p
    finally:
        async with p.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("TRUNCATE quote_threads CASCADE;")
        await p.close()


@pytest.fixture(autouse=True)
async def clean_db(pool):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("TRUNCATE quote_threads CASCADE;")
    yield
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("TRUNCATE quote_threads CASCADE;")


async def test_init_schema_is_idempotent(pool):
    # Run once
    await init_schema(pool)
    # Run twice
    await init_schema(pool)

    # Verify quote_threads exists
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'quote_threads';
                """
            )
            tables = {row[0] for row in await cur.fetchall()}
            assert "quote_threads" in tables


async def test_bind_then_thread_for_customer_returns_quote_id(pool):
    index = PostgresThreadIndex(pool)
    await index.bind_customer("quote-1", "+1234567890")
    result = await index.thread_for_customer("+1234567890")
    assert result == "quote-1"


async def test_binding_same_pair_twice_does_not_error(pool):
    index = PostgresThreadIndex(pool)
    await index.bind_customer("quote-1", "+1234567890")
    await index.bind_customer("quote-1", "+1234567890")

    result = await index.thread_for_customer("+1234567890")
    assert result == "quote-1"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM quote_threads WHERE quote_id = %s;",
                ("quote-1",),
            )
            count = (await cur.fetchone())[0]
            assert count == 1


async def test_closed_thread_not_returned_by_thread_for_customer(pool):
    index = PostgresThreadIndex(pool)
    await index.bind_customer("quote-1", "+1234567890")
    await index.close("quote-1")
    result = await index.thread_for_customer("+1234567890")
    assert result is None


async def test_two_quotes_same_customer_most_recent_non_closed_wins(pool):
    index = PostgresThreadIndex(pool)
    await index.bind_customer("quote-1", "+1234567890")
    await asyncio.sleep(0.01)
    await index.bind_customer("quote-2", "+1234567890")

    assert await index.thread_for_customer("+1234567890") == "quote-2"

    # Close quote-2 -> older non-closed quote-1 wins
    await index.close("quote-2")
    assert await index.thread_for_customer("+1234567890") == "quote-1"

    # Close quote-1 -> none remain
    await index.close("quote-1")
    assert await index.thread_for_customer("+1234567890") is None


async def test_pending_thread_returns_none_when_nothing_awaiting(pool):
    index = PostgresThreadIndex(pool)
    result = await index.pending_thread()
    assert result is None


async def test_mark_awaiting_true_then_pending_thread_returns_it(pool):
    index = PostgresThreadIndex(pool)
    await index.mark_awaiting("quote-1", True)
    result = await index.pending_thread()
    assert result == "quote-1"


async def test_mark_awaiting_false_clears_it(pool):
    index = PostgresThreadIndex(pool)
    await index.mark_awaiting("quote-1", True)
    assert await index.pending_thread() == "quote-1"

    await index.mark_awaiting("quote-1", False)
    assert await index.pending_thread() is None


async def test_closed_thread_never_returned_by_pending_thread(pool):
    index = PostgresThreadIndex(pool)
    await index.mark_awaiting("quote-1", True)
    assert await index.pending_thread() == "quote-1"

    await index.close("quote-1")
    assert await index.pending_thread() is None


async def test_two_awaiting_threads_most_recently_updated_wins(pool):
    index = PostgresThreadIndex(pool)
    await index.mark_awaiting("quote-1", True)
    await asyncio.sleep(0.01)
    await index.mark_awaiting("quote-2", True)

    assert await index.pending_thread() == "quote-2"

    # Updating quote-1 makes it the most recently updated
    await asyncio.sleep(0.01)
    await index.mark_awaiting("quote-1", True)
    assert await index.pending_thread() == "quote-1"

    # Closing quote-1 leaves quote-2 awaiting
    await index.close("quote-1")
    assert await index.pending_thread() == "quote-2"
