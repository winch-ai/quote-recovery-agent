"""Integration tests for PostgresContactWindow.

This class exists because of a real production bug: the WhatsApp free-form
send window was originally checked by querying events for
event_type='customer_replied', which is only ever written for end customers
(see nodes.triage). The contractor's own number never wrote that event type,
so is_open() for the contractor was structurally always False - every attempt
to notify the contractor via send_freeform was silently refused, and four real
quotes parsed correctly and then told nobody.

record_inbound() must be called for every inbound message unconditionally,
before any routing decision, because the window opens on receipt regardless of
what the message goes on to do.
"""
from __future__ import annotations

import uuid as _uuid
import os

import pytest

from winch.db import init_schema, make_pool
from winch.repository import PostgresContactWindow

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://postgres:winchtest@localhost:55432/winch_test",
)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def pool():
    _SCHEMA = f"winch_run_{_uuid.uuid4().hex[:10]}"
    p = await make_pool(TEST_DATABASE_URL, min_size=2, max_size=10)
    async with p.connection() as _c:
        await _c.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")
    await p.close()
    p = await make_pool(
        TEST_DATABASE_URL + f"?options=-csearch_path%3D{_SCHEMA}",
        min_size=2, max_size=10,
    )
    await init_schema(p)
    try:
        yield p
    finally:
        await p.close()


pytestmark = pytest.mark.anyio


async def test_unknown_number_has_no_open_window(pool):
    window = PostgresContactWindow(pool)
    assert await window.is_open("447700900001") is False


async def test_recorded_number_has_an_open_window(pool):
    window = PostgresContactWindow(pool)
    await window.record_inbound("447700900001")
    assert await window.is_open("447700900001") is True


async def test_window_is_per_number(pool):
    """The bug this class fixes was scoped to ONE number (the contractor).
    Recording one number's inbound must never open another's window."""
    window = PostgresContactWindow(pool)
    await window.record_inbound("447700900001")
    assert await window.is_open("447700900002") is False


async def test_recording_twice_is_idempotent_and_refreshes(pool):
    window = PostgresContactWindow(pool)
    await window.record_inbound("447700900001")
    await window.record_inbound("447700900001")
    assert await window.is_open("447700900001") is True


async def test_contractor_and_customer_numbers_are_independent(pool):
    """This is precisely the scenario that was broken in production: the
    contractor forwards a quote (their number), and their window must open
    independently of any customer number ever being involved at all."""
    window = PostgresContactWindow(pool)
    contractor = "447700900555"
    await window.record_inbound(contractor)
    assert await window.is_open(contractor) is True
    assert await window.is_open("254700000000") is False
