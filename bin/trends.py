#!/usr/bin/env python3
"""
Beast Mode Trend Analyzer.

Default: compact column chart (weeks as X-axis). Embeddable by beast-status.

Usage:
    python ~/.claude/beast-mode/bin/trends.py [--bucket day|week|month] [--height N]
    python ~/.claude/beast-mode/bin/trends.py --detail          # before/after table
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, date
from pathlib import Path

LEDGER = Path.home() / ".claude" / "beast-mode" / "ledger" / "drift.jsonl"
BEAST_INSTALLED = date(2026, 5, 20)
DIMS = ["parallelism", "scope", "depth", "sequencing", "deferrals", "boldness"]

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM_C  = "\033[2m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"


def _col_color(ratio: float) -> str:
    if ratio >= 0.85: return GREEN
    if ratio >= 0.70: return YELLOW
    return RED


def _pct(ratio: float) -> str:
    c = _col_color(ratio)
    return f"{c}{ratio:.0%}{RESET}"


def load_weekly_buckets(ledger: Path = LEDGER) -> dict[tuple[int, int], dict]:
    """Return {(year, week): {beast, total}} from non-trivial scored entries."""
    buckets: dict[tuple[int, int], dict] = defaultdict(lambda: {"beast": 0, "total": 0})
    with ledger.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("score") == "N/A":
                continue
            ts = e.get("ts", "")
            try:
                d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            except Exception:
                continue
            dims = e.get("dims") or {}
            scored = [v for v in dims.values() if v is not None]
            if not scored:
                continue
            iso = d.isocalendar()
            k = (iso.year, iso.week)
            buckets[k]["beast"] += sum(v for v in scored if v == 1)
            buckets[k]["total"] += len(scored)
    return dict(buckets)


def render_column_chart(
    buckets: dict[tuple[int, int], dict],
    height: int = 8,
    col_w: int = 3,
    install_key: tuple[int, int] | None = None,
    indent: str = "  ",
) -> list[str]:
    """
    Render a vertical-bar column chart where each column = one week.
    Returns a list of strings (one per output line), no trailing newlines.
    Gaps between non-consecutive weeks are shown as a "┄" filler column.
    """
    if not buckets:
        return [f"{indent}{DIM_C}no data{RESET}"]

    sorted_keys = sorted(buckets.keys())

    # Build display columns: data cols + gap markers between non-consecutive weeks
    cols: list[tuple[str, float | None, bool]] = []  # (label, ratio|None, is_install)
    for i, k in enumerate(sorted_keys):
        b = buckets[k]
        ratio = b["beast"] / b["total"] if b["total"] else None
        is_install = (k == install_key)
        label = f"W{k[1]:02d}"
        # Insert gap marker if there's a jump > 1 week
        if i > 0:
            prev = sorted_keys[i - 1]
            gap = k[1] - prev[1] if k[0] == prev[0] else 99
            if gap > 1:
                cols.append(("┄", None, False))  # gap column
        cols.append((label, ratio, is_install))

    n_cols = len(cols)
    bar_w = col_w - 1  # chars for bar, 1 for spacing
    total_w = n_cols * col_w

    lines: list[str] = []

    # Chart rows top→bottom
    for row in range(height, 0, -1):
        threshold = row / height
        row_str = indent
        for label, ratio, is_install in cols:
            if ratio is None:
                # gap or missing data
                if label == "┄":
                    row_str += DIM_C + "┄" * bar_w + " " + RESET
                else:
                    row_str += " " * col_w
            else:
                filled = ratio >= (threshold - 1 / height * 0.5)
                c = _col_color(ratio)
                if filled:
                    char = "▐▌" if (is_install and bar_w == 2) else ("█" * bar_w)
                    row_str += c + char + RESET + " "
                else:
                    row_str += " " * col_w
        lines.append(row_str.rstrip())

    # Bottom border
    lines.append(indent + DIM_C + "─" * total_w + RESET)

    # Week labels row
    label_row = indent
    for label, ratio, is_install in cols:
        install_marker = CYAN + "▲" + RESET if is_install and bar_w == 2 else ""
        if label == "┄":
            label_row += DIM_C + "┄" * bar_w + " " + RESET
        elif is_install and bar_w == 2:
            label_row += CYAN + label + RESET + " "
        else:
            label_row += DIM_C + label + RESET + " "
    lines.append(label_row.rstrip())

    # Score % row
    score_row = indent
    for label, ratio, is_install in cols:
        if label == "┄":
            score_row += " " * col_w
        elif ratio is None:
            score_row += DIM_C + "·" * bar_w + " " + RESET
        else:
            pct_int = round(ratio * 100)
            c = _col_color(ratio)
            score_row += c + f"{pct_int:<{bar_w}}" + RESET + " "
    lines.append(score_row.rstrip())

    # Install annotation row
    if install_key and install_key in buckets:
        ann_row = indent
        for label, ratio, is_install in cols:
            if label == "┄":
                ann_row += " " * col_w
            elif is_install:
                ann_row += CYAN + "▲" * bar_w + " " + RESET
            else:
                ann_row += " " * col_w
        lines.append(ann_row.rstrip())
        lines.append(f"{indent}{CYAN}▲ beast installed{RESET}")

    return lines


def before_after_summary(buckets: dict[tuple[int, int], dict]) -> tuple[dict, dict]:
    install_iso = BEAST_INSTALLED.isocalendar()
    install_k = (install_iso.year, install_iso.week)
    before = {k: v for k, v in buckets.items() if k < install_k}
    after  = {k: v for k, v in buckets.items() if k >= install_k}

    def agg(d: dict) -> dict:
        beast = sum(v["beast"] for v in d.values())
        total = sum(v["total"] for v in d.values())
        return {"beast": beast, "total": total, "ratio": beast / total if total else 0}

    return agg(before), agg(after)


def print_detail(buckets: dict[tuple[int, int], dict]) -> None:
    """Full before/after table."""
    bef, aft = before_after_summary(buckets)

    print(f"\n{BOLD}{WHITE}  BEFORE vs AFTER BEAST INSTALLATION{RESET}")
    print(f"  {DIM_C}{'─' * 56}{RESET}")

    def row(label: str, b: int, t: int) -> None:
        r = b / t if t else 0
        c = _col_color(r)
        bar_filled = round(r * 20)
        bar = c + "█" * bar_filled + DIM_C + "░" * (20 - bar_filled) + RESET
        print(f"  {label:<10}  {c}{r:.0%}{RESET}  {bar}  {DIM_C}({t} scored){RESET}")

    row("Before", bef["beast"], bef["total"])
    row("After ", aft["beast"], aft["total"])

    delta = aft["ratio"] - bef["ratio"]
    delta_c = GREEN if delta > 0 else RED if delta < 0 else DIM_C
    print(f"\n  Delta     {delta_c}{delta:+.1%}{RESET}")

    if aft["total"] < 200:
        print(f"  {YELLOW}⚠ Post-beast sample small ({aft['total']} scored dims) — wait 2-3 weeks{RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Beast Mode Trend Analyzer")
    parser.add_argument("--bucket", choices=["week", "month"], default="week",
                        help="Time bucket (default: week)")
    parser.add_argument("--height", type=int, default=8, help="Chart height in rows")
    parser.add_argument("--detail", action="store_true", help="Show before/after table")
    args = parser.parse_args()

    if not LEDGER.exists():
        print(f"Ledger not found: {LEDGER}", file=sys.stderr)
        sys.exit(1)

    buckets = load_weekly_buckets()
    if not buckets:
        print("No scoreable entries.", file=sys.stderr)
        sys.exit(1)

    bef, aft = before_after_summary(buckets)
    n_weeks = len(buckets)
    span_start = sorted(buckets.keys())[0]
    span_end   = sorted(buckets.keys())[-1]

    print()
    print(f"{BOLD}{CYAN}  BEAST TREND  {RESET}{DIM_C}({n_weeks} weeks  W{span_start[1]}/{span_start[0]} → W{span_end[1]}/{span_end[0]}){RESET}")
    print(f"  {DIM_C}before {_pct(bef['ratio'])}   after {_pct(aft['ratio'])}   Δ {GREEN if aft['ratio']>bef['ratio'] else RED}{aft['ratio']-bef['ratio']:+.1%}{RESET}")
    print()

    install_iso = BEAST_INSTALLED.isocalendar()
    install_k = (install_iso.year, install_iso.week)

    chart_lines = render_column_chart(
        buckets,
        height=args.height,
        install_key=install_k,
    )
    for line in chart_lines:
        print(line)

    if args.detail:
        print_detail(buckets)

    print()


if __name__ == "__main__":
    main()
