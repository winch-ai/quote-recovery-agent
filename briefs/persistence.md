# Task: persistence

## Goal
Implement the Postgres persistence layer: schema, the durable touchpoint queue
with atomic claiming, the event sink, and the deduplicator.

## Context you need
Cloud Run may run several instances, and the scheduler tick fires on all of them
at once. If two instances claim the same touchpoint, the customer receives the
same follow-up twice under the contractor's name. `claim_due` must therefore be
atomic under concurrency — `SELECT ... FOR UPDATE SKIP LOCKED` inside a single
transaction that also marks the row claimed. A `SELECT` then a separate `UPDATE`
races and is wrong.

The `events` table is the instrument this whole project exists to produce. It
must never silently drop a row.

## Contract — DO NOT CHANGE ANY OF THIS

These already exist in `src/winch/protocols.py` and `src/winch/events.py`.
Import them. Do not redefine or modify them:

```python
class DueTouchpoint(BaseModel):
    quote_id: str
    touchpoint_index: int
    due_at: datetime

class TouchpointQueue(Protocol):
    async def schedule(self, quote_id: str, touchpoints: list[Touchpoint]) -> None: ...
    async def claim_due(self, now: datetime, limit: int = 20) -> list[DueTouchpoint]: ...
    async def mark(self, quote_id: str, index: int, status: TouchpointStatus) -> None: ...

class MessageDeduplicator(Protocol):
    async def seen(self, provider_message_id: str) -> bool: ...
    async def release(self, provider_message_id: str) -> None: ...

class EventSink(Protocol):
    async def write(self, quote_id: str, event_type: EventType,
                    payload: dict | None = None, at: datetime | None = None) -> None: ...
```

`Touchpoint`, `TouchpointStatus` and `EventType` come from `winch.state` /
`winch.events`. Import them; do not redefine.

Implement in `src/winch/db.py`:

```python
SCHEMA_SQL: str      # full CREATE TABLE IF NOT EXISTS DDL for all tables

async def init_schema(pool: AsyncConnectionPool) -> None:
    """Apply SCHEMA_SQL. Idempotent — safe to run on every boot."""

async def make_pool(dsn: str, min_size: int = 1, max_size: int = 5) -> AsyncConnectionPool:
    """Open a psycopg_pool AsyncConnectionPool and wait until it is usable."""
```

Implement in `src/winch/repository.py`:

```python
class PostgresTouchpointQueue:      # satisfies TouchpointQueue
    def __init__(self, pool: AsyncConnectionPool) -> None: ...

class PostgresDeduplicator:         # satisfies MessageDeduplicator
    def __init__(self, pool: AsyncConnectionPool) -> None: ...

class PostgresEventSink:            # satisfies EventSink
    def __init__(self, pool: AsyncConnectionPool) -> None: ...
```

## Schema requirements
- `touchpoints`: primary key `(quote_id, touchpoint_index)`; columns for
  `template_name`, `due_at timestamptz`, `status text`, `claimed_at timestamptz`,
  `provider_message_id text`. Index on `(status, due_at)`.
- `processed_messages`: `provider_message_id text primary key`, `seen_at timestamptz`.
  `seen` is an `INSERT ... ON CONFLICT DO NOTHING` returning whether a row was
  inserted — that is the atomic claim. Do NOT do a SELECT then an INSERT.
- `events`: `id bigserial primary key`, `quote_id text`, `event_type text`,
  `payload jsonb`, `at timestamptz not null default now()`. Index on
  `(quote_id, at)`.
- All timestamps `timestamptz`, stored UTC.

## Required behaviour
1. `claim_due` returns only `PENDING` touchpoints with `due_at <= now`, marks
   them `AWAITING_GATE` and sets `claimed_at`, all in one transaction using
   `FOR UPDATE SKIP LOCKED`.
2. `schedule` is idempotent: calling it twice with the same touchpoints must not
   duplicate rows and must not reset the status of a row already progressed.
   Use `ON CONFLICT (quote_id, touchpoint_index) DO NOTHING`.
3. `seen` returns `True` only when the id was already present.
4. `release` deletes the row so a redelivery is treated as new.
5. No connection string, password or DSN may appear in any log line or exception.

## Files you may edit
- `src/winch/db.py`         (new)
- `src/winch/repository.py` (new)
- `tests/test_repository.py`(new)

## Files you must NOT touch
Everything else. In particular `src/winch/protocols.py`, `src/winch/state.py`,
`src/winch/events.py`, `src/winch/guards.py`, `src/winch/webhook.py`,
`src/winch/media.py`, `src/winch/llm/`, and any existing test file.

## Tests you must write
In `tests/test_repository.py`, against the real Postgres at:

```
postgresql://postgres:winchtest@localhost:55432/winch_test
```

Read it from env var `TEST_DATABASE_URL` with that value as the default. Each
test must create and drop its own data so the suite is re-runnable. Cover:
- `init_schema` is idempotent (run it twice)
- `schedule` then `claim_due` returns the due touchpoint
- a touchpoint due in the future is NOT returned
- **concurrency: two `claim_due` calls running concurrently via `asyncio.gather`
  never return the same touchpoint.** This is the most important test in the file.
- `schedule` called twice does not duplicate rows
- `schedule` does not reset a row already marked SENT
- `mark` records a terminal status
- `seen` returns False then True for the same id
- `release` makes a previously-seen id unseen again
- `EventSink.write` persists and is readable back, including jsonb payload
- `limit` on `claim_due` is respected

## Definition of done
```
cd /home/zaibaki/github_projects/wt-persistence
PYTHONPATH=src /home/zaibaki/github_projects/Winch/.venv/bin/python -m pytest tests/test_repository.py -q
```
must exit 0.

## Forbidden
- New dependencies. psycopg[binary,pool], pydantic, pytest, pytest-asyncio are
  available; nothing else.
- Any ORM. Write SQL.
- A SELECT-then-UPDATE claim. It races.
- Changing, weakening or deleting any assertion to make a test pass.
- Editing files not listed above, for any reason.
