"""Percentile summary + PASS/FAIL verdict for the stress harness
(``tests/e2e/stress.py``). See ``tests/unit/test_stress_report.py`` for the
pinned contract.
"""

from __future__ import annotations

import math
from typing import Any


def _percentile(sorted_samples: list[float], pct: float) -> float:
    """Nearest-rank percentile — no interpolation, so a round input (1..100)
    lands on an unambiguous value."""
    n = len(sorted_samples)
    if n == 1:
        return sorted_samples[0]
    rank = max(1, math.ceil(pct / 100 * n))
    rank = min(rank, n)
    return sorted_samples[rank - 1]


def summarise(
    steps: dict[str, list[float]],
    *,
    errors: dict[str, int],
    timeouts: dict[str, int],
) -> dict[str, Any]:
    """Compute per-step p50/p95/p99/max/count plus an overall PASS/FAIL
    verdict. FAIL on any recorded error, any recorded timeout, or any step
    with zero samples (a step the run never reached at all)."""
    out_steps: dict[str, dict[str, float | int]] = {}
    verdict = "PASS"

    for name, samples in steps.items():
        if not samples:
            out_steps[name] = {
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "max": 0.0,
                "count": 0,
            }
            verdict = "FAIL"
            continue
        ordered = sorted(samples)
        out_steps[name] = {
            "p50": _percentile(ordered, 50),
            "p95": _percentile(ordered, 95),
            "p99": _percentile(ordered, 99),
            "max": ordered[-1],
            "count": len(ordered),
        }

    total_errors = sum(errors.values())
    total_timeouts = sum(timeouts.values())
    total_samples = sum(len(s) for s in steps.values())

    if total_errors > 0 or total_timeouts > 0:
        verdict = "FAIL"

    return {
        "steps": out_steps,
        "errors": dict(errors),
        "timeouts": dict(timeouts),
        "total_errors": total_errors,
        "total_timeouts": total_timeouts,
        "total_samples": total_samples,
        "verdict": verdict,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    """Render ``summarise``'s output as a Markdown report."""
    lines = [
        "# Stress run report",
        "",
        f"**Verdict: {summary['verdict']}**",
        "",
        f"- total samples: {summary['total_samples']}",
        f"- total errors: {summary['total_errors']}",
        f"- total timeouts: {summary['total_timeouts']}",
        "",
        "| step | count | p50 | p95 | p99 | max |",
        "|---|---|---|---|---|---|",
    ]
    for name, stats in summary["steps"].items():
        lines.append(
            f"| {name} | {stats['count']} | {stats['p50']} | {stats['p95']} | "
            f"{stats['p99']} | {stats['max']} |"
        )
    if summary["errors"] or summary["timeouts"]:
        lines.append("")
        lines.append("## Errors / timeouts")
        for name, count in summary["errors"].items():
            if count:
                lines.append(f"- {name}: {count} error(s)")
        for name, count in summary["timeouts"].items():
            if count:
                lines.append(f"- {name}: {count} timeout(s)")
    return "\n".join(lines) + "\n"
