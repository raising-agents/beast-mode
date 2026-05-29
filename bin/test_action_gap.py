#!/usr/bin/env python3
"""
WP-8 self-test — action-gap gate hook.

No pytest. Isolated env per test via temp dirs + monkey-patched module paths.

Run: /opt/miniconda3/bin/python ~/.claude/beast-mode/bin/test_action_gap.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOOK_PATH = Path.home() / ".claude" / "hooks" / "beast-action-gap-gate.py"

import importlib.util
_spec = importlib.util.spec_from_file_location("beast_action_gap", HOOK_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}  {detail}")


class IsolatedEnv:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-wp8-"))

    def __enter__(self):
        rules = self.tmp / "rules"
        state_dir = self.tmp / "state"
        for d in (rules, state_dir):
            d.mkdir(parents=True)
        self._save = {
            "RULES_DIR": gate.RULES_DIR,
            "EVENTS_PATH": gate.EVENTS_PATH,
            "ERROR_LOG": gate.ERROR_LOG,
            "STATE_DIR": gate.STATE_DIR,
        }
        gate.RULES_DIR = rules
        gate.EVENTS_PATH = rules / "action-gap-events.jsonl"
        gate.ERROR_LOG = rules / "action-gap-errors.log"
        gate.STATE_DIR = state_dir
        return self.tmp

    def __exit__(self, *exc):
        for k, v in self._save.items():
            setattr(gate, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)


def run_hook(stdin_obj: dict) -> tuple[int, str]:
    """Invoke gate.main() with redirected stdin/stdout."""
    import io
    old_in = sys.stdin
    old_out = sys.stdout
    sys.stdin = io.StringIO(json.dumps(stdin_obj))
    out = io.StringIO()
    sys.stdout = out
    try:
        ec = gate.main()
    except SystemExit as e:
        ec = e.code
    finally:
        sys.stdin = old_in
        sys.stdout = old_out
    return ec, out.getvalue()


def write_transcript(path: Path, assistant_text: str) -> None:
    with path.open("w") as f:
        f.write(json.dumps({"role": "user", "content": "go"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": assistant_text}) + "\n")


def write_state(state_dir: Path, session_id: str, calls_ago_secs: list[float],
                gates_fired: dict | None = None) -> None:
    """Write structural state. calls_ago_secs is list of how-many-seconds-ago each call was."""
    now = time.time()
    state = {
        "session_id": session_id,
        "calls": [{"tool": "Bash", "ts": now - secs, "summary": "x"}
                  for secs in calls_ago_secs],
        "turn_seq": 1,
    }
    if gates_fired:
        state["gates_fired"] = gates_fired
    (state_dir / f"beast-struct-{session_id}.json").write_text(
        json.dumps(state, separators=(",", ":"))
    )


# ─── tests ───────────────────────────────────────────────────────────────────

def test_intent_no_tool_call_fires():
    print("\n[test] intent phrase + no tool call → gate fires")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        write_transcript(transcript,
                          "I'll look at the data first. " * 5 + "Let me check the config.")
        # No state file = no tool calls
        ec, out = run_hook({"session_id": "s1", "transcript_path": str(transcript)})
        check("exit 0", ec == 0)
        events = (tmp / "rules" / "action-gap-events.jsonl")
        check("event logged", events.exists())
        if events.exists():
            ev = json.loads(events.read_text().splitlines()[-1])
            check("intent_phrase captured",
                  "let me" in ev["intent_phrase"].lower(),
                  f"got {ev['intent_phrase']!r}")
            check("schema_version=1", ev["schema_version"] == 1)
            check("session matches", ev["session"] == "s1")
        # systemMessage emitted to stdout
        check("systemMessage emitted", "action_gap" in out)


def test_intent_with_recent_tool_call_no_fire():
    print("\n[test] intent phrase + recent tool call → no fire")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        write_transcript(transcript,
                          "Reading the config now. " * 5 + "Let me check the value.")
        # Tool call 10 seconds ago (inside the 60s lookback)
        write_state(tmp / "state", "s2", [10.0])
        ec, out = run_hook({"session_id": "s2", "transcript_path": str(transcript)})
        check("exit 0", ec == 0)
        events = (tmp / "rules" / "action-gap-events.jsonl")
        check("no event logged", not events.exists() or events.stat().st_size == 0)
        check("no systemMessage", "action_gap" not in out)


def test_intent_with_old_tool_call_fires():
    print("\n[test] intent phrase + tool call >60s ago → gate fires")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        write_transcript(transcript,
                          "previous turn output. " * 5 + "Now I'll start over.")
        # Tool call 120s ago (outside lookback)
        write_state(tmp / "state", "s3", [120.0])
        ec, out = run_hook({"session_id": "s3", "transcript_path": str(transcript)})
        events = (tmp / "rules" / "action-gap-events.jsonl")
        check("event logged", events.exists())
        if events.exists():
            ev = json.loads(events.read_text().splitlines()[-1])
            check("last_tool_call_age >60s", ev.get("last_tool_call_age_secs", 0) >= 60)


def test_no_intent_no_fire():
    print("\n[test] no intent phrase → no fire")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        write_transcript(transcript, "Config is fine. No changes needed. Done." * 3)
        ec, out = run_hook({"session_id": "s4", "transcript_path": str(transcript)})
        events = (tmp / "rules" / "action-gap-events.jsonl")
        check("no event logged",
              not events.exists() or events.stat().st_size == 0)


def test_short_text_no_fire():
    print("\n[test] response too short (<50 chars) → no fire")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        write_transcript(transcript, "Let me check.")  # only 13 chars
        ec, out = run_hook({"session_id": "s5", "transcript_path": str(transcript)})
        events = (tmp / "rules" / "action-gap-events.jsonl")
        check("no event logged",
              not events.exists() or events.stat().st_size == 0)


def test_fire_once_per_session():
    print("\n[test] gate fires at most once per session")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        write_transcript(transcript,
                          "more text. " * 5 + "Let me check the file again.")
        # First fire
        run_hook({"session_id": "s6", "transcript_path": str(transcript)})
        # Second fire — should be deduped
        ec, out = run_hook({"session_id": "s6", "transcript_path": str(transcript)})
        events = (tmp / "rules" / "action-gap-events.jsonl")
        if events.exists():
            n = len(events.read_text().strip().splitlines())
            check("exactly 1 event despite 2 calls", n == 1, f"got {n}")
        check("second invocation silent stdout", "action_gap" not in out)


def test_recursion_guard():
    print("\n[test] recursion guard BEAST_MODE_AUDITOR_RUNNING=1 → exit silently")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        write_transcript(transcript, "Let me check this. " * 10)
        os.environ["BEAST_MODE_AUDITOR_RUNNING"] = "1"
        try:
            ec, out = run_hook({"session_id": "s7", "transcript_path": str(transcript)})
            check("exit 0", ec == 0)
            check("stdout empty", out == "")
            events = (tmp / "rules" / "action-gap-events.jsonl")
            check("no event logged",
                  not events.exists() or events.stat().st_size == 0)
        finally:
            del os.environ["BEAST_MODE_AUDITOR_RUNNING"]


def test_disable_env():
    print("\n[test] BEAST_ACTION_GAP_DISABLED=1 → silent")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        write_transcript(transcript, "Let me check the data " * 10)
        os.environ["BEAST_ACTION_GAP_DISABLED"] = "1"
        try:
            ec, out = run_hook({"session_id": "s8", "transcript_path": str(transcript)})
            check("exit 0", ec == 0)
            check("no event", "action_gap" not in out)
        finally:
            del os.environ["BEAST_ACTION_GAP_DISABLED"]


def test_malformed_transcript():
    print("\n[test] malformed transcript → fail-open exit 0")
    with IsolatedEnv() as tmp:
        transcript = tmp / "broken.jsonl"
        transcript.write_text("not json\n{also bad\n")
        ec, out = run_hook({"session_id": "s9", "transcript_path": str(transcript)})
        check("exit 0", ec == 0)


def test_missing_session():
    print("\n[test] missing session_id → exit 0")
    with IsolatedEnv():
        ec, out = run_hook({"transcript_path": "/tmp/x"})
        check("exit 0", ec == 0)
        check("stdout empty", out == "")


def test_intent_at_response_middle_not_end():
    print("\n[test] intent phrase in middle of response (outside tail) → no fire")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        # intent at beginning, then 500+ chars of other text → won't be in tail
        prefix = "Let me check the config. " + ("Found the issue. Fixed. ") * 30
        suffix = "All tests pass. Done."
        write_transcript(transcript, prefix + suffix)
        ec, out = run_hook({"session_id": "s10", "transcript_path": str(transcript)})
        events = (tmp / "rules" / "action-gap-events.jsonl")
        check("no event (intent outside tail)",
              not events.exists() or events.stat().st_size == 0)


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("WP-8 — action-gap gate self-test")
    print("=" * 60)
    test_intent_no_tool_call_fires()
    test_intent_with_recent_tool_call_no_fire()
    test_intent_with_old_tool_call_fires()
    test_no_intent_no_fire()
    test_short_text_no_fire()
    test_fire_once_per_session()
    test_recursion_guard()
    test_disable_env()
    test_malformed_transcript()
    test_missing_session()
    test_intent_at_response_middle_not_end()
    print()
    print("=" * 60)
    total = PASS + FAIL
    print(f"Result: {PASS}/{total} passed, {FAIL} failed")
    if FAIL:
        print("Failures:")
        for t in FAILED:
            print(f"  - {t}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
