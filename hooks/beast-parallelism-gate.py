#!/usr/bin/env python3
"""
Beast Mode Parallelism Gate — PostToolUse hook.

Detects sequential tool calls (same tool type across separate model turns)
and injects a systemMessage receipt into the transcript mid-turn.

Uses timestamp delta: calls <MIN_BATCH_GAP seconds apart = same batch (parallel).
Calls >MIN_BATCH_GAP apart = separate model turns (sequential → leak).

Fires at most once per session per tool class (Read/Bash) to avoid spam.
Fails open silently.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path("/tmp")
MIN_BATCH_GAP = 6.0  # seconds — conservative; model turn latency is >6s
GATE_TOOLS = {
    "Read": "Read",
    "Write": "Read",   # Write after Read in same area = same class
    "Edit": "Read",
    "MultiEdit": "Read",
    "Bash": "Bash",
    "Glob": "Bash",
    "Grep": "Bash",
}


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"beast-struct-{session_id}.json"


def load_state(session_id: str) -> dict:
    p = state_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"session_id": session_id, "calls": [], "turn_seq": 0, "gates_fired": {}}


def save_state(session_id: str, state: dict) -> None:
    p = state_path(session_id)
    try:
        p.write_text(json.dumps(state, separators=(",", ":")))
    except Exception:
        pass


def main() -> int:
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
    tool_class = GATE_TOOLS.get(tool_name)

    if not tool_class:
        return 0

    now = time.time()

    try:
        state = load_state(session_id)
        gates_fired = state.get("gates_fired") or {}

        if not gates_fired.get(tool_class):
            # Find the most recent call of same class
            same_class_calls = [
                c for c in state.get("calls", [])
                if GATE_TOOLS.get(c.get("tool", "")) == tool_class
            ]
            if same_class_calls:
                last_call = same_class_calls[-1]
                delta = now - last_call.get("ts", now)
                if delta > MIN_BATCH_GAP:
                    # Sequential detected — inject receipt
                    last_summary = last_call.get("summary", "")
                    tool_input = data.get("tool_input") or {}
                    current_summary = (
                        tool_input.get("file_path")
                        or tool_input.get("command", "")[:60]
                        or ""
                    )
                    msg = (
                        f"[BEAST STRUCTURAL · parallelism] "
                        f"Sequential {tool_class} detected ({delta:.0f}s gap). "
                        f"Previous: {last_summary!r} — "
                        f"Current: {current_summary!r}. "
                        f"Batch remaining {tool_class} calls in this turn if independent."
                    )
                    print(json.dumps({"systemMessage": msg}))
                    gates_fired[tool_class] = True
                    state["gates_fired"] = gates_fired
                    save_state(session_id, state)

    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
