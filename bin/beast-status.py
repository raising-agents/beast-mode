#!/usr/bin/env python3
"""
beast-status — full dashboard for Beast Mode system.
Shows: Beast Index, dim breakdown, recent leaks, proposals, system health.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

LEDGER = Path.home() / ".claude" / "beast-mode" / "ledger" / "drift.jsonl"
_TRENDS_PY = Path(__file__).parent / "trends.py"

def _load_trends_module():
    if not _TRENDS_PY.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("beast_trends", _TRENDS_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None
PROPOSALS = Path.home() / ".claude" / "beast-mode" / "proposals"
CONSTITUTION = Path.home() / ".claude" / "instructions" / "beast-mode-constitution.md"
SETTINGS = Path.home() / ".claude" / "settings.json"
LAUNCHD = Path.home() / "Library" / "LaunchAgents" / "com.adrian.beast-mode-evolution.plist"
ERROR_LOG = Path.home() / ".claude" / "beast-mode" / "ledger" / "auditor-errors.log"

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"
MAGENTA = "\033[35m"

def hr(char="─", width=60):
    return DIM + char * width + RESET

def bold(s): return BOLD + s + RESET
def green(s): return GREEN + s + RESET
def red(s): return RED + s + RESET
def yellow(s): return YELLOW + s + RESET
def cyan(s): return CYAN + s + RESET
def dim(s): return DIM + s + RESET
def magenta(s): return MAGENTA + s + RESET

def load_ledger(window_days: int | None = None) -> list[dict]:
    if not LEDGER.exists():
        return []
    cutoff = None
    if window_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    entries = []
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cutoff:
            try:
                ts = datetime.fromisoformat(obj.get("ts", "").replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            except ValueError:
                continue
        entries.append(obj)
    return entries

def score_entries(entries: list[dict]) -> tuple[int, int, Counter, dict[str, list]]:
    beasts = applicable = 0
    leak_dims: Counter = Counter()
    leak_quotes: dict[str, list] = defaultdict(list)
    for e in entries:
        dims = e.get("dims") or {}
        for dim, v in dims.items():
            if v in (0, 1):
                applicable += 1
                if v == 1:
                    beasts += 1
                else:
                    leak_dims[dim] += 1
        for lk in e.get("leaks") or []:
            d = lk.get("dim", "unknown")
            leak_quotes[d].append({
                "quote": lk.get("quote", ""),
                "fix": lk.get("fix", ""),
                "ts": e.get("ts", ""),
                "prompt": e.get("user_prompt_excerpt", ""),
            })
    return beasts, applicable, leak_dims, leak_quotes

def pct_bar(beasts, applicable, width=20):
    if applicable == 0:
        return dim("░" * width)
    ratio = beasts / applicable
    filled = round(ratio * width)
    color = GREEN if ratio >= 0.8 else (YELLOW if ratio >= 0.6 else RED)
    return color + "█" * filled + RESET + DIM + "░" * (width - filled) + RESET

def dim_bar(n_leak, total_turns, width=10):
    if total_turns == 0:
        return dim("░" * width)
    ratio = min(1.0, n_leak / total_turns)
    filled = round(ratio * width)
    color = GREEN if filled == 0 else (YELLOW if filled <= 3 else RED)
    return color + "█" * filled + RESET + DIM + "░" * (width - filled) + RESET

def hook_active() -> bool:
    try:
        data = json.loads(SETTINGS.read_text())
        hooks = data.get("hooks", {})
        for events in hooks.values():
            for group in events:
                if isinstance(group, dict):
                    for hook in group.get("hooks", []):
                        if "beast-mode-stop" in hook.get("command", ""):
                            return True
    except Exception:
        pass
    return False

def evolution_active() -> bool:
    # Check launchd registration directly — plist may be loaded from bin/ path
    # without copying to ~/Library/LaunchAgents/
    try:
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=3
        )
        return "beast-mode-evolution" in result.stdout
    except Exception:
        return LAUNCHD.exists()

def latest_proposal() -> tuple[str | None, str | None]:
    if not PROPOSALS.exists():
        return None, None
    files = sorted(PROPOSALS.glob("*.md"), reverse=True)
    if not files:
        return None, None
    f = files[0]
    try:
        text = f.read_text()
    except Exception:
        return f.name, None
    return f.name, text

def recent_errors() -> list[str]:
    if not ERROR_LOG.exists():
        return []
    lines = ERROR_LOG.read_text().splitlines()
    return [l for l in lines if l.strip()][-5:]

def print_header():
    print()
    print(bold(cyan("  ██████╗ ███████╗ █████╗ ███████╗████████╗")))
    print(bold(cyan("  ██╔══██╗██╔════╝██╔══██╗██╔════╝╚══██╔══╝")))
    print(bold(cyan("  ██████╔╝█████╗  ███████║███████╗   ██║   ")))
    print(bold(cyan("  ██╔══██╗██╔══╝  ██╔══██║╚════██║   ██║   ")))
    print(bold(cyan("  ██████╔╝███████╗██║  ██║███████║   ██║   ")))
    print(bold(cyan("  ╚═════╝ ╚══════╝╚═╝  ╚═╝╚══════╝   ╚═╝   ")))
    print(dim("  Beast Mode Dashboard — " + datetime.now().strftime("%Y-%m-%d %H:%M")))
    print()

def section(title):
    print()
    print(bold(WHITE + "  " + title))
    print("  " + hr("─", 56))

def main():
    all_entries = load_ledger()
    week_entries = load_ledger(window_days=7)
    recent_entries = all_entries[-30:] if len(all_entries) > 30 else all_entries

    print_header()

    # ── SYSTEM STATUS ────────────────────────────────────────────
    section("SYSTEM")
    hook_ok = hook_active()
    evo_ok = evolution_active()
    const_ok = CONSTITUTION.exists()

    hook_str = green("● ACTIVE") if hook_ok else red("○ NOT WIRED")
    evo_str  = green("● RUNNING") if evo_ok else yellow("○ DISABLED  (run: launchctl bootstrap gui/$UID ~/.claude/beast-mode/bin/com.adrian.beast-mode-evolution.plist)")
    const_str = green("● v1") if const_ok else red("○ MISSING")

    print(f"  Stop hook     {hook_str}")
    print(f"  Evolution     {evo_str}")
    print(f"  Constitution  {const_str}")
    print(f"  Ledger        {dim(str(LEDGER))}")
    print(f"  Total turns   {bold(str(len(all_entries)))}")

    if not all_entries:
        section("BEAST INDEX")
        print(f"  {yellow('No data yet.')} Hook is wired — ledger fills after next assistant turns.")
        print()
        return

    # ── BEAST INDEX (rolling 30 + 7d) ────────────────────────────
    section("BEAST INDEX")
    b30, a30, ld30, lq30 = score_entries(recent_entries)
    b7,  a7,  ld7,  lq7  = score_entries(week_entries)
    ball, aall, ldall, lqall = score_entries(all_entries)

    def idx_line(label, b, a):
        if a == 0:
            return f"  {label:<16} {dim('no data')}"
        pct = b / a * 100
        color = GREEN if pct >= 80 else (YELLOW if pct >= 60 else RED)
        score_str = color + bold(f"{b}/{a}") + RESET + f"  {pct:.0f}%"
        bar = pct_bar(b, a)
        return f"  {label:<16} {score_str:<30} {bar}"

    print(idx_line("All time", ball, aall))
    print(idx_line("Last 7 days", b7, a7))
    print(idx_line("Last 30 turns", b30, a30))

    # ── TREND (weekly column chart) ──────────────────────────────
    trends = _load_trends_module()
    if trends:
        try:
            wk_buckets = trends.load_weekly_buckets()
            if wk_buckets:
                bef, aft = trends.before_after_summary(wk_buckets)
                install_iso = trends.BEAST_INSTALLED.isocalendar()
                install_k = (install_iso.year, install_iso.week)
                span = sorted(wk_buckets.keys())
                section(f"TREND  (weekly  W{span[0][1]:02d}/{span[0][0]} → W{span[-1][1]:02d}/{span[-1][0]})")
                delta = aft["ratio"] - bef["ratio"]
                delta_c = GREEN if delta > 0 else RED if delta < 0 else DIM
                bef_pct = f"{bef['ratio']:.0%}"
                aft_pct = f"{aft['ratio']:.0%}"
                print(f"  {dim('before')} {yellow(bef_pct)}   "
                      f"{dim('after')} {yellow(aft_pct)}   "
                      f"{dim('delta')} {delta_c}{delta:+.1%}{RESET}")
                print()
                chart_lines = trends.render_column_chart(
                    wk_buckets, height=6, install_key=install_k,
                )
                for cl in chart_lines:
                    print(cl)
        except Exception:
            pass

    # ── RECEIPT BREAKDOWN ────────────────────────────────────────
    RECEIPT_DIR = Path.home() / ".claude" / "beast-mode" / "receipts"
    RECEIPT_BIN = Path.home() / ".claude" / "beast-mode" / "bin" / "receipt_store.py"
    if RECEIPT_DIR.exists() and RECEIPT_BIN.exists():
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location("receipt_store", RECEIPT_BIN)
            _rs = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_rs)
            _stats = _rs.receipt_stats(days=7)
            _by_type = _stats.get("by_type", {})
            _rdims = _stats.get("dims", {})

            section("RECEIPTS  (last 7 days)")
            total_receipts = sum(_by_type.values())
            if total_receipts == 0:
                print(f"  {dim('No receipts yet.')} Accumulates after next session.")
            else:
                _type_colors = {"structural": CYAN, "behavioral": YELLOW, "meta": MAGENTA, "evolution": GREEN}
                for rtype, count in sorted(_by_type.items()):
                    c = _type_colors.get(rtype, DIM)
                    print(f"  {c}{rtype:<14}{RESET}  {bold(str(count))} receipts")

                # Meta signals from behavioral receipts
                meta_signals_agg = {"self_audited": 0, "total": 0, "coached_total": 0, "beast_total": 0}
                _seven_days_ago = datetime.now(timezone.utc).replace(tzinfo=None)
                for r in _rs.read_receipts(days=7, receipt_type="behavioral"):
                    ms = r.get("meta_signals") or {}
                    meta_signals_agg["total"] += 1
                    if ms.get("self_audited"):
                        meta_signals_agg["self_audited"] += 1
                    meta_signals_agg["coached_total"] += len(ms.get("coached_dims") or [])
                    meta_signals_agg["beast_total"] += len(ms.get("autonomous_beast_dims") or [])

                if meta_signals_agg["total"] > 0:
                    audit_rate = meta_signals_agg["self_audited"] / meta_signals_agg["total"]
                    audit_c = GREEN if audit_rate >= 0.2 else (YELLOW if audit_rate >= 0.05 else RED)
                    sa_n = meta_signals_agg["self_audited"]
                    sa_t = meta_signals_agg["total"]
                    print(f"\n  {dim('self_audit_rate')}  {audit_c}{audit_rate:.0%}{RESET}  "
                          f"{dim(f'({sa_n}/{sa_t} turns)')}")
                    total_dim_scores = meta_signals_agg["coached_total"] + meta_signals_agg["beast_total"]
                    if total_dim_scores > 0:
                        coach_dep = meta_signals_agg["coached_total"] / total_dim_scores
                        dep_c = GREEN if coach_dep < 0.2 else (YELLOW if coach_dep < 0.4 else RED)
                        print(f"  {dim('coaching_dep')}    {dep_c}{coach_dep:.0%}{RESET}  "
                              f"{dim('(lower = more autonomous)')}")

        except Exception:
            pass

    # ── DIMENSION BREAKDOWN ──────────────────────────────────────
    section("DIMENSION BREAKDOWN  (last 30 turns, [S]=structural [LM]=Haiku)")
    # 10 dims: 6 legacy (from drift.jsonl) + 4 new (from receipt store if available)
    dims_order_legacy = ["parallelism", "scope", "depth", "sequencing", "deferrals", "boldness"]
    dims_order_new = ["verification_by_evidence", "action_over_announcement", "block_breaking", "self_direction_over_ask"]
    dim_labels = {
        "parallelism":              "Parallelism",
        "scope":                    "Scope",
        "depth":                    "Depth",
        "sequencing":               "Sequencing",
        "deferrals":                "Deferrals",
        "boldness":                 "Boldness",
        "verification_by_evidence": "Verification",
        "action_over_announcement": "Action/Announ.",
        "block_breaking":           "BlockBreaking",
        "self_direction_over_ask":  "Self-Direction",
    }
    # Method badges
    dim_method = {
        "parallelism": "[S] ",
        "action_over_announcement": "[S] ",
    }
    STRUCTURAL_DIMS = {"parallelism", "action_over_announcement"}

    # Per-dim beast/applicable from drift.jsonl (legacy 6)
    dim_scores: dict[str, tuple[int, int]] = {}
    for d in dims_order_legacy:
        b = a = 0
        for e in recent_entries:
            v = (e.get("dims") or {}).get(d)
            if v in (0, 1):
                a += 1
                if v == 1:
                    b += 1
        dim_scores[d] = (b, a)

    # Per-dim from receipt store (new 4 dims + override for structural)
    try:
        if RECEIPT_DIR.exists() and RECEIPT_BIN.exists() and "_rs" in dir():
            _rdims_stats = _rs.receipt_stats(days=30).get("dims", {})
            for d in dims_order_new:
                ds = _rdims_stats.get(d, {})
                dim_scores[d] = (ds.get("beast", 0), ds.get("total", 0))
            # If structural receipt has parallelism, use it
            if "parallelism" in _rdims_stats and _rdims_stats["parallelism"].get("method") == "structural":
                ps = _rdims_stats["parallelism"]
                dim_scores["parallelism"] = (ps.get("beast", 0), ps.get("total", 0))
    except Exception:
        for d in dims_order_new:
            dim_scores.setdefault(d, (0, 0))

    def _print_dim_row(d: str) -> None:
        b, a = dim_scores.get(d, (0, 0))
        label = dim_labels[d]
        leaks = ld30.get(d, 0)
        badge = dim(dim_method.get(d, "[LM]")) + " "
        if d in STRUCTURAL_DIMS:
            badge = cyan("[S] ")
        if a == 0:
            score_s = dim("n/a")
            bar_s = dim("░" * 12)
        else:
            pct = b / a * 100
            color = GREEN if pct >= 80 else (YELLOW if pct >= 60 else RED)
            score_s = color + f"{b}/{a}" + RESET
            bar_s = pct_bar(b, a, width=12)
        leak_tag = red(f"  ← {leaks} leak{'s' if leaks > 1 else ''}") if leaks > 0 else ""
        print(f"  {badge}{label:<14} {score_s:<20} {bar_s}{leak_tag}")

    print(f"  {dim('── behavioral (LM) ─────────────────────────────────────')}")
    for d in dims_order_legacy:
        _print_dim_row(d)
    print(f"  {dim('── new dims ─────────────────────────────────────────────')}")
    for d in dims_order_new:
        _print_dim_row(d)

    # ── RECENT LEAKS ─────────────────────────────────────────────
    all_leaks = []
    for e in reversed(all_entries[-50:]):
        for lk in (e.get("leaks") or []):
            all_leaks.append({**lk, "ts": e.get("ts", ""), "prompt": e.get("user_prompt_excerpt", "")})

    if all_leaks:
        section(f"RECENT LEAKS  (last {min(len(all_leaks), 8)})")
        for lk in all_leaks[:8]:
            ts = lk.get("ts", "")[:16].replace("T", " ")
            d = lk.get("dim", "?")
            quote = lk.get("quote", "")
            fix = lk.get("fix", "")
            print(f"  {dim(ts)}  {yellow(d)}")
            print(f"    {RED}❝{RESET} {quote}")
            if fix:
                print(f"    {GREEN}→{RESET} {dim(fix)}")
            print()
    else:
        section("LEAKS")
        print(f"  {green('None recorded.')} Clean run so far.")

    # ── EVOLUTION PROPOSALS ──────────────────────────────────────
    section("EVOLUTION PROPOSALS")
    fname, ptext = latest_proposal()
    if fname is None:
        print(f"  {dim('No proposals yet.')} Weekly loop writes to ~/.claude/beast-mode/proposals/")
    else:
        is_noop = "noop" in fname or "error" in fname
        status = dim("(noop)") if "noop" in fname else (red("(error)") if "error" in fname else green("(ready for review)"))
        print(f"  Latest: {bold(fname)}  {status}")
        if ptext and not is_noop:
            # Print first 20 lines of proposal
            lines = ptext.splitlines()
            for l in lines[:20]:
                print(f"  {dim(l)}")
            if len(lines) > 20:
                print(f"  {dim(f'... (+{len(lines)-20} more lines)')}")
        total = len(list(PROPOSALS.glob("*.md"))) if PROPOSALS.exists() else 0
        print(f"  Total proposals: {bold(str(total))}")

    # ── RECENT NOTES ─────────────────────────────────────────────
    notes = [(e.get("ts", "")[:16].replace("T", " "), e.get("notes", ""), e.get("score",""))
             for e in reversed(all_entries[-10:]) if e.get("notes") and e.get("notes") != "trivial"]
    if notes:
        section("RECENT AUDIT NOTES")
        for ts, note, score in notes[:6]:
            score_str = f" [{score}]" if score else ""
            print(f"  {dim(ts)}{cyan(score_str)}  {note}")

    # ── AUDITOR ERRORS ───────────────────────────────────────────
    errs = recent_errors()
    if errs:
        section("AUDITOR ERRORS  (recent)")
        for e in errs:
            print(f"  {red('!')} {dim(e)}")

    print()
    print(hr("═", 60))
    print(dim("  run `beast` anytime  ·  ledger: ~/.claude/beast-mode/ledger/drift.jsonl"))
    print(dim("  constitution: ~/.claude/instructions/beast-mode-constitution.md"))
    print()


if __name__ == "__main__":
    main()
