#!/usr/bin/env python3
"""
Beast Mode Correction Detector — UserPromptSubmit hook.

Detects when Adrian corrects the agent ("no, stop, you missed X, redo") and
labels the prior assistant turn's audit entry as a confirmed-leak candidate.

Output sinks (both written for redundancy + analytics):
  1. ~/.claude/beast-mode/calibration/corrections.jsonl  (single rolling file)
  2. ~/.claude/beast-mode/receipts/{YYYY-MM-DD}.jsonl     (typed receipt, type=calibration)

Linkage strategy:
  Reverse-tail drift.jsonl (~last 200 lines), find latest row with matching
  session_id. Compute prior_turn_id = SHA256(f"{session}:{ts}")[:12] via the
  shared receipt_store._turn_id helper.

Fail-open: any exception → exit 0. Never blocks user prompt.

Self-test:
  python3 ~/.claude/hooks/beast-correction-detector.py --self-test
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
LEDGER_PATH = HOME / ".claude" / "beast-mode" / "ledger" / "drift.jsonl"
CALIB_DIR = HOME / ".claude" / "beast-mode" / "calibration"
CALIB_PATH = CALIB_DIR / "corrections.jsonl"
ERROR_LOG = CALIB_DIR / "detector-errors.log"
RECEIPT_STORE_PATH = HOME / ".claude" / "beast-mode" / "bin" / "receipt_store.py"

SCHEMA_VERSION = 1
TAIL_LINES = 200          # reverse-scan window in drift.jsonl
MAX_PROMPT_LEN = 2000     # skip pathological inputs
MIN_PROMPT_LEN = 3
USER_QUOTE_CAP = 300

# Correction patterns. Order = precedence on multi-hit.
CORRECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("missed_object",
     re.compile(r"\byou\s+(missed|forgot|ignored|skipped|didn'?t)\b", re.IGNORECASE)),
    ("restate_intent",
     re.compile(r"\bi\s+(said|told\s+you|asked\s+for|wanted|meant)\b", re.IGNORECASE)),
    ("correction_imperative",
     re.compile(r"\b(undo|redo|revert|ignore\s+that|scratch\s+that)\b", re.IGNORECASE)),
    ("negative_eval",
     re.compile(r"\b(wrong|incorrect|that'?s\s+not|nope)\b", re.IGNORECASE)),
    ("negation_imperative",
     re.compile(r"^\s*(no|stop|wait|don'?t|do\s+not)\b", re.IGNORECASE)),
]

# Soft positive openers — kill obvious false positives like "no problem" / "thanks".
POSITIVE_ACK = re.compile(
    r"^\s*(thanks?|ok(?:ay)?|cool|nice|great|yes|yeah|good|perfect|sweet|sure|"
    r"no\s+(problem|worries))\b",
    re.IGNORECASE,
)

# Strong correction signals — override the soft positive ack.
STRONG_SIGNALS = re.compile(
    r"\b(missed|forgot|wrong|incorrect|undo|revert|nope|that'?s\s+not)\b",
    re.IGNORECASE,
)


def log_error(msg: str) -> None:
    try:
        CALIB_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except Exception:
        pass


def _load_receipt_store():
    """Import receipt_store via spec — same trick evolution.py uses."""
    if not RECEIPT_STORE_PATH.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("receipt_store", RECEIPT_STORE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        log_error(f"receipt_store import failed: {e}")
        return None


def match_correction(prompt: str) -> str | None:
    """Return bucket name or None."""
    if len(prompt) < MIN_PROMPT_LEN or len(prompt) > MAX_PROMPT_LEN:
        return None
    stripped = prompt.strip()
    if stripped.startswith("<system-reminder>") or stripped.startswith("<command-name>"):
        return None
    # Skip if positive ack AND no strong signal alongside
    if POSITIVE_ACK.match(stripped) and not STRONG_SIGNALS.search(stripped):
        return None
    for bucket, pattern in CORRECTION_PATTERNS:
        if pattern.search(stripped):
            return bucket
    return None


def find_prior_turn(session_id: str) -> dict | None:
    """Reverse-scan drift.jsonl tail for last row with matching session_id."""
    if not LEDGER_PATH.exists():
        return None
    try:
        with LEDGER_PATH.open("rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            # Read up to ~80KB from end (enough for 200 typical rows)
            read_bytes = min(file_size, 80_000)
            f.seek(file_size - read_bytes)
            chunk = f.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log_error(f"ledger read failed: {e}")
        return None

    lines = chunk.splitlines()
    # Trim partial first line (mid-row from seek)
    if file_size > 80_000 and lines:
        lines = lines[1:]
    # Walk from end
    for raw in reversed(lines[-TAIL_LINES:]):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("session") == session_id:
            return row
    return None


def build_entry(
    session_id: str,
    prompt: str,
    matched_pattern: str,
    prior_row: dict | None,
    turn_id_fn,
) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    user_quote = prompt.strip()[:USER_QUOTE_CAP]

    if prior_row is None:
        return {
            "ts": now_iso,
            "session": session_id,
            "prior_turn_id": None,
            "prior_audit_ts": None,
            "prior_audit_score": None,
            "prior_audit_leaks": [],
            "user_quote": user_quote,
            "matched_pattern": matched_pattern,
            "confirms_existing_leak": None,
            "confidence": "candidate",
            "schema_version": SCHEMA_VERSION,
            "notes": "no_prior_audit_yet",
        }

    prior_ts = prior_row.get("ts") or ""
    prior_session = prior_row.get("session") or session_id
    prior_dims = prior_row.get("dims") or {}
    leak_dims = sorted({d for d, v in prior_dims.items() if v == 0})
    explicit_leaks = prior_row.get("leaks") or []
    explicit_leak_dims = sorted({l.get("dim") for l in explicit_leaks if l.get("dim")})
    # Union — auditor may flag via dim score OR leak list separately
    all_leak_dims = sorted(set(leak_dims) | set(explicit_leak_dims))

    try:
        turn_id = turn_id_fn(prior_session, prior_ts) if turn_id_fn else None
    except Exception as e:
        log_error(f"turn_id compute failed: {e}")
        turn_id = None

    return {
        "ts": now_iso,
        "session": session_id,
        "prior_turn_id": turn_id,
        "prior_audit_ts": prior_ts or None,
        "prior_audit_score": prior_row.get("score"),
        "prior_audit_leaks": all_leak_dims,
        "user_quote": user_quote,
        "matched_pattern": matched_pattern,
        "confirms_existing_leak": bool(all_leak_dims),
        "confidence": "candidate",
        "schema_version": SCHEMA_VERSION,
    }


def write_corrections_jsonl(entry: dict) -> None:
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    with CALIB_PATH.open("a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def write_typed_receipt(entry: dict, receipt_store) -> None:
    if receipt_store is None:
        return
    receipt = {
        "ts": entry["ts"],
        "session": entry["session"],
        "receipt_type": "calibration",
        "matched_pattern": entry["matched_pattern"],
        "prior_turn_id": entry["prior_turn_id"],
        "prior_audit_score": entry["prior_audit_score"],
        "prior_audit_leaks": entry["prior_audit_leaks"],
        "confirms_existing_leak": entry["confirms_existing_leak"],
        "confidence": entry["confidence"],
        "user_quote": entry["user_quote"],
        "schema_version": SCHEMA_VERSION,
    }
    try:
        receipt_store.write_receipt(receipt)
    except Exception as e:
        log_error(f"typed receipt write failed: {e}")


# ─── Self-test ───────────────────────────────────────────────────────────────

SELF_TEST_CASES = [
    # (prompt, expected_bucket_or_None)
    ("no, you missed the auth case", "missed_object"),
    ("you forgot to handle the null case", "missed_object"),
    ("I said full scope", "restate_intent"),
    ("I asked for tests too", "restate_intent"),
    ("undo that change", "correction_imperative"),
    ("revert the last edit", "correction_imperative"),
    ("wrong approach", "negative_eval"),
    ("that's not what I meant", "restate_intent"),  # "I meant" wins on precedence — still a correction
    ("nope, try again", "negative_eval"),
    ("no don't do that", "negation_imperative"),
    ("stop, wait", "negation_imperative"),
    # Negatives
    ("thanks, looks good", None),
    ("ok cool", None),
    ("no problem, continue", None),
    ("no worries", None),
    ("yes that works", None),
    ("perfect", None),
    ("", None),
    ("hi", None),
    ("<system-reminder>blah</system-reminder>", None),
    # Strong override of positive ack
    ("no problem but you missed the edge case", "missed_object"),
]


def run_self_test() -> int:
    fail = 0
    for prompt, expected in SELF_TEST_CASES:
        got = match_correction(prompt)
        status = "OK" if got == expected else "FAIL"
        if got != expected:
            fail += 1
        print(f"[{status}] {prompt!r:60s} → {got} (expected {expected})")
    print(f"\n{len(SELF_TEST_CASES) - fail}/{len(SELF_TEST_CASES)} passed")
    return 0 if fail == 0 else 1


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    if "--self-test" in sys.argv:
        return run_self_test()

    # Recursion guards — same env vars as Stop hook
    if os.environ.get("BEAST_MODE_AUDITOR_RUNNING") == "1":
        return 0
    if os.environ.get("BEAST_MODE_INTERPRETER_RUNNING") == "1":
        return 0

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    session_id = data.get("session_id") or ""
    prompt = data.get("prompt") or ""
    if not session_id or not prompt:
        return 0

    try:
        bucket = match_correction(prompt)
        if not bucket:
            return 0

        receipt_store = _load_receipt_store()
        turn_id_fn = getattr(receipt_store, "_turn_id", None) if receipt_store else None

        prior_row = find_prior_turn(session_id)
        entry = build_entry(session_id, prompt, bucket, prior_row, turn_id_fn)

        write_corrections_jsonl(entry)
        write_typed_receipt(entry, receipt_store)
    except Exception as e:
        log_error(f"main failed: {e}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
