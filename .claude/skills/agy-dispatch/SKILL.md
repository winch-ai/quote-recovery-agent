---
name: agy-dispatch
description: Dispatch a coding task to an agy worker (Gemini 3.8 Flash) in an isolated git worktree. Use when delegating implementation work — writing a task brief, launching parallel workers, or checking on running ones. Enforces the closed-brief format and the disjoint-allowlist parallelism rule.
---

# Dispatching work to agy

`agy` runs Gemini 3.8 Flash. It is fast, literal and cheap, and it does not plan well. It will do
exactly what the brief says and nothing more — which is an asset if the brief is closed, and a
liability if it is not.

## Before dispatching — the closed-brief test

Refuse to dispatch unless all five hold. If any fails, the brief is not ready and the work will come
back wrong in a way that is expensive to detect:

1. **Contract is pasted, not described.** Actual signatures/schemas in the brief, not "implement a
   function that takes a quote".
2. **Allowlist is explicit.** Named files. Not "the extractor module".
3. **Done is a command.** Something that exits 0. Not "the tests pass" — *which* command.
4. **Forbidden list present**, including "do not change test assertions to make them pass".
5. **No design decisions remain.** If the brief contains a choice the worker has to make, make it
   yourself first and put the answer in the contract.

## Always include an execution-order instruction

A brief whose task involves slow commands needs an explicit
**"write all files before running anything"** line. Observed twice: the worker
spends its single turn launching a verification command, goes idle, and the
harness terminates it — `status: SUCCESS`, `num_turns: 1`, zero files written.

Name the slow command and its duration in the brief so the worker does not
front-load it.

## Launch

```bash
TASK=extractor-pdf
git worktree add ../wt-$TASK -b task/$TASK
cd ../wt-$TASK
agy -p "$(cat briefs/$TASK.md)" \
    --model gemini-3.8-flash-medium \
    --output-format json \
    --sandbox \
    > .agy/runs/$TASK.json 2>&1
```

Run in the background (`run_in_background: true`) so several proceed at once. Never pass `--effort`
— it is already in the model id. Never use `--dangerously-skip-permissions` outside a worktree.

## Parallelism rule

Two tasks may run concurrently **only if their file allowlists are disjoint.** Check this before
launching, not after. Overlapping allowlists get serialised.

This is the entire regression defence: concurrent workers cannot overwrite each other's work if no
two of them can write the same file.

## Cost

~16k input tokens of fixed overhead per call. Prefer one brief covering three related functions over
three briefs covering one each. Batch aggressively.

## When it comes back

Do not read the diff for correctness first. Run the audit in the `agy-audit` skill. The first check
there — files touched vs. allowlist — invalidates everything else if it fails.
