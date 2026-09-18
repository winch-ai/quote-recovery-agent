"""Integration tests for Postgres persistence layer."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import pytest

from winch.db import SCHEMA_SQL, init_schema, make_pool
from winch.events import EventSink, EventType
from winch.protocols import DueTouchpoint, MessageDeduplicator, TouchpointQueue
from winch.repository import (
    PostgresDeduplicator,
    PostgresEventSink,
    PostgresTouchpointQueue,
)
from winch.state import Touchpoint, TouchpointStatus

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
                await cur.execute("TRUNCATE touchpoints, processed_messages, events CASCADE;")
        await p.close()


@pytest.fixture(autouse=True)
async def clean_db(pool):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("TRUNCATE touchpoints, processed_messages, events CASCADE;")
    yield
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("TRUNCATE touchpoints, processed_messages, events CASCADE;")


def test_protocol_conformance(pool):
    queue = PostgresTouchpointQueue(pool)
    dedup = PostgresDeduplicator(pool)
    sink = PostgresEventSink(pool)

    assert isinstance(queue, TouchpointQueue)
    assert isinstance(dedup, MessageDeduplicator)
    assert isinstance(sink, EventSink)


async def test_init_schema_is_idempotent(pool):
    # Run once
    await init_schema(pool)
    # Run twice
    await init_schema(pool)

    # Verify tables exist by inserting dummy rows or querying information_schema
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('touchpoints', 'processed_messages', 'events');
                """
            )
            tables = {row[0] for row in await cur.fetchall()}
            assert {"touchpoints", "processed_messages", "events"}.issubset(tables)


async def test_schedule_then_claim_due_returns_the_due_touchpoint(pool):
    queue = PostgresTouchpointQueue(pool)
    now = datetime.now(timezone.utc)
    due_time = now - timedelta(minutes=5)

    touchpoints = [
        Touchpoint(
            index=0,
            template_name="checkin_soft",
            due_at=due_time,
            status=TouchpointStatus.PENDING,
        )
    ]
    await queue.schedule("quote-1", touchpoints)

    claimed = await queue.claim_due(now)
    assert len(claimed) == 1
    assert isinstance(claimed[0], DueTouchpoint)
    assert claimed[0].quote_id == "quote-1"
    assert claimed[0].touchpoint_index == 0
    assert claimed[0].due_at == due_time

    # Verify state in DB was updated to AWAITING_GATE and claimed_at is set
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, claimed_at FROM touchpoints WHERE quote_id = %s AND touchpoint_index = %s;",
                ("quote-1", 0),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == TouchpointStatus.AWAITING_GATE.value
            assert row[1] is not None


async def test_touchpoint_due_in_future_is_not_returned(pool):
    queue = PostgresTouchpointQueue(pool)
    now = datetime.now(timezone.utc)
    future_time = now + timedelta(hours=2)

    touchpoints = [
        Touchpoint(
            index=0,
            template_name="checkin_soft",
            due_at=future_time,
            status=TouchpointStatus.PENDING,
        )
    ]
    await queue.schedule("quote-future", touchpoints)

    claimed = await queue.claim_due(now)
    assert len(claimed) == 0

    # Verify status is still PENDING and claimed_at is NULL
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, claimed_at FROM touchpoints WHERE quote_id = %s AND touchpoint_index = %s;",
                ("quote-future", 0),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == TouchpointStatus.PENDING.value
            assert row[1] is None


async def test_concurrency_two_claim_due_never_return_same_touchpoint(pool):
    """Concurrency: two claim_due calls running concurrently via asyncio.gather

    never return the same touchpoint. This is the most important test in the file.
    """
    queue = PostgresTouchpointQueue(pool)
    now = datetime.now(timezone.utc)

    # Schedule 20 touchpoints all due now
    touchpoints = [
        Touchpoint(
            index=i,
            template_name="checkin_soft",
            due_at=now - timedelta(seconds=i),
            status=TouchpointStatus.PENDING,
        )
        for i in range(20)
    ]
    await queue.schedule("quote-concurrent", touchpoints)

    # Concurrently claim with two workers
    claimed_1, claimed_2 = await asyncio.gather(
        queue.claim_due(now, limit=12),
        queue.claim_due(now, limit=12),
    )

    ids_1 = {(tp.quote_id, tp.touchpoint_index) for tp in claimed_1}
    ids_2 = {(tp.quote_id, tp.touchpoint_index) for tp in claimed_2}

    # CRITICAL: Intersection must be empty!
    overlap = ids_1.intersection(ids_2)
    assert len(overlap) == 0, f"Concurrent claim returned duplicate touchpoints: {overlap}"

    # All 20 items should have been claimed between worker 1 and worker 2
    assert len(ids_1) + len(ids_2) == 20
    assert ids_1.union(ids_2) == {("quote-concurrent", i) for i in range(20)}


async def test_schedule_called_twice_does_not_duplicate_rows(pool):
    queue = PostgresTouchpointQueue(pool)
    now = datetime.now(timezone.utc)

    touchpoints = [
        Touchpoint(
            index=0,
            template_name="checkin_soft",
            due_at=now,
            status=TouchpointStatus.PENDING,
        ),
        Touchpoint(
            index=1,
            template_name="schedule_nudge",
            due_at=now + timedelta(days=1),
            status=TouchpointStatus.PENDING,
        ),
    ]

    await queue.schedule("quote-dup", touchpoints)
    await queue.schedule("quote-dup", touchpoints)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM touchpoints WHERE quote_id = %s;",
                ("quote-dup",),
            )
            count = (await cur.fetchone())[0]
            assert count == 2


async def test_schedule_does_not_reset_row_already_marked_sent(pool):
    queue = PostgresTouchpointQueue(pool)
    now = datetime.now(timezone.utc)

    touchpoint = Touchpoint(
        index=0,
        template_name="checkin_soft",
        due_at=now - timedelta(minutes=1),
        status=TouchpointStatus.PENDING,
    )
    await queue.schedule("quote-progress", [touchpoint])

    # Mark as SENT
    await queue.mark("quote-progress", 0, TouchpointStatus.SENT)

    # Try to schedule again with PENDING status
    await queue.schedule("quote-progress", [touchpoint])

    # Verify status was NOT reset to PENDING
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM touchpoints WHERE quote_id = %s AND touchpoint_index = %s;",
                ("quote-progress", 0),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == TouchpointStatus.SENT.value


async def test_mark_records_a_terminal_status(pool):
    queue = PostgresTouchpointQueue(pool)
    now = datetime.now(timezone.utc)

    touchpoint = Touchpoint(
        index=0,
        template_name="checkin_soft",
        due_at=now,
        status=TouchpointStatus.PENDING,
    )
    await queue.schedule("quote-terminal", [touchpoint])

    for status in [
        TouchpointStatus.AWAITING_GATE,
        TouchpointStatus.APPROVED,
        TouchpointStatus.SENT,
        TouchpointStatus.DELIVERED,
        TouchpointStatus.FAILED,
        TouchpointStatus.RELAYED,
    ]:
        await queue.mark("quote-terminal", 0, status)

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status FROM touchpoints WHERE quote_id = %s AND touchpoint_index = %s;",
                    ("quote-terminal", 0),
                )
                row = await cur.fetchone()
                assert row is not None
                assert row[0] == status.value


async def test_seen_returns_false_then_true_for_the_same_id(pool):
    dedup = PostgresDeduplicator(pool)
    msg_id = "wamid.HBgLMTIzNDU2Nzg5MA=="

    # First time seen: should claim and return False
    first = await dedup.seen(msg_id)
    assert first is False

    # Second time seen: already claimed, should return True
    second = await dedup.seen(msg_id)
    assert second is True


async def test_release_makes_previously_seen_id_unseen_again(pool):
    dedup = PostgresDeduplicator(pool)
    msg_id = "wamid.retry_test_123"

    # First seen -> False
    assert await dedup.seen(msg_id) is False
    # Second seen -> True
    assert await dedup.seen(msg_id) is True

    # Release it
    await dedup.release(msg_id)

    # Seen again after release -> False
    assert await dedup.seen(msg_id) is False


async def test_eventsink_write_persists_and_is_readable_back(pool):
    sink = PostgresEventSink(pool)
    now = datetime.now(timezone.utc)
    payload = {
        "customer_phone": "+447700900000",
        "quote_total": 12500.50,
        "tags": ["fencing", "urgent"],
    }

    await sink.write(
        quote_id="quote-event-1",
        event_type=EventType.QUOTE_RECEIVED,
        payload=payload,
        at=now,
    )

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT quote_id, event_type, payload, at
                FROM events
                WHERE quote_id = %s;
                """,
                ("quote-event-1",),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == "quote-event-1"
            assert row[1] == EventType.QUOTE_RECEIVED.value
            assert row[2] == payload
            assert isinstance(row[2], dict)
            assert row[2]["quote_total"] == 12500.50
            # Timestamps are comparable (allowing microsecond precision matching)
            assert abs((row[3] - now).total_seconds()) < 0.001


async def test_eventsink_write_with_defaults(pool):
    sink = PostgresEventSink(pool)

    # Write without payload and without explicit at
    await sink.write(
        quote_id="quote-event-default",
        event_type=EventType.DELIVERY_CONFIRMED,
    )

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT quote_id, event_type, payload, at
                FROM events
                WHERE quote_id = %s;
                """,
                ("quote-event-default",),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == "quote-event-default"
            assert row[1] == EventType.DELIVERY_CONFIRMED.value
            assert row[2] is None
            assert row[3] is not None


async def test_limit_on_claim_due_is_respected(pool):
    queue = PostgresTouchpointQueue(pool)
    now = datetime.now(timezone.utc)

    # Schedule 5 touchpoints
    touchpoints = [
        Touchpoint(
            index=i,
            template_name="checkin_soft",
            due_at=now - timedelta(minutes=10 - i),
            status=TouchpointStatus.PENDING,
        )
        for i in range(5)
    ]
    await queue.schedule("quote-limit", touchpoints)

    # Claim with limit=2
    first_batch = await queue.claim_due(now, limit=2)
    assert len(first_batch) == 2
    assert [tp.touchpoint_index for tp in first_batch] == [0, 1]

    # Claim with limit=2 again
    second_batch = await queue.claim_due(now, limit=2)
    assert len(second_batch) == 2
    assert [tp.touchpoint_index for tp in second_batch] == [2, 3]

    # Claim remaining with limit=2
    third_batch = await queue.claim_due(now, limit=2)
    assert len(third_batch) == 1
    assert [tp.touchpoint_index for tp in third_batch] == [4]

    # No more left
    fourth_batch = await queue.claim_due(now, limit=2)
    assert len(fourth_batch) == 0


async def test_no_dsn_leak_in_exception():
    secret_pw = "super_secret_password_never_log_me"
    bad_dsn = f"postgresql://invalid_user:{secret_pw}@127.0.0.1:55432/nonexistent_db"

    with pytest.raises(RuntimeError) as exc_info:
        await make_pool(bad_dsn, min_size=1, max_size=1)

    # Verify no secret password or DSN in the exception representation or cause
    err_str = str(exc_info.value)
    assert secret_pw not in err_str
    assert bad_dsn not in err_str
    assert exc_info.value.__cause__ is None
