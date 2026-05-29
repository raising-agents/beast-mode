#!/usr/bin/env python3
"""
Beast Mode Action-Gap Gate — Stop hook.

Detects when the assistant ends a turn with a stated intent ("let me X / now I'll Y")
but no tool call followed in the same turn.

Why a structural detector and not an LM call:
- This is a tight, well-bounded class of phrases. The auditor's INTENT_PHRASES regex
  is already in production at bin/auditor-worker.py:36-41. We mirror it here.
- Stop hook latency budget is small; calling Haiku synchronously would re-introduce
  the recursion + cost we explicitly avoid.
- Adrian's "no regex" preference applies to BROAD pattern matching where regex is
  brittle. Action-gap detection is the explicit narrow-case exception: a small
  closed set of intent phrases that change rarely.

Output: append-only event row to ~/.claude/beast-mode/rules/action-gap-events.jsonl.

Fail-open: any exception → exit 0. Never blocks user.

Coexists with bin/beast-mode-stop.py (the auditor-spawner) — both run as Stop hooks.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
RULES_DIR = HOME / ".claude" / "beast-mode" / "rules"
EVENTS_PATH = RULES_DIR / "action-gap-events.jsonl"
ERROR_LOG = RULES_DIR / "action-gap-errors.log"
STATE_DIR = Path("/tmp")

# Mirror auditor-worker INTENT_PHRASES (bin/auditor-worker.py:36-41).
# Doctrine-anchored: maps to Constitution §III Action/Announcement antipatterns.
INTENT_PHRASES = re.compile(
    r"(let me |now i'?ll |i'?ll (start|begin|proceed|first|now)|"
    r"now (building|reading|checking|running|creating|writing|looking)|"
    r"next[,:]? i'?ll |proceeding to |going to )",
    re.IGNORECASE,
)

# Tail window scanned for intent phrase (chars).
TAIL_CHARS = 400

# Min assistant text length to qualify — avoids triggering on tool acknowledgments.
MIN_TEXT_LEN = 50

# Lookback window for the "did a tool call happen?" check.
TOOL_CALL_LOOKBACK_SECS = 60.0

SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_error(msg: str) -> None:
    try:
        RULES_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"{now_iso()} {msg}\n")
    except Exception:
        pass


# ─── structural state read ───────────────────────────────────────────────────

def load_structural_state(session_id: str) -> dict | None:
    p = STATE_DIR / f"beast-struct-{session_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def had_recent_tool_call(state: dict | None, before_ts: float, lookback: float) -> bool:
    if not state:
        return False
    calls = state.get("calls") or []
    if not calls:
        return False
    cutoff = before_ts - lookback
    for c in calls:
        ts = c.get("ts")
        if isinstance(ts, (int, float)) and ts >= cutoff:
            return True
    return False


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
        log_error(f"transcript read: {e}")
        return ""
    last = ""
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or obj
        if msg.get("role") != "assistant":
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
            last = text
    return last


# ─── once-per-session-per-pattern dedup ──────────────────────────────────────

def gate_already_fired(state: dict | None) -> bool:
    if not state:
        return False
    gates = state.get("gates_fired") or {}
    return bool(gates.get("action_gap"))


def mark_gate_fired(session_id: str) -> None:
    """Set gates_fired.action_gap=True in structural state file."""
    p = STATE_DIR / f"beast-struct-{session_id}.json"
    try:
        state = {}
        if p.exists():
            try:
                state = json.loads(p.read_text())
            except Exception:
                state = {"session_id": session_id, "calls": [], "turn_seq": 0}
        gates = state.get("gates_fired") or {}
        gates["action_gap"] = True
        state["gates_fired"] = gates
        p.write_text(json.dumps(state, separators=(",", ":")))
    except Exception as e:
        log_error(f"mark_gate_fired: {e}")


# ─── event write ─────────────────────────────────────────────────────────────

def append_event(event: dict) -> None:
    try:
        RULES_DIR.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception as e:
        log_error(f"event append: {e}")


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    # Recursion + disable guards
    if os.environ.get("BEAST_MODE_AUDITOR_RUNNING") == "1":
        return 0
    if os.environ.get("BEAST_MODE_INTERPRETER_RUNNING") == "1":
        return 0
    if os.environ.get("BEAST_ACTION_GAP_DISABLED") == "1":
        return 0

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    session_id = data.get("session_id") or ""
    transcript_path = data.get("transcript_path") or ""
    if not session_id or not transcript_path:
        return 0

    try:
        state = load_structural_state(session_id)

        # Fire-once per session
        if gate_already_fired(state):
            return 0

        asst_text = extract_last_assistant(transcript_path)
        if len(asst_text) < MIN_TEXT_LEN:
            return 0

        tail = asst_text[-TAIL_CHARS:]
        m = INTENT_PHRASES.search(tail)
        if not m:
            return 0

        # Intent phrase present in tail. Was there a tool call recently?
        now_ts = time.time()
        if had_recent_tool_call(state, now_ts, TOOL_CALL_LOOKBACK_SECS):
            return 0

        # Action gap detected. Log + mark fired.
        event = {
            "ts": now_iso(),
            "session": session_id,
            "schema_version": SCHEMA_VERSION,
            "intent_phrase": m.group(0).strip(),
            "tail_excerpt": tail[-200:],
            "tool_calls_in_state": len((state or {}).get("calls", []) or []),
            "last_tool_call_age_secs": (
                round(now_ts - max((c.get("ts") or 0)
                                    for c in (state or {}).get("calls") or [{"ts": 0}]),
                       1)
                if state and state.get("calls") else None
            ),
        }
        append_event(event)
        mark_gate_fired(session_id)

        # Optional systemMessage (PostToolUse / parallelism-gate pattern). Stop hooks
        # may or may not surface this depending on Claude Code version; emit anyway —
        # cheap, harmless if ignored, useful if visible.
        msg = (
            f"[BEAST STRUCTURAL · action_gap] Final intent phrase "
            f"{m.group(0).strip()!r} with no tool call in same turn. "
            f"Either execute or rewrite without announcing."
        )
        print(json.dumps({"systemMessage": msg}))

    except Exception as e:
        log_error(f"main: {e}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
