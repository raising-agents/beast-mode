#!/usr/bin/env python3
"""
WP-7 self-test — auditor v2 kernel + structural verification detector.

No pytest. Tests cover:
  - CLAIM_PHRASES regex matrix (positive + negative)
  - _detect_claim_phrases tail behavior
  - compute_structural_dims verification_by_evidence scoring
  - KERNEL JSON schema validates with 6 keys
  - trivial-response fallback has 6 null keys
  - DIM_KEYS_V2 tuple integrity

Run: /opt/miniconda3/bin/python ~/.claude/beast-mode/bin/test_auditor_kernel.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

AUDITOR_PATH = Path(__file__).parent / "auditor-worker.py"
_spec = importlib.util.spec_from_file_location("aw", AUDITOR_PATH)
aw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aw)


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


# ─── DIM_KEYS_V2 integrity ───────────────────────────────────────────────────

def test_dim_keys():
    print("\n[test] DIM_KEYS_V2 contains exactly the 6 core dims")
    expected = {"parallelism", "action_over_announcement", "verification_by_evidence",
                "scope", "depth", "boldness"}
    check("set equality", set(aw.DIM_KEYS_V2) == expected,
          f"got {set(aw.DIM_KEYS_V2)}")
    check("len == 6", len(aw.DIM_KEYS_V2) == 6)
    # Retired dims absent
    for retired in ("sequencing", "deferrals", "block_breaking", "self_direction_over_ask"):
        check(f"retired {retired!r} absent", retired not in aw.DIM_KEYS_V2)


# ─── CLAIM_PHRASES regex matrix ──────────────────────────────────────────────

def test_claim_phrases_positive():
    print("\n[test] CLAIM_PHRASES matches claim phrases")
    positives = [
        "Done.",
        "the tests pass",
        "all tests pass",
        "Fixed.",
        "I verified the output",
        "everything works correctly",
        "found the issue",
        "complete",
        "approved.",
        "passing on main",
    ]
    for s in positives:
        m = aw.CLAIM_PHRASES.search(s)
        check(f"{s!r} matches", m is not None,
              f"no match in {s!r}")


def test_claim_phrases_negative():
    print("\n[test] CLAIM_PHRASES does NOT match non-claims")
    negatives = [
        "Plan: do X, then Y",
        "I will check the file",
        "should I proceed?",
        "this is the implementation",
        "let me explain",
        "the design",
        "hello there",
    ]
    for s in negatives:
        m = aw.CLAIM_PHRASES.search(s)
        check(f"{s!r} does not match", m is None,
              f"unexpected match: {m.group(0) if m else None}")


# ─── _detect_claim_phrases tail behavior ─────────────────────────────────────

def test_detect_tail():
    print("\n[test] _detect_claim_phrases scans only tail")
    # claim at very beginning, lots of filler after
    text = "Fixed. " + ("filler " * 200) + " end."
    phrase, pos = aw._detect_claim_phrases(text)
    check("claim outside tail not detected", phrase is None,
          f"got {phrase!r}")

    # claim at end
    text2 = ("filler " * 50) + " all tests pass."
    phrase, pos = aw._detect_claim_phrases(text2)
    check("claim in tail detected", phrase is not None)
    check("position is 'tail'", pos == "tail")

    # empty text
    phrase, pos = aw._detect_claim_phrases("")
    check("empty text → None", phrase is None)


# ─── compute_structural_dims verification scoring ────────────────────────────

def test_verification_score_1():
    print("\n[test] verification_by_evidence=1 (claim + recent tool call)")
    now = time.time()
    state = {
        "session_id": "t1",
        "calls": [
            {"tool": "Bash", "ts": now - 5.0, "summary": "ran tests"},
        ],
        "turn_seq": 1,
    }
    text = "Patched the bug. " + ("output " * 30) + "All tests pass."
    dims, ev = aw.compute_structural_dims(state, text)
    ver = dims.get("verification_by_evidence", {})
    check("dim present", ver != {})
    check("score == 1", ver.get("score") == 1, f"got {ver.get('score')}")
    check("method structural", ver.get("method") == "structural")
    check("evidence has claim_phrase",
          ver.get("evidence", {}).get("claim_phrase") is not None)
    check("last_tool_call_age <= 30s",
          (ver.get("evidence", {}).get("last_tool_call_age_secs") or 999) <= 30)


def test_verification_score_0():
    print("\n[test] verification_by_evidence=0 (claim + no recent tool call)")
    # state with no calls
    state = {"session_id": "t2", "calls": [], "turn_seq": 1}
    text = "Patched the bug. " + ("output " * 30) + "Fixed."
    dims, ev = aw.compute_structural_dims(state, text)
    ver = dims.get("verification_by_evidence", {})
    check("score == 0", ver.get("score") == 0, f"got {ver.get('score')}")
    check("claim_phrase still captured",
          ver.get("evidence", {}).get("claim_phrase") is not None)
    check("last_tool_call_age is None",
          ver.get("evidence", {}).get("last_tool_call_age_secs") is None)


def test_verification_no_claim_phrase():
    print("\n[test] verification_by_evidence=None (no claim phrase)")
    now = time.time()
    state = {
        "session_id": "t3",
        "calls": [{"tool": "Bash", "ts": now - 2.0, "summary": "x"}],
        "turn_seq": 1,
    }
    text = "I will review the design. Plan: implement A then B."
    dims, ev = aw.compute_structural_dims(state, text)
    ver = dims.get("verification_by_evidence", {})
    check("score is None", ver.get("score") is None, f"got {ver.get('score')}")


def test_verification_tool_call_too_old():
    print("\n[test] verification_by_evidence=0 (tool call >30s ago)")
    now = time.time()
    state = {
        "session_id": "t4",
        "calls": [{"tool": "Bash", "ts": now - 120.0, "summary": "x"}],
        "turn_seq": 1,
    }
    text = "Plan executed. " + ("filler " * 30) + "Done."
    dims, ev = aw.compute_structural_dims(state, text)
    ver = dims.get("verification_by_evidence", {})
    check("score == 0 (window expired)", ver.get("score") == 0,
          f"got {ver.get('score')}")


# ─── KERNEL JSON schema ──────────────────────────────────────────────────────

def test_kernel_trivial_fallback_json():
    print("\n[test] KERNEL trivial-response fallback parses with 6 dims")
    # extract last line that looks like a complete JSON object
    lines = aw.KERNEL.splitlines()
    found = None
    for l in lines:
        s = l.strip()
        if s.startswith("{") and s.endswith("}") and '"dims"' in s and '"trivial"' in s:
            found = s
            break
    check("trivial example present in KERNEL", found is not None)
    if found:
        try:
            obj = json.loads(found)
            check("parses as JSON", True)
            check("dims has 6 keys", len(obj["dims"]) == 6, f"got {len(obj['dims'])}")
            check("all values null", all(v is None for v in obj["dims"].values()))
            check("matches DIM_KEYS_V2",
                  set(obj["dims"].keys()) == set(aw.DIM_KEYS_V2))
        except json.JSONDecodeError as e:
            check("parses as JSON", False, str(e))


def test_kernel_no_retired_dims():
    print("\n[test] KERNEL string does not mention retired dim IDs")
    for retired in ("sequencing", "deferrals", "block_breaking", "self_direction_over_ask"):
        check(f"{retired!r} absent from KERNEL",
              retired not in aw.KERNEL)


def test_kernel_mentions_v2_dims():
    print("\n[test] KERNEL string defines all 6 v2 dims")
    for k in aw.DIM_KEYS_V2:
        check(f"{k!r} present in KERNEL", k in aw.KERNEL)


# ─── compute_score works with v2 6-key dim dicts ─────────────────────────────

def test_compute_score_v2_shape():
    print("\n[test] compute_score handles v2 6-dim dicts")
    dims_all_beast = {k: 1 for k in aw.DIM_KEYS_V2}
    check("6/6", aw.compute_score(dims_all_beast) == "6/6")

    dims_half = {"parallelism": 1, "scope": 0, "depth": 1, "boldness": 0,
                 "action_over_announcement": 1, "verification_by_evidence": None}
    check("3/5 (one N/A)", aw.compute_score(dims_half) == "3/5")

    dims_all_na = {k: None for k in aw.DIM_KEYS_V2}
    check("N/A", aw.compute_score(dims_all_na) == "N/A")


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("WP-7 — auditor v2 kernel + structural verification self-test")
    print("=" * 60)
    test_dim_keys()
    test_claim_phrases_positive()
    test_claim_phrases_negative()
    test_detect_tail()
    test_verification_score_1()
    test_verification_score_0()
    test_verification_no_claim_phrase()
    test_verification_tool_call_too_old()
    test_kernel_trivial_fallback_json()
    test_kernel_no_retired_dims()
    test_kernel_mentions_v2_dims()
    test_compute_score_v2_shape()
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
