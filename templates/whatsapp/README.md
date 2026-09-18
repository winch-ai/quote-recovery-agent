# WhatsApp template catalogue

Cold touchpoints are business-initiated and fall outside Meta's 24-hour window, so they must be
pre-approved templates. Button labels are frozen at approval time and capped at three — the agent
**selects** a template, it never generates buttons.

Once the customer taps a button or replies, the 24-hour window opens and the agent may send
free-form interactive messages with dynamic buttons.

## Category

All three are submitted as `UTILITY`, not `MARKETING`. Utility requires the message to relate to an
existing transaction, and these all reference a quote the customer asked for. The wording is
deliberately free of promotional language — no offers, no discounts, no "we'd love to work with
you" — because that is what pushes a template into Marketing.

**Meta can override the category.** If one comes back as `MARKETING`, it still works but costs more
and carries stricter rules. Tighten the wording toward the existing job and resubmit before
accepting it.

## Variables

Variables are positional and must be sequential from `{{1}}`. Note that `{{5}}` in `checkin_soft` is
the quote total: it is filled **by code from frozen state**, never by the model. See the immutable
money guard in `docs/DESIGN.md`.

| Template | Variables |
| `checkin_soft` | 1 customer first name · 2 contractor first name · 3 business name · 4 project title · 5 quote total |
| `schedule_nudge` | 1 customer first name · 2 contractor first name · 3 business name · 4 project title |
| `soft_close` | 1 customer first name · 2 contractor first name · 3 business name · 4 project title |

## Submitting

    export WABA_ID=...           # WhatsApp Business Account ID
    export META_ACCESS_TOKEN=... # System user or temporary dev token

    python scripts/submit_templates.py            # submit all
    python scripts/submit_templates.py --status   # poll approval status

Submission does **not** require Meta Business Verification. Verification is only needed to move off
the test number onto a real one and to raise messaging limits.
