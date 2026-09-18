"""Tests for winch.scheduler."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from winch.scheduler import (
    DEFAULT_CADENCE_DAYS,
    TEMPLATE_SEQUENCE,
    build_sequence,
    next_pending,
)
from winch.state import Touchpoint, TouchpointStatus

UTC = ZoneInfo("UTC")


class TestBuildSequence:
    def test_three_touchpoints_indices_and_templates_in_order(self):
        approved_at = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        touchpoints = build_sequence(approved_at, "Europe/London")

        assert len(touchpoints) == 3
        assert [tp.index for tp in touchpoints] == [0, 1, 2]
        assert [tp.template_name for tp in touchpoints] == list(TEMPLATE_SEQUENCE)
        assert [tp.template_name for tp in touchpoints] == [
            "checkin_soft",
            "schedule_nudge",
            "soft_close",
        ]

    def test_all_due_at_are_timezone_aware_utc(self):
        approved_at = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        touchpoints = build_sequence(approved_at, "Europe/London")

        for tp in touchpoints:
            assert tp.due_at.tzinfo is not None
            assert tp.due_at.tzinfo == UTC

    def test_all_statuses_pending(self):
        approved_at = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        touchpoints = build_sequence(approved_at, "Europe/London")

        for tp in touchpoints:
            assert tp.status is TouchpointStatus.PENDING

    def test_due_times_are_strictly_increasing(self):
        approved_at = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        touchpoints = build_sequence(approved_at, "Europe/London")

        assert touchpoints[0].due_at < touchpoints[1].due_at < touchpoints[2].due_at

    def test_sunday_night_approval_inside_local_business_hours(self):
        # 2026-09-20 is a Sunday
        london_tz = ZoneInfo("Europe/London")
        approved_at = datetime(2026, 9, 20, 22, 40, tzinfo=london_tz)
        assert approved_at.weekday() == 6  # Sunday

        touchpoints = build_sequence(approved_at, "Europe/London")
        assert len(touchpoints) == 3

        local_tps = [tp.due_at.astimezone(london_tz) for tp in touchpoints]

        # Assert every touchpoint is within 09:00-17:00 on weekdays only
        for local_dt in local_tps:
            assert local_dt.weekday() < 5, f"Expected weekday, got {local_dt.weekday()}"
            assert 9 <= local_dt.hour < 17, f"Expected hour inside 9-17, got {local_dt.hour}"

        # Touchpoint 0 (+2d -> Tue 22:40 -> Wed 09:00)
        assert (local_tps[0].weekday(), local_tps[0].hour) == (2, 9)
        # Touchpoint 1 (+5d -> Fri 22:40 -> Mon 09:00)
        assert (local_tps[1].weekday(), local_tps[1].hour) == (0, 9)
        # Touchpoint 2 (+9d -> Tue 22:40 -> Wed 09:00)
        assert (local_tps[2].weekday(), local_tps[2].hour) == (2, 9)

    def test_sunday_night_approval_sydney(self):
        sydney_tz = ZoneInfo("Australia/Sydney")
        approved_at = datetime(2026, 9, 20, 22, 40, tzinfo=sydney_tz)
        assert approved_at.weekday() == 6  # Sunday

        touchpoints = build_sequence(approved_at, "Australia/Sydney")
        assert len(touchpoints) == 3

        local_tps = [tp.due_at.astimezone(sydney_tz) for tp in touchpoints]

        for local_dt in local_tps:
            assert local_dt.weekday() < 5, f"Expected weekday, got {local_dt.weekday()}"
            assert 9 <= local_dt.hour < 17, f"Expected hour inside 9-17, got {local_dt.hour}"

        # Touchpoint 0 (+2d -> Wed 09:00 Sydney)
        assert (local_tps[0].weekday(), local_tps[0].hour) == (2, 9)
        # Touchpoint 1 (+5d -> Mon 09:00 Sydney)
        assert (local_tps[1].weekday(), local_tps[1].hour) == (0, 9)
        # Touchpoint 2 (+9d -> Wed 09:00 Sydney)
        assert (local_tps[2].weekday(), local_tps[2].hour) == (2, 9)

    @pytest.mark.parametrize(
        "bad_cadence",
        [
            (2, 2, 5),
            (5, 2, 9),
            (9, 5, 2),
            (3, 3, 3),
            (5, 5, 2),
        ],
    )
    def test_non_increasing_cadence_raises_value_error(self, bad_cadence):
        approved_at = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="strictly increasing"):
            build_sequence(approved_at, "Europe/London", cadence_days=bad_cadence)

    @pytest.mark.parametrize(
        "wrong_length_cadence",
        [
            (),
            (2,),
            (2, 5),
            (2, 5, 9, 14),
        ],
    )
    def test_cadence_of_wrong_length_raises_value_error(self, wrong_length_cadence):
        approved_at = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        with pytest.raises(ValueError):
            build_sequence(approved_at, "Europe/London", cadence_days=wrong_length_cadence)

    def test_naive_approved_at_treated_as_utc(self):
        naive_approved = datetime(2026, 9, 20, 22, 40)
        aware_utc_approved = datetime(2026, 9, 20, 22, 40, tzinfo=UTC)

        tps_from_naive = build_sequence(naive_approved, "Europe/London")
        tps_from_aware = build_sequence(aware_utc_approved, "Europe/London")

        assert len(tps_from_naive) == len(tps_from_aware)
        for tp_naive, tp_aware in zip(tps_from_naive, tps_from_aware):
            assert tp_naive.due_at.tzinfo == UTC
            assert tp_naive.due_at == tp_aware.due_at


class TestNextPending:
    def test_returns_earliest_due_touchpoint(self):
        base_time = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
        tp0 = Touchpoint(
            index=0,
            template_name="checkin_soft",
            due_at=base_time,
            status=TouchpointStatus.PENDING,
        )
        tp1 = Touchpoint(
            index=1,
            template_name="schedule_nudge",
            due_at=base_time + timedelta(days=3),
            status=TouchpointStatus.PENDING,
        )
        tp2 = Touchpoint(
            index=2,
            template_name="soft_close",
            due_at=base_time + timedelta(days=7),
            status=TouchpointStatus.PENDING,
        )

        # Both tp0 and tp1 are due at or before now; tp0 is earlier
        now = base_time + timedelta(days=4)
        assert next_pending([tp0, tp1, tp2], now) == tp0

        # Should also work if touchpoints are given in reverse order
        assert next_pending([tp2, tp1, tp0], now) == tp0

    def test_returns_none_when_none_are_due_yet(self):
        base_time = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
        tp0 = Touchpoint(
            index=0,
            template_name="checkin_soft",
            due_at=base_time + timedelta(days=1),
            status=TouchpointStatus.PENDING,
        )
        tp1 = Touchpoint(
            index=1,
            template_name="schedule_nudge",
            due_at=base_time + timedelta(days=3),
            status=TouchpointStatus.PENDING,
        )

        now = base_time
        assert next_pending([tp0, tp1], now) is None
        assert next_pending([], now) is None

    def test_ignores_touchpoints_that_are_not_pending(self):
        base_time = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
        tp0 = Touchpoint(
            index=0,
            template_name="checkin_soft",
            due_at=base_time,
            status=TouchpointStatus.SENT,
        )
        tp1 = Touchpoint(
            index=1,
            template_name="schedule_nudge",
            due_at=base_time + timedelta(days=1),
            status=TouchpointStatus.AWAITING_GATE,
        )
        tp2 = Touchpoint(
            index=2,
            template_name="soft_close",
            due_at=base_time + timedelta(days=2),
            status=TouchpointStatus.PENDING,
        )

        now = base_time + timedelta(days=5)
        # tp0 and tp1 are due, but tp0 is SENT and tp1 is AWAITING_GATE.
        # The earliest PENDING touchpoint is tp2.
        assert next_pending([tp0, tp1, tp2], now) == tp2

        # If tp2 is also not PENDING, next_pending should return None
        tp2_sent = tp2.model_copy(update={"status": TouchpointStatus.SKIPPED})
        assert next_pending([tp0, tp1, tp2_sent], now) is None

    def test_naive_now_handled_as_utc(self):
        base_time = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
        tp = Touchpoint(
            index=0,
            template_name="checkin_soft",
            due_at=base_time,
            status=TouchpointStatus.PENDING,
        )
        naive_now = datetime(2026, 9, 21, 10, 0)
        assert next_pending([tp], naive_now) == tp
