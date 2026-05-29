#!/usr/bin/env python3
"""
Beast Mode Blocklist — self-test suite.

No pytest. Pure stdlib + yaml + temp dirs. Exercises:
  - YAML round-trip
  - normalize()
  - merge_clusters() with mocked Haiku output
  - auto_promote / auto_retire sweeps
  - log event validation
  - manager CLI integration (seed, list, promote, retire, log)

Run: /opt/miniconda3/bin/python ~/.claude/beast-mode/bin/test_blocklist.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Make blocklist_manager + blocklist_promoter importable
BIN_DIR = Path(__file__).parent
sys.path.insert(0, str(BIN_DIR))

PYTHON = "/opt/miniconda3/bin/python"
MANAGER = str(BIN_DIR / "blocklist_manager.py")
PROMOTER = str(BIN_DIR / "blocklist_promoter.py")


PASS = 0
FAIL = 0
FAILED_TESTS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        FAILED_TESTS.append(label)
        print(f"  [FAIL] {label}  {detail}")


# ─── isolated env per test ────────────────────────────────────────────────────

class IsolatedRules:
    """Redirect rules dir to a temp location for the duration of the context."""
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-test-"))
        self.rules = self.tmp / "rules"
        self.original = None

    def __enter__(self):
        import blocklist_manager as bm
        self.original_rules_dir = bm.RULES_DIR
        self.original_blocklist = bm.BLOCKLIST_PATH
        self.original_log = bm.LOG_PATH
        bm.RULES_DIR = self.rules
        bm.BLOCKLIST_PATH = self.rules / "blocklist.yaml"
        bm.LOG_PATH = self.rules / "blocklist-log.jsonl"
        return self.rules

    def __exit__(self, *exc):
        import blocklist_manager as bm
        bm.RULES_DIR = self.original_rules_dir
        bm.BLOCKLIST_PATH = self.original_blocklist
        bm.LOG_PATH = self.original_log
        shutil.rmtree(self.tmp, ignore_errors=True)


# ─── unit: normalize ──────────────────────────────────────────────────────────

def test_normalize():
    print("\n[test] normalize()")
    from blocklist_promoter import normalize
    fixtures = [
        ("Let me check", "let me check"),
        ("  for now.  ", "for now"),
        ("`MVP first`!", "mvp first"),
        ('"let\'s start with a basic version"', "let's start with a basic version"),
        ("Now\n\nI'll  proceed", "now i'll proceed"),
        ("...", ""),
    ]
    for raw, expected in fixtures:
        got = normalize(raw)
        check(f"normalize({raw!r}) == {expected!r}", got == expected, f"got={got!r}")


# ─── unit: log validation ────────────────────────────────────────────────────

def test_log_validation():
    print("\n[test] log append vocabulary validation")
    with IsolatedRules():
        import blocklist_manager as bm
        # valid event
        try:
            bm.log_append("candidate_added", "ph_test01", "promoter", {"x": 1})
            check("valid event accepted", True)
        except ValueError as e:
            check("valid event accepted", False, str(e))
        # invalid event
        try:
            bm.log_append("nonsense_event", "ph_test01", "promoter")
            check("invalid event rejected", False, "no exception")
        except ValueError:
            check("invalid event rejected", True)
        # invalid actor
        try:
            bm.log_append("candidate_added", "ph_test01", "nonsense_actor")
            check("invalid actor rejected", False, "no exception")
        except ValueError:
            check("invalid actor rejected", True)


# ─── unit: yaml roundtrip ────────────────────────────────────────────────────

def test_yaml_roundtrip():
    print("\n[test] YAML roundtrip via load/save")
    with IsolatedRules():
        import blocklist_manager as bm
        sample = {
            "schema_version": 2,
            "updated_ts": None,
            "phrases": [
                {"id": "ph_aaa111", "phrase": "let me check", "status": "candidate",
                 "examples": ["Let me check", "let me check it"],
                 "dim_primary": "action_over_announcement",
                 "dim_counts": {"action_over_announcement": 3},
                 "hits": 3, "source": "haiku"},
            ],
        }
        bm.save_blocklist(sample)
        loaded = bm.load_blocklist()
        check("schema_version preserved", loaded["schema_version"] == 2)
        check("phrase list length preserved", len(loaded["phrases"]) == 1)
        check("phrase content preserved",
              loaded["phrases"][0]["phrase"] == "let me check")
        check("updated_ts written", loaded["updated_ts"] is not None)


# ─── unit: merge_clusters ────────────────────────────────────────────────────

def test_merge_clusters_add_and_update():
    print("\n[test] merge_clusters add + update")
    with IsolatedRules():
        import blocklist_manager as bm
        import blocklist_promoter as bp
        data = {"schema_version": 2, "updated_ts": None, "phrases": []}

        haiku_resp = {
            "clusters": [
                {
                    "label": "action_gap_let_me",
                    "representative_phrase": "let me check",
                    "rationale": "Action-gap antipattern",
                    "dim_primary": "action_over_announcement",
                    "examples": ["Let me check", "let me check it", "Let me check the file",
                                 "let me see", "let me look"],
                },
                {
                    "label": "small_cluster_filtered_out",
                    "representative_phrase": "for now",
                    "rationale": "...",
                    "dim_primary": "deferrals",
                    "examples": ["for now", "for now."],  # only 2 — below min 5
                },
            ]
        }

        added, updated = bp.merge_clusters(data, haiku_resp["clusters"],
                                           min_cluster_size=5, dry_run=False)
        check("merge added 1 (large cluster)", added == 1, f"added={added}")
        check("merge filtered out small cluster", updated == 0, f"updated={updated}")
        check("phrase count = 1", len(data["phrases"]) == 1)
        first = data["phrases"][0]
        check("phrase is representative", first["phrase"] == "let me check")
        check("source is haiku", first["source"] == "haiku")
        check("status is candidate", first["status"] == "candidate")

        # rerun same input → no add, 1 updated
        added2, updated2 = bp.merge_clusters(data, haiku_resp["clusters"],
                                             min_cluster_size=5, dry_run=False)
        check("rerun: 0 new", added2 == 0, f"added2={added2}")
        check("rerun: 1 updated", updated2 == 1, f"updated2={updated2}")


# ─── unit: auto_promote ──────────────────────────────────────────────────────

def test_auto_promote():
    print("\n[test] auto_promote sweep")
    with IsolatedRules():
        import blocklist_manager as bm
        import blocklist_promoter as bp
        # 8 days old candidate → should promote
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        # 2 days old candidate → should NOT promote
        new_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        data = {
            "schema_version": 2, "updated_ts": None,
            "phrases": [
                {"id": "ph_old1", "phrase": "old", "status": "candidate",
                 "first_seen_ts": old_ts, "source": "haiku", "examples": [], "hits": 5},
                {"id": "ph_new1", "phrase": "new", "status": "candidate",
                 "first_seen_ts": new_ts, "source": "haiku", "examples": [], "hits": 5},
            ],
        }
        # need a real log file for log_filter
        bm.save_blocklist(data)
        promoted = bp.auto_promote(data, age_days=7, dry_run=False)
        check("auto_promote count == 1", promoted == 1, f"promoted={promoted}")
        by_id = {p["id"]: p for p in data["phrases"]}
        check("old candidate promoted", by_id["ph_old1"]["status"] == "active")
        check("new candidate stays candidate", by_id["ph_new1"]["status"] == "candidate")
        check("promoted_ts set on old", by_id["ph_old1"].get("promoted_ts") is not None)


def test_auto_promote_respects_manual_retire():
    print("\n[test] auto_promote respects prior manual_retired event")
    with IsolatedRules():
        import blocklist_manager as bm
        import blocklist_promoter as bp
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        data = {
            "schema_version": 2, "updated_ts": None,
            "phrases": [
                {"id": "ph_revived", "phrase": "x", "status": "candidate",
                 "first_seen_ts": old_ts, "source": "haiku", "examples": [], "hits": 5},
            ],
        }
        bm.save_blocklist(data)
        # log a manual_retired event in the past
        bm.log_append("manual_retired", "ph_revived", "adrian", {"reason": "test"})
        # then revived (back to candidate but with retire history)
        promoted = bp.auto_promote(data, age_days=7, dry_run=False)
        check("revived entry NOT auto-promoted", promoted == 0,
              f"promoted={promoted}")
        check("status still candidate", data["phrases"][0]["status"] == "candidate")


# ─── unit: decay sweep ───────────────────────────────────────────────────────

def test_decay_sweep():
    print("\n[test] decay_sweep")
    with IsolatedRules():
        import blocklist_manager as bm
        import blocklist_promoter as bp
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        data = {
            "schema_version": 2, "updated_ts": None,
            "phrases": [
                {"id": "ph_stale1", "phrase": "stale", "status": "active",
                 "last_triggered_ts": None, "last_seen_ts": old_ts, "source": "haiku",
                 "examples": [], "hits": 5},
                {"id": "ph_active1", "phrase": "active", "status": "active",
                 "last_triggered_ts": recent_ts, "last_seen_ts": recent_ts, "source": "haiku",
                 "examples": [], "hits": 5},
            ],
        }
        bm.save_blocklist(data)
        retired = bp.decay_sweep(data, decay_days=28, dry_run=False)
        check("decay retired 1", retired == 1, f"retired={retired}")
        by_id = {p["id"]: p for p in data["phrases"]}
        check("stale -> retired", by_id["ph_stale1"]["status"] == "retired")
        check("recent -> still active", by_id["ph_active1"]["status"] == "active")


# ─── unit: dry_run does not write ─────────────────────────────────────────────

def test_dry_run_no_writes():
    print("\n[test] dry_run does not write log or yaml")
    with IsolatedRules() as rules:
        import blocklist_manager as bm
        import blocklist_promoter as bp
        bm.save_blocklist({"schema_version": 2, "updated_ts": None, "phrases": []})
        log_path = bm.LOG_PATH
        log_size_before = log_path.stat().st_size if log_path.exists() else 0

        clusters = [{
            "label": "test_cluster",
            "representative_phrase": "test phrase",
            "rationale": "...",
            "dim_primary": "scope",
            "examples": ["a", "b", "c", "d", "e"],
        }]
        data = bm.load_blocklist()
        added, _ = bp.merge_clusters(data, clusters, min_cluster_size=5, dry_run=True)
        check("dry_run merge_clusters returns count", added == 1)
        # In dry_run we don't append to log
        log_size_after = log_path.stat().st_size if log_path.exists() else 0
        check("dry_run log NOT appended", log_size_after == log_size_before,
              f"before={log_size_before} after={log_size_after}")


# ─── integration: full manager CLI flow ──────────────────────────────────────

def test_cli_integration():
    print("\n[test] CLI integration (seed → list → log)")
    tmp = Path(tempfile.mkdtemp(prefix="bm-cli-"))
    env = os.environ.copy()
    env["HOME"] = str(tmp)

    def run(*args, check_zero=True):
        r = subprocess.run([PYTHON, MANAGER, *args],
                           capture_output=True, text=True, env=env)
        if check_zero and r.returncode != 0:
            return r, False
        return r, True

    try:
        r, ok = run("seed")
        check("CLI seed exit 0", ok and r.returncode == 0, r.stderr)
        check("CLI seed creates yaml",
              (tmp / ".claude/beast-mode/rules/blocklist.yaml").exists())
        check("CLI seed creates log",
              (tmp / ".claude/beast-mode/rules/blocklist-log.jsonl").exists())

        r, _ = run("seed", check_zero=False)
        check("CLI second seed refuses", r.returncode != 0)

        r, ok = run("list")
        check("CLI list on empty works", ok)
        check("CLI list shows empty marker", "no matching phrases" in r.stdout)

        # add manual phrase
        r, ok = run("add", "for now", "--dim", "deferrals")
        check("CLI add exit 0", ok, r.stderr)

        r, ok = run("list", "--source", "manual")
        check("CLI list filters by source", ok and "for now" in r.stdout)

        # log tail
        r, ok = run("log", "tail", "--n", "5", "--format", "jsonl")
        check("CLI log tail jsonl works", ok)
        lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
        check("log has >= 2 events (seed + add)", len(lines) >= 2,
              f"lines={len(lines)}")

        # log append with bad event
        r = subprocess.run([PYTHON, MANAGER, "log", "append",
                            "--event", "garbage", "--actor", "agent"],
                           capture_output=True, text=True, env=env)
        check("CLI log append rejects bad event", r.returncode != 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("Beast Mode Blocklist — self-test")
    print("=" * 60)
    test_normalize()
    test_log_validation()
    test_yaml_roundtrip()
    test_merge_clusters_add_and_update()
    test_auto_promote()
    test_auto_promote_respects_manual_retire()
    test_decay_sweep()
    test_dry_run_no_writes()
    test_cli_integration()
    print()
    print("=" * 60)
    total = PASS + FAIL
    print(f"Result: {PASS}/{total} passed, {FAIL} failed")
    if FAIL:
        print("Failures:")
        for t in FAILED_TESTS:
            print(f"  - {t}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
