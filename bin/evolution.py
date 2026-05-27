#!/usr/bin/env python3
"""
Beast Mode Evolution Loop.

Run weekly (cron / launchd / scheduled remote agent). Reads the drift ledger,
identifies the agent's top recurring human-framing patterns, and uses Opus to
propose Constitution amendments as a PR-shaped markdown proposal Adrian reviews.

Output: ~/.claude/beast-mode/proposals/{YYYY-MM-DD}-amendment.md

Does NOT modify the Constitution directly. Adrian approves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))
LEDGER = Path.home() / ".claude" / "beast-mode" / "ledger" / "drift.jsonl"
PROPOSAL_DIR = Path.home() / ".claude" / "beast-mode" / "proposals"
CONSTITUTION = Path.home() / ".claude" / "instructions" / "beast-mode-constitution.md"
WINDOW_DAYS = 7

OPUS_KERNEL = """\
You are the Beast Mode Constitution Editor. You read a digest of the past week's drift-ledger findings — concrete cases where the coding agent leaked human-framing — and propose amendments to the Constitution.

Your output is a PR-shaped markdown proposal Adrian will read. It must:
1. Cite specific quoted leaks from the digest as evidence
2. Identify patterns (3+ similar leaks = pattern) before proposing changes
3. Propose surgical Constitution edits — section, before/after text — not rewrites
4. Justify each amendment in 1-2 sentences
5. Self-rate proposals: HIGH leverage (kills a recurring leak pattern), MEDIUM (refines existing rule), LOW (cosmetic)

You may also propose:
- New Beast Index dimensions if leaks consistently fall outside the current six
- New forbidden phrases for the antipatterns section
- Sunsetting rules that don't fire (zero leaks against a rule for 4+ weeks)

Output format (markdown, no JSON):

```
# Beast Mode Amendment Proposal — {DATE}

**Window**: {WINDOW_START} → {WINDOW_END}
**Entries analyzed**: {N}
**Rolling Beast Index**: {SCORE}

## Patterns observed
- ...

## Proposed amendments

### Amendment 1: <title>  [HIGH|MEDIUM|LOW]
**Why**: <evidence + reasoning>
**Section affected**: <section name>
**Before**:
> <existing text>
**After**:
> <proposed text>

(repeat for each amendment)

## Open questions for Adrian
- ...
```

If the week's data does not warrant any amendments, say so plainly and explain why.
"""


RECEIPT_BIN = Path.home() / ".claude" / "beast-mode" / "bin" / "receipt_store.py"


def load_recent_entries(window_days: int = WINDOW_DAYS) -> list[dict]:
    if not LEDGER.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    entries = []
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_str = obj.get("ts")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= cutoff:
            entries.append(obj)
    return entries


def load_typed_receipts(window_days: int = WINDOW_DAYS) -> dict[str, list[dict]]:
    """Load receipts from typed receipt store, grouped by receipt_type."""
    if not RECEIPT_BIN.exists():
        return {}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("receipt_store", RECEIPT_BIN)
        rs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rs)
        all_r = rs.read_receipts(days=window_days)
        grouped: dict[str, list[dict]] = {}
        for r in all_r:
            t = r.get("receipt_type", "unknown")
            grouped.setdefault(t, []).append(r)
        return grouped
    except Exception:
        return {}


def build_receipt_section(typed: dict[str, list[dict]]) -> str:
    """Build the typed-receipt section of the digest."""
    lines = ["## Typed receipt breakdown (v2 observability layer)"]
    lines.append("")

    structural = typed.get("structural", [])
    behavioral = typed.get("behavioral", [])

    lines.append(f"- Structural receipts: {len(structural)}")
    lines.append(f"- Behavioral receipts: {len(behavioral)}")

    # Structural: parallelism ground truth
    if structural:
        par_scores = [
            r.get("dims", {}).get("parallelism", {}).get("score")
            for r in structural
            if isinstance(r.get("dims", {}).get("parallelism"), dict)
        ]
        par_scored = [s for s in par_scores if s in (0, 1)]
        if par_scored:
            par_rate = sum(par_scored) / len(par_scored)
            lines.append(f"- Structural parallelism rate (ground truth): {par_rate:.0%} "
                         f"({sum(par_scored)}/{len(par_scored)} turns beast)")

    # Meta signals from behavioral receipts
    self_audits = sum(
        1 for r in behavioral
        if (r.get("meta_signals") or {}).get("self_audited")
    )
    if behavioral:
        lines.append(f"- Self-audit rate: {self_audits}/{len(behavioral)} turns "
                     f"({self_audits/len(behavioral):.0%}) — target >20%")
        if self_audits == 0:
            lines.append("  ⚠ Constitution §V not self-executing: check_framing never called autonomously.")

    # Structural vs LM disagreement on parallelism
    disagreements = []
    for r in behavioral:
        dims = r.get("dims") or {}
        par = dims.get("parallelism") or {}
        if isinstance(par, dict) and par.get("method") == "structural":
            # This was overridden with structural ground truth
            pass  # agreement by construction
    if disagreements:
        lines.append(f"\n### Structural/LM disagreements (highest-value amendment targets):")
        for d in disagreements[:3]:
            lines.append(f"- {d}")

    # New dims (4 added in v2) — show if data exists
    new_dims = ["verification_by_evidence", "action_over_announcement", "block_breaking", "self_direction_over_ask"]
    new_dim_data: dict[str, dict] = {}
    for r in behavioral:
        for dim in new_dims:
            d = (r.get("dims") or {}).get(dim)
            if isinstance(d, dict) and d.get("score") in (0, 1):
                new_dim_data.setdefault(dim, {"beast": 0, "total": 0})
                new_dim_data[dim]["total"] += 1
                if d["score"] == 1:
                    new_dim_data[dim]["beast"] += 1

    if new_dim_data:
        lines.append("")
        lines.append("## New dim scores (v2)")
        for dim in new_dims:
            if dim in new_dim_data:
                d = new_dim_data[dim]
                rate = d["beast"] / d["total"]
                lines.append(f"- **{dim}**: {rate:.0%} ({d['beast']}/{d['total']})")

    return "\n".join(lines)


def build_digest(entries: list[dict], typed_receipts: dict | None = None) -> str:
    if not entries:
        return "No entries in window."

    # Aggregate legacy dims
    total_beasts = 0
    total_applicable = 0
    leak_dim_counter: Counter[str] = Counter()
    leak_quotes: dict[str, list[str]] = defaultdict(list)

    for e in entries:
        dims = e.get("dims") or {}
        for dim, v in dims.items():
            if v in (0, 1):
                total_applicable += 1
                if v == 1:
                    total_beasts += 1
                else:
                    leak_dim_counter[dim] += 1
        for leak in e.get("leaks") or []:
            dim = leak.get("dim", "unknown")
            quote = leak.get("quote", "").strip()
            if quote:
                leak_quotes[dim].append(quote)

    lines = []
    lines.append("# Drift Ledger Digest")
    lines.append("")
    lines.append(f"- Window: last {WINDOW_DAYS} days")
    lines.append(f"- Entries: {len(entries)}")
    lines.append(
        f"- Rolling Beast Index: {total_beasts}/{total_applicable}"
        + (f" ({total_beasts/total_applicable*100:.0f}%)" if total_applicable else "")
    )
    lines.append("")
    lines.append("## Leak frequency by dimension (legacy 6)")
    for dim, n in leak_dim_counter.most_common():
        lines.append(f"- **{dim}**: {n} leak(s)")
    lines.append("")
    lines.append("## Sample leaks (up to 8 per dimension)")
    for dim, quotes in leak_quotes.items():
        lines.append("")
        lines.append(f"### {dim}")
        for q in quotes[:8]:
            lines.append(f'- "{q}"')
    lines.append("")
    lines.append("## Recent audit notes")
    for e in entries[-10:]:
        n = e.get("notes")
        if n:
            lines.append(f"- {n}")

    # Typed receipt section (v2 layer)
    if typed_receipts:
        lines.append("")
        lines.append(build_receipt_section(typed_receipts))

    return "\n".join(lines)


def call_opus(digest: str) -> str | None:
    if os.environ.get("BEAST_MODE_INTERPRETER_RUNNING") == "1":
        return None
    env = os.environ.copy()
    env["BEAST_MODE_INTERPRETER_RUNNING"] = "1"

    constitution_text = CONSTITUTION.read_text() if CONSTITUTION.exists() else ""

    payload = (
        f"## CURRENT CONSTITUTION (for reference)\n---\n{constitution_text}\n---\n\n"
        f"## THIS WEEK'S DIGEST\n---\n{digest}\n---\n\n"
        f"Produce the amendment proposal now."
    )

    cmd = [
        CLAUDE_BIN, "-p",
        "--model", "opus",
        "--append-system-prompt", OPUS_KERNEL,
        payload,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=600, env=env, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def main() -> int:
    entries = load_recent_entries()
    typed_receipts = load_typed_receipts()
    digest = build_digest(entries, typed_receipts)

    PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not entries:
        # Still write a no-op record so cron has a heartbeat
        (PROPOSAL_DIR / f"{today}-noop.md").write_text(
            f"# Beast Mode Evolution — {today}\n\nNo ledger entries in the past {WINDOW_DAYS} days. No proposal generated.\n"
        )
        return 0

    proposal = call_opus(digest)
    if not proposal:
        (PROPOSAL_DIR / f"{today}-error.md").write_text(
            f"# Beast Mode Evolution — {today}\n\nOpus call failed or timed out.\n\n## Digest fallback\n\n{digest}\n"
        )
        return 1

    proposal_path = PROPOSAL_DIR / f"{today}-amendment.md"
    proposal_path.write_text(proposal)
    print(f"Wrote {proposal_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
