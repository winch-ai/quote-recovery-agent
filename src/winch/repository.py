"""Postgres implementations of persistence protocols."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from winch.events import EventSink, EventType
from winch.protocols import DueTouchpoint, MessageDeduplicator, TouchpointQueue
from winch.state import Touchpoint, TouchpointStatus


class PostgresTouchpointQueue:
    """Postgres-backed durable touchpoint queue satisfying TouchpointQueue protocol."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def schedule(self, quote_id: str, touchpoints: list[Touchpoint]) -> None:
        """Persist the plan. Idempotent on (quote_id, touchpoint_index).

        Calling twice with the same touchpoints must not duplicate rows and must
        not reset the status of a row already progressed.
        """
        if not touchpoints:
            return

        params = [
            (
                quote_id,
                tp.index,
                tp.template_name,
                tp.due_at if tp.due_at.tzinfo is not None else tp.due_at.replace(tzinfo=timezone.utc),
                tp.status.value if hasattr(tp.status, "value") else str(tp.status),
                tp.provider_message_id,
            )
            for tp in touchpoints
        ]

        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO touchpoints (
                        quote_id,
                        touchpoint_index,
                        template_name,
                        due_at,
                        status,
                        provider_message_id
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (quote_id, touchpoint_index) DO NOTHING;
                    """,
                    params,
                )

    async def claim_due(self, now: datetime, limit: int = 20) -> list[DueTouchpoint]:
        """Atomically claim touchpoints due at or before `now`.

        Returns only PENDING touchpoints with due_at <= now, marks them
        AWAITING_GATE and sets claimed_at, all in one transaction using
        FOR UPDATE SKIP LOCKED.
        """
        if limit <= 0:
            return []

        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)

        sql = """
        WITH due AS (
            SELECT quote_id, touchpoint_index, due_at
            FROM touchpoints
            WHERE status = %s AND due_at <= %s
            ORDER BY due_at ASC, quote_id ASC, touchpoint_index ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE touchpoints t
        SET status = %s,
            claimed_at = now()
        FROM due
        WHERE t.quote_id = due.quote_id
          AND t.touchpoint_index = due.touchpoint_index
        RETURNING t.quote_id, t.touchpoint_index, t.due_at;
        """

        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    sql,
                    (
                        TouchpointStatus.PENDING.value,
                        now_utc,
                        limit,
                        TouchpointStatus.AWAITING_GATE.value,
                    ),
                )
                rows = await cur.fetchall()

        return [
            DueTouchpoint(
                quote_id=row[0],
                touchpoint_index=row[1],
                due_at=row[2],
            )
            for row in rows
        ]

    async def mark(self, quote_id: str, index: int, status: TouchpointStatus) -> None:
        """Record a terminal status for a claimed touchpoint."""
        status_str = status.value if hasattr(status, "value") else str(status)
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE touchpoints
                    SET status = %s
                    WHERE quote_id = %s AND touchpoint_index = %s;
                    """,
                    (status_str, quote_id, index),
                )


class PostgresDeduplicator:
    """Postgres-backed message deduplicator satisfying MessageDeduplicator protocol."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def seen(self, provider_message_id: str) -> bool:
        """Claim the id atomically. Return True only if it was already present."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO processed_messages (provider_message_id, seen_at)
                    VALUES (%s, now())
                    ON CONFLICT (provider_message_id) DO NOTHING
                    RETURNING provider_message_id;
                    """,
                    (provider_message_id,),
                )
                row = await cur.fetchone()
                return row is None

    async def release(self, provider_message_id: str) -> None:
        """Un-claim an id whose handler failed, so redelivery can retry it."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    DELETE FROM processed_messages
                    WHERE provider_message_id = %s;
                    """,
                    (provider_message_id,),
                )


class PostgresEventSink:
    """Postgres-backed event sink satisfying EventSink protocol."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def write(
        self,
        quote_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> None:
        """Persist an event to the events table. Never silently drop a row."""
        event_type_str = event_type.value if hasattr(event_type, "value") else str(event_type)
        payload_param = Jsonb(payload) if payload is not None else None
        at_param = (
            at if (at is None or at.tzinfo is not None)
            else at.replace(tzinfo=timezone.utc)
        )

        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO events (quote_id, event_type, payload, at)
                    VALUES (%s, %s, %s, COALESCE(%s, now()));
                    """,
                    (quote_id, event_type_str, payload_param, at_param),
                )


class PostgresContactWindow:
    """Tracks the last time each WhatsApp number messaged us.

    This exists separately from the events table because window-openness is a
    delivery-layer fact ("did this number message us in the last 24h"), not a
    business-layer one. The original check queried event_type='customer_replied',
    which is only ever written for end customers (see nodes.triage) - so the
    contractor's own window was structurally always closed, and every attempt
    to notify the contractor via send_freeform was silently refused.

    record_inbound() must be called for EVERY inbound message, contractor or
    customer, the moment it is received - not just ones that reach the graph.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def record_inbound(self, wa_id: str) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO inbound_contacts (wa_id, last_seen_at)
                    VALUES (%s, now())
                    ON CONFLICT (wa_id) DO UPDATE
                    SET last_seen_at = now();
                    """,
                    (wa_id,),
                )

    async def is_open(self, wa_id: str) -> bool:
        """True if wa_id messaged us within the last 24 hours."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT 1 FROM inbound_contacts
                    WHERE wa_id = %s AND last_seen_at > now() - interval \'24 hours\'
                    LIMIT 1;
                    """,
                    (wa_id,),
                )
                return await cur.fetchone() is not None


class PostgresThreadIndex:
    """Postgres-backed index mapping WhatsApp contacts and interrupts to threads."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def bind_customer(self, quote_id: str, customer_wa_id: str) -> None:
        """Record which number is the customer on this quote. Idempotent."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO quote_threads (quote_id, customer_wa_id, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (quote_id) DO UPDATE
                    SET customer_wa_id = EXCLUDED.customer_wa_id,
                        updated_at = now();
                    """,
                    (quote_id, customer_wa_id),
                )

    async def mark_awaiting(self, quote_id: str, awaiting: bool) -> None:
        """Flag/unflag this quote as waiting on a contractor answer."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO quote_threads (quote_id, awaiting, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (quote_id) DO UPDATE
                    SET awaiting = EXCLUDED.awaiting,
                        updated_at = now();
                    """,
                    (quote_id, awaiting),
                )

    async def close(self, quote_id: str) -> None:
        """Mark the thread closed so it stops matching lookups."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO quote_threads (quote_id, closed, updated_at)
                    VALUES (%s, true, now())
                    ON CONFLICT (quote_id) DO UPDATE
                    SET closed = true,
                        updated_at = now();
                    """,
                    (quote_id,),
                )

    async def thread_for_customer(self, customer_wa_id: str) -> str | None:
        """Most recent non-closed quote for that customer number, else None."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT quote_id
                    FROM quote_threads
                    WHERE customer_wa_id = %s AND closed = false
                    ORDER BY updated_at DESC
                    LIMIT 1;
                    """,
                    (customer_wa_id,),
                )
                row = await cur.fetchone()
                return row[0] if row is not None else None

    async def pending_thread(self) -> str | None:
        """Most recently flagged awaiting, non-closed thread, else None.

        Ambiguous the instant more than one quote is simultaneously awaiting -
        prefer record_prompt()/resolve_reply() below when the inbound message
        carries a reply_to_message_id, which resolves deterministically instead
        of guessing. This remains the fallback for a plain (non-reply) message
        when exactly one thread is awaiting.
        """
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT quote_id
                    FROM quote_threads
                    WHERE awaiting = true AND closed = false
                    ORDER BY updated_at DESC
                    LIMIT 1;
                    """
                )
                row = await cur.fetchone()
                return row[0] if row is not None else None

    async def record_prompt(self, quote_id: str, provider_message_id: str) -> None:
        """Record that this outbound message is the prompt for this quote.

        Call this every time a contractor-facing prompt is sent (a rendered
        interrupt payload). It is what lets a swipe-reply be resolved to the
        exact quote it answers, rather than "whichever quote is most recently
        awaiting" - the latter silently approved the wrong quote in production
        the moment two were in flight at once.
        """
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO prompt_messages (provider_message_id, quote_id, sent_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (provider_message_id) DO NOTHING;
                    """,
                    (provider_message_id, quote_id),
                )

    async def resolve_reply(self, reply_to_message_id: str | None) -> str | None:
        """Look up the quote a swipe-reply is about, if it was a reply at all.

        Returns None when reply_to_message_id is None (not a reply) or the id
        is not one we recorded a prompt for - the caller falls back to
        pending_thread() in that case.
        """
        if not reply_to_message_id:
            return None
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT quote_id FROM prompt_messages WHERE provider_message_id = %s;",
                    (reply_to_message_id,),
                )
                row = await cur.fetchone()
                return row[0] if row is not None else None

