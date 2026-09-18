"""Template variable filling and reply guards for quote follow-ups."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from winch.guards import GuardViolation, assert_no_stray_numbers
from winch.state import Intent, Quote


class ContractorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contractor_id: str
    first_name: str
    business_name: str
    wa_id: str
    timezone: str = "Europe/London"


def format_money(amount: float, currency: str) -> str:
    """'GBP 24,504.00' style. Two decimals, thousands separators, no symbol
    guessing — the currency code prefixes it."""
    return f"{currency} {amount:,.2f}"


def build_template_variables(
    quote: Quote, template_name: str, contractor: ContractorProfile
) -> list[str]:
    """Ordered variables for an approved template. Pure — no model, no I/O.

    checkin_soft   -> [customer first name, contractor first name,
                       business name, project title, formatted total]
    schedule_nudge -> [customer first name, contractor first name,
                       business name, project title]
    soft_close     -> [customer first name, contractor first name,
                       business name, project title]

    Raises ValueError on an unknown template name. Customer first name is the
    first whitespace-separated token of quote.customer_name.
    """
    tokens = quote.customer_name.split()
    customer_first_name = tokens[0] if tokens else ""

    if template_name == "checkin_soft":
        return [
            customer_first_name,
            contractor.first_name,
            contractor.business_name,
            quote.project_title,
            format_money(quote.quote_total, quote.currency),
        ]
    elif template_name in ("schedule_nudge", "soft_close"):
        return [
            customer_first_name,
            contractor.first_name,
            contractor.business_name,
            quote.project_title,
        ]
    else:
        raise ValueError(f"Unknown template name: {template_name!r}")


def guard_freeform(body: str, quote: Quote) -> None:
    """Run assert_no_stray_numbers over a model-drafted reply.

    The allowlist is exactly the numbers the system legitimately knows: the
    formatted total, the raw total, and the year/month/day parts of
    quote.expiry_date if set. Anything else the model produced is a fabrication
    and must raise GuardViolation.
    """
    allowed: list[str] = [
        format_money(quote.quote_total, quote.currency),
        f"{quote.quote_total:,.2f}",
        str(quote.quote_total),
    ]
    if quote.expiry_date:
        parts = re.findall(r"\d+", quote.expiry_date)
        if len(parts) >= 3:
            allowed.extend(parts[:3])
        else:
            allowed.extend(parts)

    assert_no_stray_numbers(body, allowed)
