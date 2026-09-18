"""Touchpoint sequence builder and queue selector.

Send timing is deterministic arithmetic over business hours, never an LLM decision.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from winch.guards import next_business_window
from winch.state import Touchpoint, TouchpointStatus

DEFAULT_CADENCE_DAYS: tuple[int, int, int] = (2, 5, 9)

TEMPLATE_SEQUENCE: tuple[str, str, str] = (
    "checkin_soft",
    "schedule_nudge",
    "soft_close",
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
    if len(cadence_days) != len(TEMPLATE_SEQUENCE):
        raise ValueError(
            f"cadence_days length ({len(cadence_days)}) does not match "
            f"TEMPLATE_SEQUENCE length ({len(TEMPLATE_SEQUENCE)})"
        )

    if any(cadence_days[i] >= cadence_days[i + 1] for i in range(len(cadence_days) - 1)):
        raise ValueError("cadence_days must be strictly increasing")

    if approved_at.tzinfo is None:
        approved_at = approved_at.replace(tzinfo=ZoneInfo("UTC"))

    touchpoints: list[Touchpoint] = []
    for idx, (days, template) in enumerate(zip(cadence_days, TEMPLATE_SEQUENCE)):
        target_instant = approved_at + timedelta(days=days)
        due_at = next_business_window(target_instant, timezone_name)
        touchpoints.append(
            Touchpoint(
                index=idx,
                template_name=template,
                due_at=due_at,
                status=TouchpointStatus.PENDING,
            )
        )

    return touchpoints


def next_pending(touchpoints: list[Touchpoint], now: datetime) -> Touchpoint | None:
    """The earliest PENDING touchpoint due at or before `now`, else None."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))

    pending_due = [
        tp
        for tp in touchpoints
        if tp.status == TouchpointStatus.PENDING and tp.due_at <= now
    ]
    if not pending_due:
        return None

    return min(pending_due, key=lambda tp: (tp.due_at, tp.index))
