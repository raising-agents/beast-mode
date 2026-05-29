#!/usr/bin/env python3
"""
Beast Mode Pre-turn Injector — UserPromptSubmit hook.

Two jobs per fire:

  JOB A (trigger detection):
    Scan prior assistant turn for active blocklist phrases.
    Each hit → append `triggered` event to rules/blocklist-log.jsonl.
    Promoter consumes events later to bump trigger_count + last_triggered_ts.

  JOB B (drift injection):
    Build a terse structured block from:
      - top-3 leak dims in rolling 7d (>=10 applicable filter)
      - active phrases from rules/blocklist.yaml (capped to 5)
    Print to stdout → Claude Code wraps as <system-reminder>.

Always writes one audit row to rules/preturn-injections.jsonl (even on skip).

Fail-open: any exception → log + exit 0. Never blocks user.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    # Fail-open if pyyaml missing — write minimal log row + exit
    print("", end="")
    sys.exit(0)

HOME = Path.home()
RULES_DIR = HOME / ".claude" / "beast-mode" / "rules"
BLOCKLIST_PATH = RULES_DIR / "blocklist.yaml"
BLOCKLIST_LOG = RULES_DIR / "blocklist-log.jsonl"
INJECTIONS_LOG = RULES_DIR / "preturn-injections.jsonl"
ERROR_LOG = RULES_DIR / "preturn-errors.log"
LEDGER_PATH = HOME / ".claude" / "beast-mode" / "ledger" / "drift.jsonl"

SCHEMA_VERSION = 1
LEDGER_TAIL_BYTES = 80_000        # ~last 200 rows covers 7d generously
LEDGER_WINDOW_DAYS = 7
DIM_APPLICABLE_MIN = 10            # ignore dims that barely fire
MAX_DIMS = 3
MAX_PHRASES = 5
HARD_CAP_CHARS = 600               # injection budget
ASST_SCAN_CAP = 20_000             # chars of prior assistant turn scanned for triggers


# ─── utilities ────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_error(msg: str) -> None:
    try:
        RULES_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"{now_iso()} {msg}\n")
    except Exception:
        pass


def write_audit(row: dict) -> None:
    try:
        RULES_DIR.mkdir(parents=True, exist_ok=True)
        row.setdefault("schema_version", SCHEMA_VERSION)
        row.setdefault("ts", now_iso())
        with INJECTIONS_LOG.open("a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as e:
        log_error(f"audit write failed: {e}")


def append_triggered_event(entry_id: str, phrase: str, session_id: str) -> None:
    try:
        RULES_DIR.mkdir(parents=True, exist_ok=True)
        obj = {
            "ts": now_iso(),
            "event": "triggered",
            "id": entry_id,
            "actor": "pre-turn-hook",
            "details": {
                "session": session_id,
                "matched_in": "prior_assistant_turn",
                "phrase": phrase[:80],
            },
        }
        with BLOCKLIST_LOG.open("a") as f:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
    except Exception as e:
        log_error(f"triggered event write failed: {e}")


# ─── blocklist read ──────────────────────────────────────────────────────────

def load_active_phrases() -> list[dict]:
    if not BLOCKLIST_PATH.exists():
        return []
    try:
        data = yaml.safe_load(BLOCKLIST_PATH.read_text()) or {}
    except Exception as e:
        log_error(f"blocklist parse failed: {e}")
        return []
    phrases = data.get("phrases") or []
    return [p for p in phrases if isinstance(p, dict) and p.get("status") == "active"]


# ─── ledger read + dim ranking ───────────────────────────────────────────────

def load_recent_ledger_rows() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    try:
        with LEDGER_PATH.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_bytes = min(size, LEDGER_TAIL_BYTES)
            f.seek(size - read_bytes)
            chunk = f.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log_error(f"ledger read failed: {e}")
        return []
    lines = chunk.splitlines()
    if size > LEDGER_TAIL_BYTES and lines:
        lines = lines[1:]  # drop possibly-partial first line
    cutoff = datetime.now(timezone.utc) - timedelta(days=LEDGER_WINDOW_DAYS)
    rows = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            r = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            ts = datetime.fromisoformat((r.get("ts") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= cutoff:
            rows.append(r)
    return rows


def rank_dims(rows: list[dict]) -> list[dict]:
    applicable = defaultdict(int)
    leaks = defaultdict(int)
    samples: dict[str, str] = {}
    for r in rows:
        dims = r.get("dims") or {}
        for d, v in dims.items():
            if v in (0, 1):
                applicable[d] += 1
                if v == 0:
                    leaks[d] += 1
        for leak in (r.get("leaks") or []):
            d = leak.get("dim")
            q = (leak.get("quote") or "").strip()
            if d and q and d not in samples:
                samples[d] = q[:60]
    ranked = []
    for d, app in applicable.items():
        if app < DIM_APPLICABLE_MIN:
            continue
        lk = leaks[d]
        if lk == 0:
            continue
        ranked.append({
            "dim": d,
            "rate": lk / app,
            "leaks": lk,
            "applicable": app,
            "sample": samples.get(d, ""),
        })
    ranked.sort(key=lambda x: (-x["rate"], -x["leaks"]))
    return ranked[:MAX_DIMS]


# ─── transcript read ─────────────────────────────────────────────────────────

def extract_last_assistant(transcript_path: str) -> str:
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(errors="ignore").splitlines()
    except Exception as e:
        log_error(f"transcript read failed: {e}")
        return ""
    last_text = ""
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or obj
        role = msg.get("role")
        if role != "assistant":
            continue
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    text += blk.get("text", "")
        if text.strip():
            last_text = text
    return last_text


# ─── job A — trigger detection ───────────────────────────────────────────────

def detect_triggers(active_phrases: list[dict], asst_text: str, session_id: str) -> list[dict]:
    if not active_phrases or not asst_text:
        return []
    asst_lower = asst_text[:ASST_SCAN_CAP].lower()
    hits: list[dict] = []
    seen_ids = set()
    for p in active_phrases:
        phrase = (p.get("phrase") or "").strip().lower()
        eid = p.get("id")
        if not phrase or not eid or eid in seen_ids:
            continue
        if phrase in asst_lower:
            seen_ids.add(eid)
            append_triggered_event(eid, p.get("phrase") or "", session_id)
            hits.append({"id": eid, "phrase": p.get("phrase"),
                         "dim_primary": p.get("dim_primary")})
    return hits


# ─── job B — render injection ────────────────────────────────────────────────

def sort_phrases_for_injection(active: list[dict]) -> list[dict]:
    def keyfn(p: dict):
        hits = -int(p.get("hits") or 0)
        ts = p.get("last_seen_ts") or ""
        return (hits, -len(ts), ts)
    return sorted(active, key=keyfn)


def render_block(ranked_dims: list[dict], active_phrases: list[dict]) -> str:
    if not ranked_dims and not active_phrases:
        return ""
    lines: list[str] = []
    if ranked_dims:
        lines.append("BEAST DRIFT (rolling 7d):")
        for d in ranked_dims:
            pct = int(round(d["rate"] * 100))
            sample = d["sample"]
            sample_part = f' e.g. "{sample}"' if sample else ""
            lines.append(f'- {d["dim"]}: {pct}% leak ({d["leaks"]}/{d["applicable"]}).{sample_part}')
        lines.append("")
    if active_phrases:
        lines.append("DO NOT use (active blocklist):")
        sorted_p = sort_phrases_for_injection(active_phrases)[:MAX_PHRASES]
        for p in sorted_p:
            phr = (p.get("phrase") or "").strip()
            if phr:
                lines.append(f'- "{phr}"')
        lines.append("")
    lines.append("Cite (a)-(d) for deferrals. Restore full scope.")
    block = "\n".join(lines)
    if len(block) <= HARD_CAP_CHARS:
        return block
    # over cap → trim phrase lines first
    if active_phrases:
        # rebuild without phrases
        lines2 = []
        if ranked_dims:
            lines2.append("BEAST DRIFT (rolling 7d):")
            for d in ranked_dims:
                pct = int(round(d["rate"] * 100))
                lines2.append(f'- {d["dim"]}: {pct}% leak ({d["leaks"]}/{d["applicable"]}).')
            lines2.append("")
        lines2.append("Cite (a)-(d) for deferrals. Restore full scope.")
        trimmed = "\n".join(lines2)
        if len(trimmed) <= HARD_CAP_CHARS:
            return trimmed
        return trimmed[:HARD_CAP_CHARS]
    return block[:HARD_CAP_CHARS]


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    # Recursion + disable guards
    if os.environ.get("BEAST_MODE_AUDITOR_RUNNING") == "1":
        write_audit({"session": None, "injected": False,
                     "skipped_reason": "recursion_guard_auditor"})
        return 0
    if os.environ.get("BEAST_MODE_INTERPRETER_RUNNING") == "1":
        write_audit({"session": None, "injected": False,
                     "skipped_reason": "recursion_guard_interpreter"})
        return 0
    if os.environ.get("BEAST_PRETURN_DISABLED") == "1":
        write_audit({"session": None, "injected": False,
                     "skipped_reason": "dryrun_disabled"})
        return 0

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        write_audit({"session": None, "injected": False, "skipped_reason": "stdin_parse_error"})
        return 0

    session_id = data.get("session_id") or ""
    transcript_path = data.get("transcript_path") or ""
    if not session_id:
        write_audit({"session": None, "injected": False, "skipped_reason": "no_session_id"})
        return 0

    try:
        active_phrases = load_active_phrases()
        rows = load_recent_ledger_rows()
        ranked = rank_dims(rows)
        asst_text = extract_last_assistant(transcript_path)
        triggers = detect_triggers(active_phrases, asst_text, session_id)
        block = render_block(ranked, active_phrases)

        injected = bool(block)
        skipped_reason = None if injected else "nothing_to_inject"

        if injected:
            print(block)

        write_audit({
            "session": session_id,
            "prior_assistant_chars": len(asst_text),
            "ledger_rows_window": len(rows),
            "triggers_found": triggers,
            "top_leak_dims": ranked,
            "active_phrases_count": len(active_phrases),
            "active_phrases_injected": [
                (p.get("phrase") or "")
                for p in sort_phrases_for_injection(active_phrases)[:MAX_PHRASES]
            ] if active_phrases else [],
            "block_chars": len(block),
            "injected": injected,
            "skipped_reason": skipped_reason,
        })
    except Exception as e:
        log_error(f"main failed: {e}")
        write_audit({"session": session_id, "injected": False,
                     "skipped_reason": f"exception:{type(e).__name__}"})

    return 0


if __name__ == "__main__":
    sys.exit(main())
