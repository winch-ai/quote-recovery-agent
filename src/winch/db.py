"""Database initialization and connection pool management."""
from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS touchpoints (
    quote_id text NOT NULL,
    touchpoint_index integer NOT NULL,
    template_name text NOT NULL,
    due_at timestamptz NOT NULL,
    status text NOT NULL,
    claimed_at timestamptz,
    provider_message_id text,
    PRIMARY KEY (quote_id, touchpoint_index)
);

CREATE INDEX IF NOT EXISTS idx_touchpoints_status_due_at ON touchpoints (status, due_at);

CREATE TABLE IF NOT EXISTS processed_messages (
    provider_message_id text PRIMARY KEY,
    seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id bigserial PRIMARY KEY,
    quote_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb,
    at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_quote_id_at ON events (quote_id, at);

CREATE TABLE IF NOT EXISTS inbound_contacts (
    wa_id text PRIMARY KEY,
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quote_threads (
    quote_id text PRIMARY KEY,
    customer_wa_id text,
    awaiting boolean NOT NULL DEFAULT false,
    closed boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_quote_threads_customer_closed ON quote_threads (customer_wa_id, closed);
CREATE INDEX IF NOT EXISTS idx_quote_threads_awaiting_closed_updated_at ON quote_threads (awaiting, closed, updated_at DESC);

-- Maps a sent WhatsApp message (the prompt) to the quote it is about, so a
-- contractor's swipe-reply can be resolved deterministically instead of
-- guessed as "whichever quote is most recently awaiting". Without this, two
-- quotes simultaneously awaiting a bare "yes" are indistinguishable and a
-- reply can silently approve the wrong one - this happened in production
-- before this table existed.
CREATE TABLE IF NOT EXISTS prompt_messages (
    provider_message_id text PRIMARY KEY,
    quote_id text NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_prompt_messages_quote_id ON prompt_messages (quote_id);
"""


async def init_schema(pool: AsyncConnectionPool) -> None:
    """Apply SCHEMA_SQL. Idempotent — safe to run on every boot."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)


async def make_pool(dsn: str, min_size: int = 1, max_size: int = 5) -> AsyncConnectionPool:
    """Open a psycopg_pool AsyncConnectionPool and wait until it is usable."""
    pool = None
    try:
        pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        await pool.wait()
        return pool
    except Exception:
        if pool is not None:
            try:
                await pool.close()
            except Exception:
                pass
        raise RuntimeError("Failed to initialize database connection pool") from None
