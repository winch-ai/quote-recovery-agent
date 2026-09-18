"""Spec for the money and timing guards.

These assertions ARE the specification. A worker task may add cases; it may never
weaken or delete one. If an implementation cannot satisfy these, the implementation
is wrong, not the test.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from winch.guards import GuardViolation, assert_no_stray_numbers, next_business_window

UTC = ZoneInfo("UTC")


class TestMoneyGuard:
    def test_allows_numbers_the_code_supplied(self):
        assert_no_stray_numbers(
            "Hi Mark, following up on the quote for 2km fencing (GBP 24,504.00).",
            allowed=["24,504.00", "2"],
        )

    def test_rejects_a_price_the_model_invented(self):
        with pytest.raises(GuardViolation):
            assert_no_stray_numbers(
                "I could probably do it for 22,000 if you decide this week.",
                allowed=["24,504.00"],
            )

    def test_rejects_an_invented_date(self):
        with pytest.raises(GuardViolation):
            assert_no_stray_numbers("We can start on the 3rd.", allowed=[])

    def test_separators_do_not_defeat_it(self):
        """24504 and 24,504.00 are the same number; formatting must not smuggle one past."""
        assert_no_stray_numbers("Total 24504", allowed=["24,504.00"])

    def test_empty_allowlist_rejects_any_number(self):
        with pytest.raises(GuardViolation):
            assert_no_stray_numbers("about 5 weeks", allowed=[])

    def test_text_with_no_numbers_passes(self):
        assert_no_stray_numbers("Any questions before we lock in the crew?", allowed=[])


class TestBusinessWindow:
    def test_inside_hours_is_unchanged(self):
        dt = datetime(2026, 9, 16, 11, 0, tzinfo=UTC)  # Wednesday 11:00 London
        assert next_business_window(dt, "Europe/London") == dt

    def test_before_opening_moves_to_nine(self):
        dt = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)
        out = next_business_window(dt, "Europe/London").astimezone(ZoneInfo("Europe/London"))
        assert (out.hour, out.minute) == (9, 0)
        assert out.date() == dt.date()

    def test_after_close_moves_to_next_morning(self):
        dt = datetime(2026, 9, 16, 21, 0, tzinfo=UTC)
        out = next_business_window(dt, "Europe/London").astimezone(ZoneInfo("Europe/London"))
        assert (out.hour, out.weekday()) == (9, 3)  # Thursday

    def test_saturday_moves_to_monday(self):
        dt = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)  # Saturday
        out = next_business_window(dt, "Europe/London").astimezone(ZoneInfo("Europe/London"))
        assert out.weekday() == 0

    def test_sunday_night_moves_to_monday_morning(self):
        """The Sunday-night quote is the canonical case for this product."""
        dt = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)  # Sunday
        out = next_business_window(dt, "Europe/London").astimezone(ZoneInfo("Europe/London"))
        assert (out.weekday(), out.hour) == (0, 9)

    def test_respects_australian_timezone(self):
        dt = datetime(2026, 9, 16, 23, 0, tzinfo=UTC)  # 09:00 Thu in Sydney
        out = next_business_window(dt, "Australia/Sydney").astimezone(ZoneInfo("Australia/Sydney"))
        assert (out.hour, out.weekday()) == (9, 3)

    def test_returns_utc(self):
        dt = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)
        assert next_business_window(dt, "Europe/London").tzinfo == ZoneInfo("UTC")
