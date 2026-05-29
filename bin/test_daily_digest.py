#!/usr/bin/env python3
"""
WP-4 self-test — daily_digest.py.

No pytest. Isolated env per test via temp dirs + monkey-patched module paths.

Run: /opt/miniconda3/bin/python ~/.claude/beast-mode/bin/test_daily_digest.py
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

BIN_DIR = Path(__file__).parent
sys.path.insert(0, str(BIN_DIR))

import daily_digest as dd  # noqa: E402
import blocklist_manager as bm  # noqa: E402

PYTHON = "/opt/miniconda3/bin/python"
DIGEST_SCRIPT = str(BIN_DIR / "daily_digest.py")

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
    """Redirect all paths daily_digest + blocklist_manager touch to a temp dir."""
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-wp4-"))

    def __enter__(self):
        rules = self.tmp / "rules"
        cal = self.tmp / "calibration"
        ledger = self.tmp / "ledger"
        digests = self.tmp / "digests"
        for d in (rules, cal, ledger, digests):
            d.mkdir(parents=True)
        self._save = {
            "dd_LEDGER_PATH": dd.LEDGER_PATH,
            "dd_CORRECTIONS_PATH": dd.CORRECTIONS_PATH,
            "dd_INJECTIONS_PATH": dd.INJECTIONS_PATH,
            "dd_BLOCKLIST_LOG_PATH": dd.BLOCKLIST_LOG_PATH,
            "dd_DIGESTS_DIR": dd.DIGESTS_DIR,
            "dd_DIGEST_INDEX": dd.DIGEST_INDEX,
            "dd_LATEST_PATH": dd.LATEST_PATH,
            "dd_ERROR_LOG": dd.ERROR_LOG,
            "bm_RULES_DIR": bm.RULES_DIR,
            "bm_BLOCKLIST_PATH": bm.BLOCKLIST_PATH,
            "bm_LOG_PATH": bm.LOG_PATH,
        }
        dd.LEDGER_PATH = ledger / "drift.jsonl"
        dd.CORRECTIONS_PATH = cal / "corrections.jsonl"
        dd.INJECTIONS_PATH = rules / "preturn-injections.jsonl"
        dd.BLOCKLIST_LOG_PATH = rules / "blocklist-log.jsonl"
        dd.DIGESTS_DIR = digests
        dd.DIGEST_INDEX = digests / "daily-summary.jsonl"
        dd.LATEST_PATH = digests / "LATEST.md"
        dd.ERROR_LOG = digests / "daily-errors.log"
        bm.RULES_DIR = rules
        bm.BLOCKLIST_PATH = rules / "blocklist.yaml"
        bm.LOG_PATH = rules / "blocklist-log.jsonl"
        return self.tmp

    def __exit__(self, *exc):
        for k, v in self._save.items():
            mod, attr = k.split("_", 1)
            if mod == "dd":
                setattr(dd, attr, v)
            elif mod == "bm":
                setattr(bm, attr, v)
        shutil.rmtree(self.tmp, ignore_errors=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def make_ledger_row(ts: str, dims: dict, leaks: list[dict] | None = None) -> dict:
    return {"ts": ts, "session": "s1", "score": "x/x",
            "dims": dims, "leaks": leaks or []}


def seed_blocklist_yaml(rules: Path, phrases: list[dict]) -> None:
    bm.save_blocklist({"schema_version": 2, "updated_ts": None, "phrases": phrases})


# ─── tests ────────────────────────────────────────────────────────────────────

def test_aggregate_basic():
    print("\n[test] aggregate() computes basic stats")
    with IsolatedEnv() as tmp:
        now = datetime.now(timezone.utc)
        rows = []
        for i in range(8):
            rows.append(make_ledger_row(
                (now - timedelta(hours=i)).isoformat(),
                {"scope": 0, "depth": 1},
                [{"quote": "let's start with a basic", "dim": "scope"}],
            ))
        for i in range(2):
            rows.append(make_ledger_row(
                (now - timedelta(hours=10 + i)).isoformat(),
                {"scope": 1, "depth": 1},
            ))
        # Outside window
        rows.append(make_ledger_row((now - timedelta(hours=30)).isoformat(),
                                     {"scope": 0}))
        write_ledger(dd.LEDGER_PATH, rows)
        seed_blocklist_yaml(tmp / "rules", [])
        stats = dd.aggregate(window_hours=24)
        check("turns_audited == 10", stats["turns_audited"] == 10,
              f"got {stats['turns_audited']}")
        check("beasts == 12 (8 depth=1 + 2 scope=1 + 2 depth=1)",
              stats["beasts"] == 12, f"got {stats['beasts']}")
        check("applicable == 20", stats["applicable"] == 20)
        check("scope in top_leak_dims",
              any(d["dim"] == "scope" for d in stats["top_leak_dims"]))
        scope_d = next(d for d in stats["top_leak_dims"] if d["dim"] == "scope")
        check("scope leak rate 0.8 (8/10)", abs(scope_d["rate"] - 0.8) < 0.01,
              f"got {scope_d['rate']}")


def test_is_idle():
    print("\n[test] is_idle() classifies empty day")
    empty_stats = {
        "turns_audited": 0, "corrections_logged": 0,
        "injections_fired": 0, "blocklist_events_by_type": {},
    }
    check("empty is idle", dd.is_idle(empty_stats) is True)
    busy_stats = {
        "turns_audited": 10, "corrections_logged": 0,
        "injections_fired": 0, "blocklist_events_by_type": {},
    }
    check("10 turns not idle", dd.is_idle(busy_stats) is False)


def test_render_idle_digest():
    print("\n[test] render idle digest has frontmatter + body")
    with IsolatedEnv():
        stats = {"turns_audited": 2, "window_hours": 24}
        content = dd.render_digest("2026-05-29", stats,
                                    {"status": "skipped"}, None, "skipped", idle=True)
        check("frontmatter present", content.startswith("---\n"))
        check("idle marker in body", "Idle day" in content)
        check("heading present", "# Beast Daily — 2026-05-29" in content)


def test_render_full_digest_no_lm():
    print("\n[test] render full digest with lm_status=unavailable")
    with IsolatedEnv():
        stats = {
            "window_hours": 24, "turns_audited": 100,
            "beasts": 50, "applicable": 120,
            "beast_index": "50/120", "leak_rate": 0.42,
            "top_leak_dims": [
                {"dim": "scope", "rate": 0.5, "leaks": 30, "applicable": 60, "sample": "let me check"},
            ],
            "corrections_logged": 2, "confirms_true": 1, "confirms_false": 1,
            "injections_fired": 50, "injections_injected": 40, "injections_avg_chars": 312,
            "blocklist_events_by_type": {"triggered": 5},
            "blocklist_active": 3, "blocklist_candidate": 2, "blocklist_retired": 1,
        }
        content = dd.render_digest("2026-05-29", stats,
                                    {"status": "ok", "summary": {"added": 1, "promoted": 0, "retired": 0}},
                                    None, "unavailable", idle=False)
        check("headline fallback", "Sonnet unavailable" in content)
        check("scope dim rendered", "scope: 50% leak" in content)
        check("recommendations section absent", "## Recommendations" not in content)
        check("links section present", "## Links" in content)
        check("frontmatter lm_status", "lm_status: unavailable" in content)


def test_render_full_digest_with_lm():
    print("\n[test] render full digest with lm output")
    with IsolatedEnv():
        stats = {
            "window_hours": 24, "turns_audited": 50,
            "beasts": 30, "applicable": 60,
            "beast_index": "30/60", "leak_rate": 0.5,
            "top_leak_dims": [],
            "corrections_logged": 0, "confirms_true": 0, "confirms_false": 0,
            "injections_fired": 0, "injections_injected": 0, "injections_avg_chars": 0,
            "blocklist_events_by_type": {},
            "blocklist_active": 0, "blocklist_candidate": 0, "blocklist_retired": 0,
        }
        lm = {"headline": "Scope leaks down 10pp",
              "recommendations": ["Review WP-3 injection rate", "Promote ph_xyz"]}
        content = dd.render_digest("2026-05-29", stats,
                                    {"status": "ok"}, lm, "ok", idle=False)
        check("lm headline present", "Scope leaks down 10pp" in content)
        check("recommendation 1", "Review WP-3 injection rate" in content)
        check("recommendation 2", "Promote ph_xyz" in content)
        check("sonnet_used true in fm", "sonnet_used: true" in content)


def test_idle_full_cli_flow():
    print("\n[test] CLI flow: empty env → idle digest written")
    with IsolatedEnv() as tmp:
        seed_blocklist_yaml(tmp / "rules", [])
        # call dd.main() directly with isolated env
        ec = dd.main(["--once", "--no-promoter", "--no-lm"])
        check("exit 0", ec == 0)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out = (tmp / "digests" / f"{date_str}.md")
        check("digest file written", out.exists())
        check("LATEST.md written", (tmp / "digests" / "LATEST.md").exists())
        check("LATEST.md identical to dated",
              out.read_text() == (tmp / "digests" / "LATEST.md").read_text())
        idx = (tmp / "digests" / "daily-summary.jsonl")
        check("index row appended", idx.exists())
        line = idx.read_text().strip().splitlines()[-1]
        row = json.loads(line)
        check("index row idle=true", row["idle"] is True)


def test_full_flow_with_mock_sonnet():
    print("\n[test] CLI flow with --mock-sonnet writes lm sections")
    with IsolatedEnv() as tmp:
        # seed ledger with enough turns to NOT be idle
        now = datetime.now(timezone.utc)
        rows = [make_ledger_row((now - timedelta(hours=1)).isoformat(),
                                  {"scope": 0, "depth": 1},
                                  [{"quote": "sample", "dim": "scope"}])
                for _ in range(10)]
        write_ledger(dd.LEDGER_PATH, rows)
        seed_blocklist_yaml(tmp / "rules", [])
        # mock sonnet
        mock = tmp / "mock_sonnet.json"
        mock.write_text(json.dumps({
            "headline": "Scope leak rate elevated",
            "recommendations": ["Investigate scope leaks", "Run promoter"],
        }))
        ec = dd.main(["--once", "--no-promoter",
                       "--mock-sonnet", str(mock)])
        check("exit 0", ec == 0)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out = (tmp / "digests" / f"{date_str}.md").read_text()
        check("lm headline rendered", "Scope leak rate elevated" in out)
        check("rec 1 rendered", "Investigate scope leaks" in out)
        check("not idle", "Idle day" not in out)


def test_dry_run_no_write():
    print("\n[test] --dry-run writes nothing")
    with IsolatedEnv() as tmp:
        seed_blocklist_yaml(tmp / "rules", [])
        ec = dd.main(["--once", "--no-promoter", "--no-lm", "--dry-run"])
        check("exit 0", ec == 0)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = tmp / "digests" / f"{date_str}.md"
        check("no digest file written", not out_path.exists())


def test_force_overrides_idle():
    print("\n[test] --force writes digest on empty env")
    with IsolatedEnv() as tmp:
        seed_blocklist_yaml(tmp / "rules", [])
        ec = dd.main(["--once", "--no-promoter", "--no-lm", "--force"])
        check("exit 0", ec == 0)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out = (tmp / "digests" / f"{date_str}.md").read_text()
        check("not idle digest", "Idle day" not in out)
        check("Drift section present", "## Drift" in out)


def test_idempotency_index_grows():
    print("\n[test] idempotency: 2 runs same day → 2 index rows, 1 digest file")
    with IsolatedEnv() as tmp:
        seed_blocklist_yaml(tmp / "rules", [])
        dd.main(["--once", "--no-promoter", "--no-lm", "--force"])
        dd.main(["--once", "--no-promoter", "--no-lm", "--force"])
        idx = (tmp / "digests" / "daily-summary.jsonl")
        lines = idx.read_text().strip().splitlines()
        check("2 index rows", len(lines) == 2, f"got {len(lines)}")
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        files = list((tmp / "digests").glob(f"{date_str}*.md"))
        check("1 dated digest file", len(files) == 1, f"got {len(files)}")


def test_yaml_frontmatter_parseable():
    print("\n[test] YAML frontmatter parses with safe_load")
    import yaml
    with IsolatedEnv() as tmp:
        seed_blocklist_yaml(tmp / "rules", [])
        dd.main(["--once", "--no-promoter", "--no-lm", "--force"])
        content = (tmp / "digests" / "LATEST.md").read_text()
        parts = content.split("---")
        check("at least 3 sections via ---", len(parts) >= 3)
        front = yaml.safe_load(parts[1])
        check("date key present", "date" in front)
        check("schema_version key", front.get("schema_version") == 1)
        check("idle flag present", "idle" in front)


def test_promoter_failure_captured():
    print("\n[test] promoter rc=1 captured as status=error")
    with IsolatedEnv() as tmp:
        # Replace promoter path with a failing script
        fake_promoter = tmp / "fake_promoter.py"
        fake_promoter.write_text("import sys; print('boom'); sys.exit(1)")
        saved = dd.PROMOTER
        dd.PROMOTER = fake_promoter
        try:
            result = dd.run_promoter(dry_run=False)
            check("status error", result.get("status") == "error",
                  f"got {result}")
        finally:
            dd.PROMOTER = saved


def test_promoter_missing_returns_not_found():
    print("\n[test] promoter missing → status=not_found")
    with IsolatedEnv() as tmp:
        saved = dd.PROMOTER
        dd.PROMOTER = tmp / "nonexistent.py"
        try:
            result = dd.run_promoter(dry_run=False)
            check("status not_found", result.get("status") == "not_found")
        finally:
            dd.PROMOTER = saved


def test_promoter_summary_parsed():
    print("\n[test] promoter JSON summary parsed from stdout")
    with IsolatedEnv() as tmp:
        # Mock promoter that prints a JSON summary
        fake = tmp / "mock_promoter.py"
        fake.write_text(
            "import sys\n"
            "print('[promoter] some log')\n"
            "import json\n"
            "print(json.dumps({'added': 2, 'promoted': 0, 'retired': 1, "
            "'applied_triggers': 5, 'by_status': {}, 'dry_run': False}))\n"
        )
        saved = dd.PROMOTER
        dd.PROMOTER = fake
        try:
            result = dd.run_promoter(dry_run=False)
            check("status ok", result.get("status") == "ok")
            check("summary parsed", result.get("summary") is not None)
            check("added=2", result["summary"]["added"] == 2)
            check("applied_triggers=5", result["summary"]["applied_triggers"] == 5)
        finally:
            dd.PROMOTER = saved


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("WP-4 — daily_digest self-test")
    print("=" * 60)
    test_aggregate_basic()
    test_is_idle()
    test_render_idle_digest()
    test_render_full_digest_no_lm()
    test_render_full_digest_with_lm()
    test_idle_full_cli_flow()
    test_full_flow_with_mock_sonnet()
    test_dry_run_no_write()
    test_force_overrides_idle()
    test_idempotency_index_grows()
    test_yaml_frontmatter_parseable()
    test_promoter_failure_captured()
    test_promoter_missing_returns_not_found()
    test_promoter_summary_parsed()
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
