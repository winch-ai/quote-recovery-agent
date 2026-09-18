---
name: agy-audit
description: Audit a branch produced by an agy worker before merging. Use whenever delegated work comes back, or before merging any task/* branch. Gate covering scope compliance, the three guards, security, idempotency, observability, test integrity and regression risk.
---

# Auditing delegated work

This is a **gate**, not a review. Findings block the merge. Work through it in order — an early
failure invalidates the later checks, so stop and reject rather than continuing.

## 1. Scope compliance — do this first

```bash
git diff --stat main..task/<id>
```

Compare against the brief's allowlist. **Any file outside it is an automatic reject**, regardless of
code quality. A worker that exceeded its brief cannot be trusted to have stayed in scope anywhere
else, so nothing below is meaningful.

Also check for new dependencies (`git diff main..task/<id> -- pyproject.toml requirements*.txt`).

## 2. Test integrity

The characteristic failure of a fast model that cannot make a test pass is to change the test.

```bash
git diff main..task/<id> -- tests/
```

- Were any **assertions** weakened, deleted, or had expected values edited?
- Any new `pytest.mark.skip`, `xfail`, or `return` early in a test body?
- Do the tests actually exercise the code, or do they assert on mocks all the way down?
- Does a test fail when the implementation is deliberately broken? If a test cannot fail, it is not a
  test.

## 3. The three guards

These are the reason this project has a human in the loop. Check them explicitly every time, even
when the task appears unrelated:

- **Immutable money.** No code path lets a model-generated string reach a customer carrying a
  monetary figure, date or line item. Totals substituted from frozen state by code, after the model
  returns. The digit-allowlist validator still runs and still rejects.
- **Deterministic timing.** No LLM call decides a wait, a cadence or a send time. Schedules are
  absolute timestamps computed in code.
- **Human gate.** No outbound customer message path bypasses the `interrupt()`. Search for any new
  send call and trace it back to a gate.

## 4. Idempotency and reliability

- Inbound webhook handlers dedupe on provider message id **before** mutating state.
- Retries cannot double-send. Any send path is either idempotent or guarded by a claimed row.
- Concurrent workers cannot both claim the same due touchpoint (`FOR UPDATE SKIP LOCKED` or
  equivalent).
- Failure paths write state. A crash between "sent" and "recorded" must not re-send on restart.

## 5. Security

- No secret in source, fixtures, logs or test data. Check the diff for anything token-shaped.
- Webhook signature verification present and **failing closed** — a missing or malformed signature
  must reject, not pass.
- No user- or model-supplied string interpolated into SQL, shell, or a file path.
- Any content from a customer or a PDF is treated as **data, never instructions** — it must not be
  concatenated into a system prompt.
- Outbound requests go to pinned hosts, not to URLs derived from model output.

## 6. Observability

- Every state transition writes an `events` row. A transition that does not is invisible, and the
  event log is the instrument this whole demo exists to produce.
- Errors are logged with the quote id and enough context to reconstruct what happened.
- No `except: pass`. No bare `except Exception` that swallows and continues.
- Log lines carry no customer PII beyond what is needed to debug.

## 7. Regression

```bash
git rebase main && pytest -q      # full suite, on the merge result
```

A branch that passed in isolation can still break something it never touched. The suite must run on
the merge result, not on the branch.

Finally, re-read the brief's Goal sentence and ask whether the diff actually achieves it — a fast
model often satisfies the letter of the contract while missing the point. If it does not, that is a
reject even if every check above passed.
