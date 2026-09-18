# Task: scheduler

## Goal
Pure function that turns an approval moment into the three scheduled touchpoints.

## Context you need
Send timing is never an LLM decision — it is arithmetic over business hours, and
it must be deterministic and testable. A quote approved at 22:40 on a Sunday must
not produce a touchpoint that fires at 22:40 on a Tuesday; it lands inside
working hours, because a text at that hour reads as automated and this audience
is acutely sensitive to that.

## Contract — DO NOT CHANGE ANY OF THIS

From `winch.state`: `Touchpoint`, `TouchpointStatus`. From `winch.guards`:
`next_business_window`. Import them; do not redefine or reimplement.
**`next_business_window` already handles weekends and out-of-hours — call it,
do not write your own version.**

Implement in `src/winch/scheduler.py`:

```python
DEFAULT_CADENCE_DAYS: tuple[int, int, int] = (2, 5, 9)

TEMPLATE_SEQUENCE: tuple[str, str, str] = (
    "checkin_soft", "schedule_nudge", "soft_close",
)


def build_sequence(
    approved_at: datetime,
    timezone_name: str,
    cadence_days: tuple[int, ...] = DEFAULT_CADENCE_DAYS,
) -> list[Touchpoint]:
    """Three touchpoints at +2, +5 and +9 days from approval, each shifted into
    the next business window in the contractor's timezone.

    - index is 0-based and matches position in TEMPLATE_SEQUENCE
    - due_at is timezone-aware UTC
    - status is PENDING
    - raises ValueError if cadence_days is not strictly increasing
    - raises ValueError if len(cadence_days) != len(TEMPLATE_SEQUENCE)
    - approved_at without tzinfo is treated as UTC
    """


def next_pending(touchpoints: list[Touchpoint], now: datetime) -> Touchpoint | None:
    """The earliest PENDING touchpoint due at or before `now`, else None."""
```

## Files you may edit
- `src/winch/scheduler.py`  (new)
- `tests/test_scheduler.py` (new)

## Files you must NOT touch
Everything else, in particular `guards.py` (call it, never modify it),
`state.py`, `protocols.py`, and every existing test file.

## Tests you must write
- three touchpoints, indices 0/1/2, templates in order
- all `due_at` are timezone-aware and in UTC
- all statuses PENDING
- due times are strictly increasing
- **a Sunday 22:40 approval produces touchpoints inside 09:00-17:00 local,
  on weekdays only** — assert the local weekday and hour of each
- the same for `Australia/Sydney`
- a non-increasing cadence raises ValueError
- a cadence of the wrong length raises ValueError
- naive `approved_at` is treated as UTC rather than raising
- `next_pending` returns the earliest due one
- `next_pending` returns None when none are due yet
- `next_pending` ignores touchpoints that are not PENDING

## Definition of done
```
cd /home/zaibaki/github_projects/wt-scheduler
PYTHONPATH=src /home/zaibaki/github_projects/Winch/.venv/bin/python -m pytest tests/test_scheduler.py tests/test_guards.py -q
```
must exit 0 — `test_guards.py` is included to prove you did not modify guards.

## Forbidden
- New dependencies.
- Reimplementing business-hours logic. Call `next_business_window`.
- Changing, weakening or deleting any assertion to make a test pass.
- Editing files not listed above.
