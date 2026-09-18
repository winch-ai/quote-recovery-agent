#!/usr/bin/env python3
"""Aggregate agy run costs from .agy/runs/*.json.

Track cost per *merged* task, not per run: a cheap run rejected and redone twice
is the expensive one. Rejected runs are recorded here too, so the real number is
visible rather than the optimistic one.

    python scripts/agy_ledger.py
"""
import json
from pathlib import Path

RUNS = Path(__file__).resolve().parent.parent / ".agy" / "runs"


def main():
    if not RUNS.exists() or not any(RUNS.glob("*.json")):
        print("no runs recorded yet")
        return

    rows, totals = [], {"input": 0, "output": 0, "thinking": 0, "cached": 0, "secs": 0.0}
    for path in sorted(RUNS.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"  UNPARSEABLE  {path.name}  (agy likely errored - check the file)")
            continue
        usage = data.get("usage", {})
        rows.append((path.stem, data.get("status", "?"), usage.get("input_tokens", 0),
                     usage.get("output_tokens", 0), usage.get("thinking_tokens", 0),
                     data.get("duration_seconds", 0.0)))
        totals["input"] += usage.get("input_tokens", 0)
        totals["output"] += usage.get("output_tokens", 0)
        totals["thinking"] += usage.get("thinking_tokens", 0)
        totals["cached"] += usage.get("cache_read_tokens", 0)
        totals["secs"] += data.get("duration_seconds", 0.0)

    print(f"{'task':<28} {'status':<10} {'in':>9} {'out':>8} {'think':>8} {'secs':>7}")
    print("-" * 74)
    for name, status, tin, tout, think, secs in rows:
        print(f"{name:<28} {status:<10} {tin:>9,} {tout:>8,} {think:>8,} {secs:>7.1f}")
    print("-" * 74)
    print(f"{'TOTAL':<28} {len(rows):<10} {totals['input']:>9,} {totals['output']:>8,} "
          f"{totals['thinking']:>8,} {totals['secs']:>7.1f}")
    print(f"\ncached reads: {totals['cached']:,}")
    print(f"fixed overhead is ~16k input tokens per call: {len(rows)} calls "
          f"= ~{len(rows) * 16000:,} tokens before any prompt content")


if __name__ == "__main__":
    main()
