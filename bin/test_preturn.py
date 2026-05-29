#!/usr/bin/env python3
"""
WP-3 self-test: pre-turn hook + promoter apply_triggers patch.

No pytest. Pure stdlib + isolated temp dirs.

Run: /opt/miniconda3/bin/python ~/.claude/beast-mode/bin/test_preturn.py
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

# Make hook + bin modules importable
HOOK_PATH = Path.home() / ".claude" / "hooks" / "beast-pre-turn.py"
BIN_DIR = Path(__file__).parent
sys.path.insert(0, str(BIN_DIR))

# Import hook module via spec (it's a .py file, not on path)
import importlib.util
_spec = importlib.util.spec_from_file_location("beast_preturn", HOOK_PATH)
preturn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preturn)

import blocklist_manager as bm  # noqa: E402
import blocklist_promoter as bp  # noqa: E402

PYTHON = "/opt/miniconda3/bin/python"


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
    """Redirect all hook + manager + promoter file paths to a temp dir."""
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-wp3-"))

    def __enter__(self):
        rules = self.tmp / "rules"
        ledger_dir = self.tmp / "ledger"
        rules.mkdir(parents=True)
        ledger_dir.mkdir(parents=True)
        # Save originals
        self._save = {
            "preturn_RULES_DIR": preturn.RULES_DIR,
            "preturn_BLOCKLIST_PATH": preturn.BLOCKLIST_PATH,
            "preturn_BLOCKLIST_LOG": preturn.BLOCKLIST_LOG,
            "preturn_INJECTIONS_LOG": preturn.INJECTIONS_LOG,
            "preturn_ERROR_LOG": preturn.ERROR_LOG,
            "preturn_LEDGER_PATH": preturn.LEDGER_PATH,
            "bm_RULES_DIR": bm.RULES_DIR,
            "bm_BLOCKLIST_PATH": bm.BLOCKLIST_PATH,
            "bm_LOG_PATH": bm.LOG_PATH,
            "bp_WATERMARK_PATH": bp.WATERMARK_PATH,
        }
        # Patch
        preturn.RULES_DIR = rules
        preturn.BLOCKLIST_PATH = rules / "blocklist.yaml"
        preturn.BLOCKLIST_LOG = rules / "blocklist-log.jsonl"
        preturn.INJECTIONS_LOG = rules / "preturn-injections.jsonl"
        preturn.ERROR_LOG = rules / "preturn-errors.log"
        preturn.LEDGER_PATH = ledger_dir / "drift.jsonl"
        bm.RULES_DIR = rules
        bm.BLOCKLIST_PATH = rules / "blocklist.yaml"
        bm.LOG_PATH = rules / "blocklist-log.jsonl"
        bp.WATERMARK_PATH = rules / ".triggers-watermark"
        return self.tmp

    def __exit__(self, *exc):
        for k, v in self._save.items():
            mod, attr = k.split("_", 1)
            if mod == "preturn":
                setattr(preturn, attr, v)
            elif mod == "bm":
                setattr(bm, attr, v)
            elif mod == "bp":
                setattr(bp, attr, v)
        shutil.rmtree(self.tmp, ignore_errors=True)


def seed_ledger(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def make_ledger_row(ts: str, dims: dict, leaks: list[dict] | None = None,
                    session: str = "s1") -> dict:
    return {
        "ts": ts, "session": session, "score": "x/x",
        "dims": dims, "leaks": leaks or [],
    }


def make_blocklist(rules_dir: Path, phrases: list[dict]) -> None:
    bm.save_blocklist({"schema_version": 2, "updated_ts": None, "phrases": phrases})


def make_transcript(path: Path, asst_text: str) -> None:
    with path.open("w") as f:
        f.write(json.dumps({"role": "user", "content": "hi"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": asst_text}) + "\n")


def run_hook(stdin_obj: dict) -> tuple[int, str, str]:
    """Invoke the in-process main() with stdin redirected. Returns (exit, stdout, stderr)."""
    import io, contextlib
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    sys.stdin = io.StringIO(json.dumps(stdin_obj))
    out = io.StringIO()
    sys.stdout = out
    try:
        ec = preturn.main()
    except SystemExit as e:
        ec = e.code
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout
    return ec, out.getvalue(), ""


# ─── tests ────────────────────────────────────────────────────────────────────

def test_idle_skip():
    print("\n[test] idle skip when nothing to inject")
    with IsolatedEnv() as tmp:
        make_blocklist(tmp / "rules", [])  # empty
        # no ledger file at all
        ec, out, _ = run_hook({"session_id": "s1", "prompt": "x", "transcript_path": ""})
        check("exit 0", ec == 0)
        check("stdout empty", out == "")
        log_path = tmp / "rules" / "preturn-injections.jsonl"
        check("audit row written", log_path.exists())
        last = json.loads(log_path.read_text().splitlines()[-1])
        check("injected false", last["injected"] is False)
        check("skipped reason nothing_to_inject",
              last["skipped_reason"] == "nothing_to_inject")


def test_dim_block_rendered():
    print("\n[test] dim block rendered when ledger has leaks")
    with IsolatedEnv() as tmp:
        ledger = tmp / "ledger" / "drift.jsonl"
        # 15 rows scope=0 + 5 rows scope=1 → 15/20 leak rate on scope (75%)
        now = datetime.now(timezone.utc)
        rows = []
        for i in range(15):
            rows.append(make_ledger_row(
                (now - timedelta(minutes=i)).isoformat(),
                {"scope": 0, "depth": 1},
                [{"quote": "let's start with a basic version", "dim": "scope"}],
            ))
        for i in range(5):
            rows.append(make_ledger_row(
                (now - timedelta(minutes=20 + i)).isoformat(),
                {"scope": 1, "depth": 1},
            ))
        seed_ledger(ledger, rows)
        make_blocklist(tmp / "rules", [])
        ec, out, _ = run_hook({"session_id": "s1", "prompt": "x", "transcript_path": ""})
        check("exit 0", ec == 0)
        check("BEAST DRIFT heading present", "BEAST DRIFT (rolling 7d):" in out)
        check("scope listed", "- scope:" in out)
        check("rate shown", "75%" in out)
        check("trailer present", "Cite (a)-(d)" in out)
        check("no DO NOT block", "DO NOT use" not in out)


def test_phrase_block_rendered():
    print("\n[test] active phrases block rendered")
    with IsolatedEnv() as tmp:
        make_blocklist(tmp / "rules", [
            {"id": "ph_aaa", "phrase": "let me check", "status": "active",
             "hits": 10, "last_seen_ts": "2026-05-29T00:00:00+00:00",
             "dim_primary": "scope", "source": "haiku"},
            {"id": "ph_bbb", "phrase": "for now", "status": "active",
             "hits": 5, "last_seen_ts": "2026-05-28T00:00:00+00:00",
             "dim_primary": "deferrals", "source": "haiku"},
            {"id": "ph_ccc", "phrase": "should not appear", "status": "candidate",
             "hits": 100, "dim_primary": "scope", "source": "manual"},
        ])
        ec, out, _ = run_hook({"session_id": "s1", "prompt": "x", "transcript_path": ""})
        check("exit 0", ec == 0)
        check("DO NOT block present", "DO NOT use" in out)
        check("let me check injected", '"let me check"' in out)
        check("for now injected", '"for now"' in out)
        check("candidate NOT injected", "should not appear" not in out)


def test_phrase_cap_5():
    print("\n[test] phrase cap of 5")
    with IsolatedEnv() as tmp:
        phrases = [
            {"id": f"ph_{i:03d}", "phrase": f"phrase{i}", "status": "active",
             "hits": 10 - i, "last_seen_ts": "2026-05-29T00:00:00+00:00",
             "dim_primary": "scope", "source": "haiku"}
            for i in range(8)
        ]
        make_blocklist(tmp / "rules", phrases)
        ec, out, _ = run_hook({"session_id": "s1", "prompt": "x", "transcript_path": ""})
        count = out.count('"phrase')
        check("at most 5 phrases shown", count == 5, f"count={count}")
        check("highest-hits first (phrase0)", '"phrase0"' in out)
        check("lowest cut (phrase7 absent)", '"phrase7"' not in out)


def test_trigger_detect_hit():
    print("\n[test] trigger detect — phrase appears in last assistant turn")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        make_transcript(transcript, "okay I'll start by deciding to let me check the config.")
        make_blocklist(tmp / "rules", [
            {"id": "ph_aaa", "phrase": "let me check", "status": "active",
             "hits": 5, "last_seen_ts": "2026-05-29T00:00:00+00:00",
             "dim_primary": "scope", "source": "haiku"},
        ])
        ec, out, _ = run_hook({"session_id": "s1", "prompt": "x",
                                "transcript_path": str(transcript)})
        check("exit 0", ec == 0)
        log = (tmp / "rules" / "blocklist-log.jsonl").read_text()
        check("triggered event logged", '"event":"triggered"' in log,
              f"log={log[:200]}")
        check("id captured", '"id":"ph_aaa"' in log)
        # audit row has triggers_found populated
        last = json.loads((tmp / "rules" / "preturn-injections.jsonl")
                          .read_text().splitlines()[-1])
        check("audit row triggers_found has 1", len(last["triggers_found"]) == 1)


def test_trigger_detect_miss():
    print("\n[test] trigger detect — phrase absent")
    with IsolatedEnv() as tmp:
        transcript = tmp / "transcript.jsonl"
        make_transcript(transcript, "fully unrelated assistant text")
        make_blocklist(tmp / "rules", [
            {"id": "ph_aaa", "phrase": "let me check", "status": "active",
             "hits": 5, "last_seen_ts": "2026-05-29T00:00:00+00:00",
             "dim_primary": "scope", "source": "haiku"},
        ])
        ec, out, _ = run_hook({"session_id": "s1", "prompt": "x",
                                "transcript_path": str(transcript)})
        log_path = tmp / "rules" / "blocklist-log.jsonl"
        log = log_path.read_text() if log_path.exists() else ""
        check("no triggered event", '"event":"triggered"' not in log)
        last = json.loads((tmp / "rules" / "preturn-injections.jsonl")
                          .read_text().splitlines()[-1])
        check("audit row no triggers", last["triggers_found"] == [])


def test_recursion_guard():
    print("\n[test] recursion guard short-circuits")
    with IsolatedEnv() as tmp:
        os.environ["BEAST_MODE_AUDITOR_RUNNING"] = "1"
        try:
            ec, out, _ = run_hook({"session_id": "s1", "prompt": "x", "transcript_path": ""})
            check("exit 0", ec == 0)
            check("stdout empty", out == "")
            last = json.loads((tmp / "rules" / "preturn-injections.jsonl")
                              .read_text().splitlines()[-1])
            check("audit row skipped reason recursion",
                  last["skipped_reason"] == "recursion_guard_auditor")
        finally:
            del os.environ["BEAST_MODE_AUDITOR_RUNNING"]


def test_disable_env():
    print("\n[test] BEAST_PRETURN_DISABLED env")
    with IsolatedEnv() as tmp:
        os.environ["BEAST_PRETURN_DISABLED"] = "1"
        try:
            ec, out, _ = run_hook({"session_id": "s1", "prompt": "x", "transcript_path": ""})
            check("exit 0", ec == 0)
            check("stdout empty", out == "")
            last = json.loads((tmp / "rules" / "preturn-injections.jsonl")
                              .read_text().splitlines()[-1])
            check("dryrun_disabled in skipped_reason",
                  last["skipped_reason"] == "dryrun_disabled")
        finally:
            del os.environ["BEAST_PRETURN_DISABLED"]


def test_malformed_transcript():
    print("\n[test] malformed transcript JSONL")
    with IsolatedEnv() as tmp:
        transcript = tmp / "broken.jsonl"
        transcript.write_text("not json\n{also not\n")
        make_blocklist(tmp / "rules", [
            {"id": "ph_aaa", "phrase": "let me check", "status": "active",
             "hits": 5, "last_seen_ts": "2026-05-29T00:00:00+00:00",
             "dim_primary": "scope", "source": "haiku"},
        ])
        ec, out, _ = run_hook({"session_id": "s1", "prompt": "x",
                                "transcript_path": str(transcript)})
        check("no crash, exit 0", ec == 0)


def test_no_session_id():
    print("\n[test] missing session_id")
    with IsolatedEnv() as tmp:
        ec, out, _ = run_hook({"prompt": "x"})
        check("exit 0", ec == 0)
        last = json.loads((tmp / "rules" / "preturn-injections.jsonl")
                          .read_text().splitlines()[-1])
        check("skipped_reason no_session_id",
              last["skipped_reason"] == "no_session_id")


def test_block_chars_cap():
    print("\n[test] block char cap")
    with IsolatedEnv() as tmp:
        # Force long phrases that would exceed cap
        phrases = [
            {"id": f"ph_{i:03d}", "phrase": "x" * 200, "status": "active",
             "hits": 10, "last_seen_ts": "2026-05-29T00:00:00+00:00",
             "dim_primary": "scope", "source": "haiku"}
            for i in range(5)
        ]
        make_blocklist(tmp / "rules", phrases)
        ec, out, _ = run_hook({"session_id": "s1", "prompt": "x", "transcript_path": ""})
        check("output within hard cap", len(out) <= preturn.HARD_CAP_CHARS + 1,
              f"out_len={len(out)}")


def test_promoter_apply_triggers():
    print("\n[test] promoter apply_triggers consumes events + watermark")
    with IsolatedEnv() as tmp:
        # Seed an active phrase
        phrase = {"id": "ph_aaa", "phrase": "let me check", "status": "active",
                  "hits": 5, "trigger_count": 0, "last_triggered_ts": None,
                  "last_seen_ts": "2026-05-29T00:00:00+00:00",
                  "dim_primary": "scope", "source": "haiku"}
        make_blocklist(tmp / "rules", [phrase])
        # Write 3 triggered events
        log_path = tmp / "rules" / "blocklist-log.jsonl"
        now = datetime.now(timezone.utc)
        events_ts = [(now - timedelta(seconds=10 - i)).isoformat() for i in range(3)]
        with log_path.open("a") as f:
            for ts in events_ts:
                f.write(json.dumps({
                    "ts": ts, "event": "triggered", "id": "ph_aaa",
                    "actor": "pre-turn-hook",
                    "details": {"session": "s1"}
                }) + "\n")
        data = bm.load_blocklist()
        applied = bp.apply_triggers(data, dry_run=False)
        check("applied 3 events", applied == 3, f"applied={applied}")
        check("trigger_count = 3", data["phrases"][0]["trigger_count"] == 3)
        check("last_triggered_ts = latest event ts",
              data["phrases"][0]["last_triggered_ts"] == events_ts[-1])
        bm.save_blocklist(data)
        # Watermark file written
        wm = (tmp / "rules" / ".triggers-watermark").read_text().strip()
        check("watermark saved", wm == events_ts[-1])

        # Rerun → 0 applied (watermark prevents re-counting)
        data2 = bm.load_blocklist()
        applied2 = bp.apply_triggers(data2, dry_run=False)
        check("rerun applied 0", applied2 == 0, f"applied2={applied2}")
        check("trigger_count still 3", data2["phrases"][0]["trigger_count"] == 3)


def test_decay_activates_post_trigger():
    print("\n[test] decay sweep retires phrase with stale last_triggered_ts")
    with IsolatedEnv() as tmp:
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        phrase = {"id": "ph_aaa", "phrase": "x", "status": "active",
                  "hits": 5, "trigger_count": 2, "last_triggered_ts": old_ts,
                  "last_seen_ts": old_ts,
                  "dim_primary": "scope", "source": "haiku"}
        make_blocklist(tmp / "rules", [phrase])
        data = bm.load_blocklist()
        retired = bp.decay_sweep(data, decay_days=28, dry_run=False)
        check("decay retired 1", retired == 1)
        check("status retired", data["phrases"][0]["status"] == "retired")


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("WP-3 — pre-turn hook + promoter apply_triggers self-test")
    print("=" * 60)
    test_idle_skip()
    test_dim_block_rendered()
    test_phrase_block_rendered()
    test_phrase_cap_5()
    test_trigger_detect_hit()
    test_trigger_detect_miss()
    test_recursion_guard()
    test_disable_env()
    test_malformed_transcript()
    test_no_session_id()
    test_block_chars_cap()
    test_promoter_apply_triggers()
    test_decay_activates_post_trigger()
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
