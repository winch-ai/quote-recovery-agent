# Delegated build workflow

How this repo is built: **Claude owns the design and the audit; `agy` (Gemini 3.8 Flash, medium)
writes implementations against contracts it is not allowed to change.**

The model is fast, literal and cheap. Given a closed problem with a verifiable exit condition it is
excellent. Given an open one it invents — plausible-looking code that satisfies the sentence but not
the system. The entire workflow below exists to keep every task closed.

## 1. Division of labour

**Never delegated.** These are where a wrong answer is expensive and a review would not catch it:

- Architecture, module boundaries, and the graph topology
- State schema, DB schema, migrations
- Every interface / Protocol signature
- The three guards: immutable money, deterministic timing, idempotency
- Security boundaries and secret handling
- Test *specifications* — Claude writes the test names and the assertions as a spec; `agy` fills bodies
- The merge audit

**Delegated.** Closed problems with a mechanical exit condition:

- Function bodies against a fixed signature
- Test bodies against a written spec
- Adapters, serialisation, boilerplate
- Docstrings and type annotations

## 2. Task brief format

A brief that omits any section below is not ready to dispatch. Vagueness is where this model invents.

```markdown
# Task: <id>

## Goal
<one sentence, one outcome>

## Contract            # exact signatures/schemas. agy MUST NOT change these.
<paste the actual code>

## Files you may edit  # explicit allowlist. Anything else is a rejection.
- src/winch/x.py

## Files you must not touch
- everything else, in particular src/winch/state.py and src/winch/guards.py

## Definition of done
<a command that exits 0, e.g. `pytest tests/test_x.py -q`>

## Forbidden
- New dependencies
- Refactoring or renaming anything outside the allowlist
- Changing test assertions to make them pass
- Touching files not listed above
```

The last bullet under Forbidden matters more than it looks. A fast model that cannot make a test
pass will often "fix" the test.

## 3. Invocation

Run from inside the task's worktree:

```bash
agy -p "$(cat briefs/<task-id>.md)" \
    --model gemini-3.8-flash-medium \
    --output-format json \
    --sandbox \
    > ../../.agy/runs/<task-id>.json 2> ../../.agy/runs/<task-id>.stderr
```

**Keep the streams separate.** `agy` writes progress chatter to stderr; merging
them with `2>&1` corrupts the JSON run record that cost tracing depends on.

- `--model gemini-3.8-flash-medium` — effort is baked into the model id; do not also pass `--effort`.
- `--sandbox` preferred. Use `--dangerously-skip-permissions` **only** inside an isolated worktree
  that will be audited before merge, never in the main checkout.
- `--output-format json` is required — it carries the usage block used for cost tracing.

**Cost note:** every call carries roughly **16k input tokens of fixed overhead** before your prompt.
Batch related work into one larger brief rather than several small ones. Ten small tasks cost 160k
tokens of overhead alone.

## 4. Parallelism

One worktree per task, one `agy` per worktree, launched in the background:

```bash
git worktree add ../wt-<task-id> -b task/<task-id>
```

**The parallelism rule:** two tasks may run concurrently **only if their file allowlists are
disjoint.** This is the regression guard — it is not possible for concurrent workers to overwrite
each other's functionality if no two of them can write the same file. Overlapping allowlists are
serialised, no exceptions.

## 5. The worker's own status is not evidence

`agy` reports `status: SUCCESS` based on its own turn completing, not on the
brief's definition of done. Observed in practice: a run returned SUCCESS with a
final message of *"I have launched the pytest suite and am waiting for execution
to complete"* — it never saw the result, and the suite had a failure in it.

**Always run the definition-of-done command yourself.** Treat the status field
as "the process exited", nothing more.

## 6. The audit gate

No branch merges without passing the audit in `.claude/skills/agy-audit/SKILL.md`. Run it on every
returned branch. It is a gate, not a review — findings block the merge.

The single highest-yield check: `git diff --stat main..task/<id>` against the brief's allowlist. A
file outside the allowlist is an automatic reject regardless of whether the code is good, because it
means the worker exceeded its brief and nothing else it did can be trusted to be in scope.

## 7. Cost tracing

Every run writes `.agy/runs/<task-id>.json` containing the `usage` block. Aggregate with:

```bash
python scripts/agy_ledger.py
```

Track cost per *merged* task, not per run. A cheap run that gets rejected and redone twice is the
expensive one.

## 8. Merge

1. Audit passes.
2. Rebase onto `main`.
3. Run the **full** suite on the merge result, not on the branch — a branch that passed in isolation
   can still break something it never touched.
4. Merge, delete the worktree and branch.
