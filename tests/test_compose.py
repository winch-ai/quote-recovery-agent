"""Pure unit tests for winch.compose: template variable filling and reply guards."""
from __future__ import annotations

import pytest

from winch.compose import (
    ContractorProfile,
    build_template_variables,
    format_money,
    guard_freeform,
)
from winch.guards import GuardViolation
from winch.state import Quote


def _make_quote(
    customer_name: str = "Mark Henderson",
    project_title: str = "Stock fencing",
    quote_total: float = 24504.0,
    currency: str = "GBP",
    expiry_date: str | None = "2026-10-11",
) -> Quote:
    return Quote(
        quote_id="quote_001",
        customer_name=customer_name,
        customer_phone="07700 900412",
        customer_email="mark@example.com",
        project_title=project_title,
        scope_summary="Supply and erect 2km stock fencing",
        quote_total=quote_total,
        currency=currency,
        expiry_date=expiry_date,
    )


def _make_contractor(
    first_name: str = "Dave",
    business_name: str = "Dave Fencing Ltd",
) -> ContractorProfile:
    return ContractorProfile(
        contractor_id="contractor_001",
        first_name=first_name,
        business_name=business_name,
        wa_id="447700900000",
        timezone="Europe/London",
    )


class TestComposeTemplates:
    def test_each_template_name_returns_right_number_of_variables_in_order(self):
        """each template name returns the right number of variables in the right order."""
        quote = _make_quote()
        contractor = _make_contractor()

        # checkin_soft -> 5 variables
        checkin_vars = build_template_variables(quote, "checkin_soft", contractor)
        assert len(checkin_vars) == 5
        assert checkin_vars == [
            "Mark",
            "Dave",
            "Dave Fencing Ltd",
            "Stock fencing",
            "GBP 24,504.00",
        ]

        # schedule_nudge -> 4 variables
        schedule_vars = build_template_variables(quote, "schedule_nudge", contractor)
        assert len(schedule_vars) == 4
        assert schedule_vars == [
            "Mark",
            "Dave",
            "Dave Fencing Ltd",
            "Stock fencing",
        ]

        # soft_close -> 4 variables
        soft_close_vars = build_template_variables(quote, "soft_close", contractor)
        assert len(soft_close_vars) == 4
        assert soft_close_vars == [
            "Mark",
            "Dave",
            "Dave Fencing Ltd",
            "Stock fencing",
        ]

    def test_customer_first_name_extracted_from_full_name(self):
        """customer first name is extracted from a full name."""
        contractor = _make_contractor()
        quote = _make_quote(customer_name="Alice Middle Last")
        vars = build_template_variables(quote, "schedule_nudge", contractor)
        assert vars[0] == "Alice"

    def test_single_word_customer_name_still_works(self):
        """a single-word customer name still works."""
        contractor = _make_contractor()
        quote = _make_quote(customer_name="Madonna")
        vars = build_template_variables(quote, "schedule_nudge", contractor)
        assert vars[0] == "Madonna"

    def test_unknown_template_name_raises_value_error(self):
        """unknown template name raises ValueError."""
        quote = _make_quote()
        contractor = _make_contractor()
        with pytest.raises(ValueError, match="Unknown template"):
            build_template_variables(quote, "nonexistent_template", contractor)


class TestFormatMoney:
    def test_format_money_renders_gbp_with_thousands_separator(self):
        """format_money renders 24504.0/GBP as 'GBP 24,504.00'."""
        assert format_money(24504.0, "GBP") == "GBP 24,504.00"

    def test_format_money_handles_whole_number_and_value_under_1000(self):
        """format_money handles a whole number and a value under 1000."""
        assert format_money(500.0, "GBP") == "GBP 500.00"
        assert format_money(500, "GBP") == "GBP 500.00"
        assert format_money(42.5, "EUR") == "EUR 42.50"
        assert format_money(0.0, "USD") == "USD 0.00"
        assert format_money(999, "GBP") == "GBP 999.00"


class TestGuardFreeform:
    def test_guard_freeform_passes_text_containing_quote_total(self):
        """guard_freeform passes text containing the quote total."""
        quote = _make_quote(quote_total=24504.0, currency="GBP")
        guard_freeform("Following up on quote total GBP 24,504.00 for your review.", quote)
        guard_freeform("Total is 24,504.00.", quote)

    def test_guard_freeform_raises_guard_violation_on_invented_price(self):
        """guard_freeform raises GuardViolation on an invented price."""
        quote = _make_quote(quote_total=24504.0, currency="GBP")
        with pytest.raises(GuardViolation):
            guard_freeform("I could do it for 22,000 if we lock it in now.", quote)

    def test_guard_freeform_raises_on_invented_date(self):
        """guard_freeform raises on an invented date."""
        quote = _make_quote(expiry_date="2026-10-11")
        with pytest.raises(GuardViolation):
            guard_freeform("We could schedule the job starting on the 3rd.", quote)

    def test_variables_built_for_checkin_soft_pass_guard_freeform_when_joined(self):
        """variables built for checkin_soft pass guard_freeform when joined."""
        quote = _make_quote(
            customer_name="Mark Henderson",
            project_title="Stock fencing",
            quote_total=24504.0,
            currency="GBP",
        )
        contractor = _make_contractor(
            first_name="Dave",
            business_name="Dave Fencing Ltd",
        )
        variables = build_template_variables(quote, "checkin_soft", contractor)
        joined = " ".join(variables)
        # Should not raise any GuardViolation
        guard_freeform(joined, quote)
