#!/usr/bin/env python3
"""
Beast Mode Structural Collector — PostToolUse hook.

Accumulates per-call tool data to /tmp/beast-struct-{session_id}.json.
Fast (<5ms). Never blocks. Fails open silently.

The auditor-worker reads this file at Stop time to compute structural receipts
without any LM call.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path("/tmp")
TRACKED_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep", "LS", "WebFetch", "WebSearch"}


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"beast-struct-{session_id}.json"


def load_state(session_id: str) -> dict:
    p = state_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {
        "session_id": session_id,
        "calls": [],       # {tool, input_summary, ts, turn_seq}
        "turn_seq": 0,     # incremented by Stop logic in auditor-worker
    }


def save_state(session_id: str, state: dict) -> None:
    p = state_path(session_id)
    p.write_text(json.dumps(state, separators=(",", ":")))


def _input_summary(tool_name: str, tool_input: dict) -> str:
    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        return tool_input.get("file_path", "")
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:120]
    if tool_name in ("WebFetch", "WebSearch"):
        return tool_input.get("url", tool_input.get("query", ""))[:120]
    return ""


def main() -> int:
    # Recursion guard
    if os.environ.get("BEAST_MODE_AUDITOR_RUNNING") == "1":
        return 0
    if os.environ.get("BEAST_MODE_INTERPRETER_RUNNING") == "1":
        return 0

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    session_id = data.get("session_id") or "unknown"
    tool_name = data.get("tool_name") or ""

    if tool_name not in TRACKED_TOOLS:
        return 0

    tool_input = data.get("tool_input") or {}
    summary = _input_summary(tool_name, tool_input)

    try:
        state = load_state(session_id)
        state["calls"].append({
            "tool": tool_name,
            "summary": summary,
            "ts": time.time(),
            "turn_seq": state.get("turn_seq", 0),
        })
        save_state(session_id, state)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
