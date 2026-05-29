#!/usr/bin/env python3
"""
Beast Mode Daily Digest — Loop 2 heartbeat.

Runs once a day (via launchd plist). Does:
  1. Aggregate last 24h structured stats (ledger, corrections, injections, blocklist log).
  2. Idle check → minimal digest if empty day.
  3. Invoke blocklist_promoter.py --once as subprocess; capture JSON summary.
  4. Optional Sonnet enrichment (Headline + Recommendations). Fail-soft.
  5. Render markdown with YAML frontmatter; atomic-write to digests/{YYYY-MM-DD}.md + LATEST.md.
  6. Append one row to digests/daily-summary.jsonl (machine index for trend queries).

CLI:
  --once          run digest now (default for launchd invocations)
  --dry-run       compute everything; write nothing
  --hours N       window override (default 24)
  --no-promoter   skip promoter subprocess
  --no-lm         skip Sonnet call
  --mock-sonnet   read Sonnet output from JSON file (testing)
  --force         bypass idle threshold

Pattern reuses across the Beast Mode codebase:
  - subprocess to claude binary  : auditor-worker.py / evolution.py
  - atomic file write            : blocklist_manager.py:save_blocklist
  - append-only JSONL summary    : receipt_store
  - recursion guard env vars     : everywhere
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(2)


HOME = Path.home()
BIN_DIR = HOME / ".claude" / "beast-mode" / "bin"
LEDGER_PATH = HOME / ".claude" / "beast-mode" / "ledger" / "drift.jsonl"
CORRECTIONS_PATH = HOME / ".claude" / "beast-mode" / "calibration" / "corrections.jsonl"
INJECTIONS_PATH = HOME / ".claude" / "beast-mode" / "rules" / "preturn-injections.jsonl"
BLOCKLIST_LOG_PATH = HOME / ".claude" / "beast-mode" / "rules" / "blocklist-log.jsonl"
DIGESTS_DIR = HOME / ".claude" / "beast-mode" / "digests"
DIGEST_INDEX = DIGESTS_DIR / "daily-summary.jsonl"
LATEST_PATH = DIGESTS_DIR / "LATEST.md"
ERROR_LOG = DIGESTS_DIR / "daily-errors.log"

PROMOTER = BIN_DIR / "blocklist_promoter.py"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local" / "bin" / "claude"))
PYTHON = "/opt/miniconda3/bin/python"

SCHEMA_VERSION = 1
PROMOTER_TIMEOUT = 180
SONNET_TIMEOUT = 60
IDLE_LEDGER_MIN = 5

# Make blocklist_manager importable
sys.path.insert(0, str(BIN_DIR))
import blocklist_manager as bm  # noqa: E402


# ─── load evolution.py via importlib (hyphen-free filename, but evolution is in
#    bin/, so spec-load to avoid potential side-effects from import) ──────────

def _load_evolution():
    spec = importlib.util.spec_from_file_location("evolution", BIN_DIR / "evolution.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── log helpers ──────────────────────────────────────────────────────────────

def log_error(msg: str) -> None:
    try:
        DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except Exception:
        pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── score_entries (inlined from beast-status.py:81-102) ──────────────────────

def score_entries(entries: list[dict]) -> tuple[int, int, Counter, dict[str, list]]:
    beasts = applicable = 0
    leak_dims: Counter = Counter()
    leak_quotes: dict[str, list] = defaultdict(list)
    for e in entries:
        dims = e.get("dims") or {}
        for d, v in dims.items():
            if v in (0, 1):
                applicable += 1
                if v == 1:
                    beasts += 1
                else:
                    leak_dims[d] += 1
        for lk in (e.get("leaks") or []):
            d = lk.get("dim", "unknown")
            leak_quotes[d].append({
                "quote": (lk.get("quote") or "")[:80],
                "ts": e.get("ts", ""),
            })
    return beasts, applicable, leak_dims, leak_quotes


# ─── window read helpers ──────────────────────────────────────────────────────

def _read_jsonl_since(path: Path, cutoff: datetime) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_str = r.get("ts") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts >= cutoff:
                out.append(r)
    except Exception as e:
        log_error(f"read {path.name}: {e}")
    return out


def read_ledger_window(cutoff: datetime) -> list[dict]:
    return _read_jsonl_since(LEDGER_PATH, cutoff)


def read_corrections_window(cutoff: datetime) -> list[dict]:
    return _read_jsonl_since(CORRECTIONS_PATH, cutoff)


def read_injections_window(cutoff: datetime) -> list[dict]:
    return _read_jsonl_since(INJECTIONS_PATH, cutoff)


def read_blocklist_log_window(cutoff: datetime) -> list[dict]:
    return _read_jsonl_since(BLOCKLIST_LOG_PATH, cutoff)


# ─── aggregation ──────────────────────────────────────────────────────────────

def aggregate(window_hours: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    entries = read_ledger_window(cutoff)
    beasts, applicable, leak_dims, leak_quotes = score_entries(entries)
    # Per-dim applicable + leak rate (only dims with >=3 applicable to surface in top-3)
    per_dim_applicable: dict[str, int] = defaultdict(int)
    per_dim_leak: dict[str, int] = defaultdict(int)
    for e in entries:
        for d, v in (e.get("dims") or {}).items():
            if v in (0, 1):
                per_dim_applicable[d] += 1
                if v == 0:
                    per_dim_leak[d] += 1
    top_dims = []
    for d, app in per_dim_applicable.items():
        if app < 3:
            continue
        lk = per_dim_leak[d]
        if lk == 0:
            continue
        sample = ""
        if leak_quotes.get(d):
            sample = leak_quotes[d][0].get("quote", "")[:60]
        top_dims.append({
            "dim": d,
            "rate": lk / app,
            "leaks": lk,
            "applicable": app,
            "sample": sample,
        })
    top_dims.sort(key=lambda x: (-x["rate"], -x["leaks"]))
    top_dims = top_dims[:3]

    corrections = read_corrections_window(cutoff)
    confirms_true = sum(1 for c in corrections if c.get("confirms_existing_leak") is True)
    confirms_false = sum(1 for c in corrections if c.get("confirms_existing_leak") is False)

    injections = read_injections_window(cutoff)
    inj_total = len(injections)
    inj_injected = sum(1 for i in injections if i.get("injected"))
    inj_chars = [int(i.get("block_chars") or 0) for i in injections if i.get("injected")]
    inj_avg_chars = int(sum(inj_chars) / len(inj_chars)) if inj_chars else 0

    log_events = read_blocklist_log_window(cutoff)
    events_by_type: Counter = Counter(ev.get("event", "unknown") for ev in log_events)

    bl = bm.load_blocklist()
    by_status: Counter = Counter(p.get("status") for p in bl.get("phrases", []))

    return {
        "window_hours": window_hours,
        "turns_audited": len(entries),
        "beasts": beasts,
        "applicable": applicable,
        "beast_index": f"{beasts}/{applicable}",
        "leak_rate": (beasts / applicable) if applicable else None,
        "top_leak_dims": top_dims,
        "corrections_logged": len(corrections),
        "confirms_true": confirms_true,
        "confirms_false": confirms_false,
        "injections_fired": inj_total,
        "injections_injected": inj_injected,
        "injections_avg_chars": inj_avg_chars,
        "blocklist_events_by_type": dict(events_by_type),
        "blocklist_active": by_status.get("active", 0),
        "blocklist_candidate": by_status.get("candidate", 0),
        "blocklist_retired": by_status.get("retired", 0),
    }


def is_idle(stats: dict) -> bool:
    return (stats["turns_audited"] < IDLE_LEDGER_MIN
            and stats["corrections_logged"] < 1
            and stats["injections_fired"] < 1
            and sum(stats["blocklist_events_by_type"].values()) < 1)


# ─── promoter subprocess ──────────────────────────────────────────────────────

def run_promoter(dry_run: bool) -> dict:
    if not PROMOTER.exists():
        return {"status": "not_found"}
    cmd = [PYTHON, str(PROMOTER), "--once"]
    if dry_run:
        cmd.append("--dry-run")
    env = os.environ.copy()
    env["BEAST_MODE_INTERPRETER_RUNNING"] = "1"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=PROMOTER_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    except Exception as e:
        log_error(f"promoter subprocess: {e}")
        return {"status": "error", "error": str(e)}

    # parse last JSON object in stdout
    stdout = p.stdout or ""
    summary = None
    end = stdout.rfind("}")
    if end >= 0:
        # find matching opening brace
        depth = 0
        start = -1
        for i in range(end, -1, -1):
            ch = stdout[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start >= 0:
            try:
                summary = json.loads(stdout[start:end + 1])
            except json.JSONDecodeError:
                summary = None
    status = "ok" if p.returncode == 0 else "error"
    return {"status": status, "summary": summary, "rc": p.returncode,
            "stderr_tail": (p.stderr or "")[-200:]}


# ─── sonnet enrichment ────────────────────────────────────────────────────────

SONNET_SYSTEM = """\
You are the Beast Mode daily editor. Given structured stats from the last 24h,
produce TWO short outputs as JSON.

OUTPUT:
{
  "headline": "one sentence, <=100 chars, what changed today",
  "recommendations": ["actionable bullet, <=80 chars", ...up to 4 bullets...]
}

Constraints:
- Concrete. No filler. No 'consider' / 'might want to'.
- Anchor every recommendation to a stat in the input.
- If unremarkable: headline 'No notable change', recommendations [].
- Return JSON only. No prose. No markdown fences.
"""


def call_sonnet(stats: dict, promoter_result: dict, timeout: int = SONNET_TIMEOUT) -> dict | None:
    if os.environ.get("BEAST_MODE_INTERPRETER_RUNNING") == "1":
        log_error("sonnet skipped: BEAST_MODE_INTERPRETER_RUNNING=1")
        return None
    env = os.environ.copy()
    env["BEAST_MODE_INTERPRETER_RUNNING"] = "1"

    payload = "INPUT:\n" + json.dumps({"stats": stats, "promoter": promoter_result},
                                       indent=2, default=str) + "\n\nReturn JSON now."
    cmd = [CLAUDE_BIN, "-p", "--model", "sonnet",
           "--append-system-prompt", SONNET_SYSTEM, payload]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        log_error("sonnet timeout")
        return None
    except Exception as e:
        log_error(f"sonnet subprocess: {e}")
        return None
    if p.returncode != 0:
        log_error(f"sonnet rc={p.returncode} stderr={(p.stderr or '')[:200]}")
        return None
    raw = (p.stdout or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        nl = raw.find("\n")
        if nl >= 0:
            raw = raw[nl + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        log_error(f"sonnet no JSON: {raw[:200]}")
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        log_error(f"sonnet json parse: {e}")
        return None


# ─── render ───────────────────────────────────────────────────────────────────

def render_digest(date_str: str, stats: dict, promoter_result: dict,
                  lm: dict | None, lm_status: str, idle: bool) -> str:
    fm: dict = {
        "date": date_str,
        "window_hours": stats.get("window_hours", 24),
        "generated_ts": now_iso(),
        "schema_version": SCHEMA_VERSION,
        "idle": idle,
    }
    if idle:
        fm["turns_audited"] = stats["turns_audited"]
        body = (
            "Idle day. <5 audited turns, no corrections, no injections, "
            "no blocklist events. Promoter + Sonnet skipped."
        )
        front = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
        return f"---\n{front}\n---\n\n# Beast Daily — {date_str}\n\n{body}\n"

    fm.update({
        "beast_index": stats["beast_index"],
        "leak_rate": round(stats["leak_rate"], 3) if stats["leak_rate"] is not None else None,
        "turns_audited": stats["turns_audited"],
        "corrections_logged": stats["corrections_logged"],
        "injections_fired": stats["injections_fired"],
        "injections_injected": stats["injections_injected"],
        "blocklist_active": stats["blocklist_active"],
        "blocklist_candidate": stats["blocklist_candidate"],
        "blocklist_retired": stats["blocklist_retired"],
        "promoter_status": promoter_result.get("status", "unknown"),
        "promoter_added": (promoter_result.get("summary") or {}).get("added"),
        "promoter_promoted": (promoter_result.get("summary") or {}).get("promoted"),
        "promoter_retired": (promoter_result.get("summary") or {}).get("retired"),
        "promoter_applied_triggers": (promoter_result.get("summary") or {}).get("applied_triggers"),
        "promoter_haiku_failed": (promoter_result.get("summary") or {}).get("haiku_failed", False),
        "lm_status": lm_status,
        "sonnet_used": lm_status == "ok",
    })

    front = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()

    headline = ""
    if lm and lm.get("headline"):
        headline = lm["headline"]
    elif lm_status == "skipped":
        headline = "Sonnet skipped — stats only."
    else:
        headline = "Stats only — Sonnet unavailable."

    lines = [f"---", front, "---", "", f"# Beast Daily — {date_str}", "",
             "## Headline", headline, ""]

    lines.append("## Drift (last 24h)")
    lines.append(f"- Audited turns: {stats['turns_audited']}")
    pct = ""
    if stats["applicable"]:
        pct = f" ({int(round(stats['beasts'] / stats['applicable'] * 100))}%)"
    lines.append(f"- Beast Index: {stats['beast_index']}{pct}")
    if stats["top_leak_dims"]:
        lines.append("- Top leak dims:")
        for d in stats["top_leak_dims"]:
            sample = f' e.g. "{d["sample"]}"' if d["sample"] else ""
            r = int(round(d["rate"] * 100))
            lines.append(f"  - {d['dim']}: {r}% leak ({d['leaks']}/{d['applicable']}).{sample}")
    else:
        lines.append("- No leak dims surfaced (insufficient applicable counts).")
    lines.append("")

    lines.append("## Calibration")
    lines.append(f"- Corrections labeled: {stats['corrections_logged']}")
    lines.append(f"- Confirms existing leak (judge + Adrian agree): {stats['confirms_true']}")
    lines.append(f"- False-negative candidates (judge missed): {stats['confirms_false']}")
    lines.append("")

    lines.append("## Pre-turn injection")
    lines.append(f"- Hook fires: {stats['injections_fired']}")
    if stats["injections_fired"]:
        rate = int(round(stats["injections_injected"] / stats["injections_fired"] * 100))
        lines.append(f"- Injected: {stats['injections_injected']} ({rate}%)")
    else:
        lines.append(f"- Injected: 0")
    lines.append(f"- Skipped: {stats['injections_fired'] - stats['injections_injected']}")
    lines.append(f"- Avg block chars: {stats['injections_avg_chars']}")
    lines.append("")

    lines.append("## Blocklist activity")
    events_str = ", ".join(f"{k}={v}" for k, v in
                            sorted(stats["blocklist_events_by_type"].items(),
                                   key=lambda kv: -kv[1])) or "(none)"
    lines.append(f"- Events in 24h: {events_str}")
    lines.append(f"- Current state: active={stats['blocklist_active']}, "
                 f"candidate={stats['blocklist_candidate']}, retired={stats['blocklist_retired']}")
    s = promoter_result.get("summary") or {}
    if s:
        prom_line = ", ".join(f"{k}={v}" for k, v in s.items() if k != "by_status")
        lines.append(f"- Promoter run ({promoter_result.get('status')}): {prom_line}")
    else:
        lines.append(f"- Promoter run: {promoter_result.get('status')}")
    lines.append("")

    if lm and lm.get("recommendations"):
        lines.append("## Recommendations")
        for r in lm["recommendations"][:4]:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("## Links")
    lines.append("- Constitution: ~/.claude/instructions/beast-mode-constitution.md")
    lines.append("- Ledger tail: tail -20 ~/.claude/beast-mode/ledger/drift.jsonl")
    lines.append("- Blocklist: /opt/miniconda3/bin/python ~/.claude/beast-mode/bin/blocklist_manager.py list")
    lines.append("")

    return "\n".join(lines)


def render_index_row(date_str: str, stats: dict, promoter_result: dict,
                     lm_status: str, idle: bool) -> dict:
    return {
        "date": date_str,
        "idle": idle,
        "beast_index": stats.get("beast_index"),
        "leak_rate": round(stats["leak_rate"], 3) if stats.get("leak_rate") is not None else None,
        "turns": stats.get("turns_audited", 0),
        "corrections": stats.get("corrections_logged", 0),
        "injections_injected": stats.get("injections_injected", 0),
        "triggered": stats.get("blocklist_events_by_type", {}).get("triggered", 0),
        "blocklist_active": stats.get("blocklist_active", 0),
        "promoter_status": promoter_result.get("status"),
        "promoter_added": (promoter_result.get("summary") or {}).get("added"),
        "lm_status": lm_status,
        "ts": now_iso(),
    }


# ─── atomic write ─────────────────────────────────────────────────────────────

def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_index_row(row: dict) -> None:
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    with DIGEST_INDEX.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


# ─── main ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Beast Mode Daily Digest")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--no-promoter", action="store_true")
    p.add_argument("--no-lm", action="store_true")
    p.add_argument("--mock-sonnet", default=None)
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    # default to --once behavior even without flag (launchd invocations)
    stats = aggregate(args.hours)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    idle = is_idle(stats)
    if idle and not args.force:
        if args.dry_run:
            print(f"[dry-run] would write idle digest for {date_str}")
            return 0
        content = render_digest(date_str, stats, {"status": "skipped"}, None, "skipped", idle=True)
        out_path = DIGESTS_DIR / f"{date_str}.md"
        atomic_write(out_path, content)
        atomic_write(LATEST_PATH, content)
        append_index_row(render_index_row(date_str, stats, {"status": "skipped"}, "skipped", True))
        print(f"[idle] {out_path}")
        return 0

    # Promoter
    if args.no_promoter:
        promoter_result = {"status": "skipped"}
    else:
        promoter_result = run_promoter(dry_run=args.dry_run)

    # Sonnet
    lm: dict | None = None
    if args.no_lm:
        lm_status = "skipped"
    elif args.mock_sonnet:
        try:
            with open(args.mock_sonnet) as f:
                lm = json.load(f)
            lm_status = "ok"
        except Exception as e:
            log_error(f"mock-sonnet load: {e}")
            lm_status = "malformed"
    else:
        lm = call_sonnet(stats, promoter_result)
        if lm is None:
            lm_status = "unavailable"
        elif not isinstance(lm, dict) or "headline" not in lm:
            lm_status = "malformed"
            lm = None
        else:
            lm_status = "ok"

    content = render_digest(date_str, stats, promoter_result, lm, lm_status, idle=False)

    if args.dry_run:
        print("[dry-run] digest preview:")
        print(content)
        return 0

    out_path = DIGESTS_DIR / f"{date_str}.md"
    atomic_write(out_path, content)
    atomic_write(LATEST_PATH, content)
    append_index_row(render_index_row(date_str, stats, promoter_result, lm_status, False))

    summary = {
        "wrote": str(out_path),
        "beast_index": stats["beast_index"],
        "promoter_status": promoter_result.get("status"),
        "lm_status": lm_status,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
