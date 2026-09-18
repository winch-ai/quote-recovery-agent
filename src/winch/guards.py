"""The three guards. CONTRACT FILE — not delegated, and not modifiable by a worker task.

These exist because the failure modes they prevent are not recoverable:
a hallucinated price sent to a customer is the contractor's legal exposure, and a
double-sent follow-up is their reputation. Both are silent failures without these.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from winch.state import Quote

# Any run of digits, with optional separators, that could read as money or a date.
_DIGIT_RUN = re.compile(r"\d[\d,.\s]*\d|\d")

BUSINESS_START = time(9, 0)
BUSINESS_END = time(17, 0)


class GuardViolation(Exception):
    """Raised when a guard rejects. Never caught and continued past — it means a
    message was about to reach a customer that should not have."""


def freeze_quote(quote: Quote) -> Quote:
    """Mark a quote immutable at the point of contractor approval."""
    return quote.model_copy(update={"frozen": True})


def _normalise(token: str) -> str:
    """Canonicalise a digit run so formatting cannot smuggle a number past.

    '24,504.00', '24504.00' and '24504' must all collapse to the same key, or a
    model could restate the total in a different format and slip an edit through.
    """
    cleaned = re.sub(r"[,\s]", "", token).rstrip(".")
    try:
        value = float(cleaned)
    except ValueError:
        return cleaned
    return str(int(value)) if value.is_integer() else repr(value)


def assert_no_stray_numbers(text: str, allowed: list[str]) -> None:
    """Reject any composed message containing a number the code did not put there.

    The Composer returns text with placeholders; code substitutes the frozen
    literals; this then runs on the result. A model that invents '£26,000' or
    'starting on the 3rd' cannot get past this, because the only digit runs
    permitted are the ones explicitly passed in `allowed`.

    Raises GuardViolation on the first unexpected number.
    """
    permitted = {_normalise(a) for a in allowed}
    for match in _DIGIT_RUN.finditer(text):
        token = _normalise(match.group())
        if token and token not in permitted:
            raise GuardViolation(
                f"unexpected number {match.group()!r} in composed message; "
                f"permitted: {sorted(permitted)}"
            )


def next_business_window(dt: datetime, tz: str) -> datetime:
    """Shift an instant into the next 09:00-17:00 local weekday slot.

    Deterministic and model-free by design: send timing is never an LLM decision.
    Returns UTC.
    """
    zone = ZoneInfo(tz)
    local = dt.astimezone(zone)

    while True:
        if local.weekday() >= 5:                      # Sat/Sun
            local = datetime.combine(local.date() + timedelta(days=1), BUSINESS_START, tzinfo=zone)
            continue
        if local.time() < BUSINESS_START:
            local = datetime.combine(local.date(), BUSINESS_START, tzinfo=zone)
            continue
        if local.time() >= BUSINESS_END:
            local = datetime.combine(local.date() + timedelta(days=1), BUSINESS_START, tzinfo=zone)
            continue
        return local.astimezone(ZoneInfo("UTC"))
