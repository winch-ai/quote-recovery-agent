# Task: thread-lookup

## Goal
Persist the mapping from a WhatsApp number to the quote thread it belongs to, so
an inbound message can be routed to the right graph thread.

## Context you need
`src/winch/app.py` currently raises `NotImplementedError` in `_pending_thread`
and `_thread_for_customer`. Without this, an inbound customer reply cannot be
matched to the quote it is about, and a contractor's "yes" cannot be matched to
the interrupt it is answering.

Two lookups are needed and they are different:
- **customer -> thread**: which quote is this number the customer of? The most
  recent non-closed one wins, because the same customer may be quoted twice.
- **contractor -> thread**: which thread is currently waiting on an answer? A
  contractor has exactly one pending interrupt at a time in v1; if there were
  several, the newest wins and the others stay parked.

## Contract — DO NOT CHANGE ANY OF THIS

Add to `src/winch/repository.py` — **append, do not restructure existing code**:

```python
class PostgresThreadIndex:
    def __init__(self, pool: AsyncConnectionPool) -> None: ...

    async def bind_customer(self, quote_id: str, customer_wa_id: str) -> None:
        """Record which number is the customer on this quote. Idempotent."""

    async def mark_awaiting(self, quote_id: str, awaiting: bool) -> None:
        """Flag/unflag this quote as waiting on a contractor answer."""

    async def close(self, quote_id: str) -> None:
        """Mark the thread closed so it stops matching lookups."""

    async def thread_for_customer(self, customer_wa_id: str) -> str | None:
        """Most recent non-closed quote for that customer number, else None."""

    async def pending_thread(self) -> str | None:
        """Most recently flagged awaiting, non-closed thread, else None."""
```

Add the table to `SCHEMA_SQL` in `src/winch/db.py`:
- `quote_threads`: `quote_id text primary key`, `customer_wa_id text`,
  `awaiting boolean not null default false`, `closed boolean not null default false`,
  `updated_at timestamptz not null default now()`.
- Index on `(customer_wa_id, closed)` and on `(awaiting, closed, updated_at desc)`.

`SCHEMA_SQL` must stay idempotent — `CREATE TABLE IF NOT EXISTS`,
`CREATE INDEX IF NOT EXISTS`.

## Required behaviour
1. `bind_customer` called twice with the same pair must not error or duplicate.
2. `thread_for_customer` ignores closed threads.
3. `pending_thread` ignores closed threads and returns the most recently updated.
4. `updated_at` is refreshed on every mutation so ordering is meaningful.
5. No DSN or password in any log line or exception.

## Files you may edit
- `src/winch/repository.py` (APPEND only)
- `src/winch/db.py`         (add to SCHEMA_SQL only)
- `tests/test_thread_index.py` (new)

## Files you must NOT touch
Everything else, including `app.py`, `graph.py`, `nodes.py`, `supervisor.py`,
and every existing test file — in particular `tests/test_repository.py`.

## Tests you must write
`tests/test_thread_index.py` against Postgres at `TEST_DATABASE_URL`, defaulting
to `postgresql://postgres:winchtest@localhost:55432/winch_test`. Cover:
- `init_schema` still idempotent after the new table (run twice)
- bind then `thread_for_customer` returns the quote id
- binding the same pair twice does not error
- a closed thread is not returned by `thread_for_customer`
- with two quotes for the same customer, the most recent non-closed one wins
- `pending_thread` returns None when nothing is awaiting
- `mark_awaiting(True)` then `pending_thread` returns it
- `mark_awaiting(False)` clears it
- a closed thread is never returned by `pending_thread`
- with two awaiting threads, the most recently updated wins

## Definition of done
```
cd /home/zaibaki/github_projects/wt-thread
PYTHONPATH=src /home/zaibaki/github_projects/Winch/.venv/bin/python -m pytest tests/test_thread_index.py tests/test_repository.py -q
```
must exit 0 — `test_repository.py` is included to prove you did not break it.

## Forbidden
- New dependencies. Write SQL; no ORM.
- Restructuring or reformatting existing code in `repository.py` or `db.py`.
- Changing, weakening or deleting any assertion to make a test pass.
- Editing files not listed above.
