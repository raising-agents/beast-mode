#!/usr/bin/env python3
"""
Beast Mode Health — shared library + tiny CLI.

Owns ~/.claude/beast-mode/health.json. Two sections: `auditor`, `evolution`.
Each tracks last_run_ts, last_status, consecutive_failures, total_runs, last_error.

All writers (auditor-worker.py, evolution.py) call record() to update their
section atomically without losing the other section.

CLI:
  health.py show               print current health.json
  health.py status             print one-line summary (used by statusline)
  health.py reset --section X  zero counters for a section (debug)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

HEALTH_PATH = Path.home() / ".claude" / "beast-mode" / "health.json"
SCHEMA_VERSION = 1
VALID_SECTIONS = {"auditor", "evolution"}
VALID_STATUSES = {"ok", "error", "timeout", "skipped"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_section() -> dict:
    return {
        "last_run_ts": None,
        "last_status": None,
        "last_error": None,
        "consecutive_failures": 0,
        "total_runs": 0,
        "total_failures": 0,
    }


def load() -> dict:
    if not HEALTH_PATH.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_ts": None,
            "auditor": _empty_section(),
            "evolution": _empty_section(),
        }
    try:
        data = json.loads(HEALTH_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_ts": None,
            "auditor": _empty_section(),
            "evolution": _empty_section(),
        }
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("auditor", _empty_section())
    data.setdefault("evolution", _empty_section())
    return data


def _atomic_write(data: dict) -> None:
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_ts"] = now_iso()
    fd, tmp = tempfile.mkstemp(prefix=".health-", suffix=".json", dir=str(HEALTH_PATH.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, HEALTH_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(section: str, status: str, error: str | None = None,
           extra: dict | None = None) -> None:
    """
    Update one section atomically.

    status='ok' resets consecutive_failures.
    Other statuses increment consecutive_failures + total_failures.
    """
    if section not in VALID_SECTIONS:
        raise ValueError(f"invalid section {section!r}")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}")

    data = load()
    sect = data.get(section) or _empty_section()
    sect["last_run_ts"] = now_iso()
    sect["last_status"] = status
    sect["total_runs"] = int(sect.get("total_runs") or 0) + 1
    if status == "ok":
        sect["consecutive_failures"] = 0
        sect["last_error"] = None
    else:
        sect["consecutive_failures"] = int(sect.get("consecutive_failures") or 0) + 1
        sect["total_failures"] = int(sect.get("total_failures") or 0) + 1
        if error:
            sect["last_error"] = error[:300]
    if extra:
        sect.update({k: v for k, v in extra.items()
                     if k not in ("last_run_ts", "last_status", "consecutive_failures",
                                  "total_runs", "total_failures", "last_error")})
    data[section] = sect
    _atomic_write(data)


def status_line() -> str:
    """One-line statusline summary. Empty string if healthy."""
    data = load()
    parts: list[str] = []

    evo = data.get("evolution") or {}
    evo_fails = int(evo.get("consecutive_failures") or 0)
    evo_ts = evo.get("last_run_ts")
    if evo_fails >= 1:
        parts.append(f"EVOL ERR x{evo_fails}")
    elif evo_ts:
        try:
            dt = datetime.fromisoformat(evo_ts.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days >= 10:
                parts.append(f"EVOL STALE {age_days}d")
        except ValueError:
            pass

    aud = data.get("auditor") or {}
    aud_fails = int(aud.get("consecutive_failures") or 0)
    if aud_fails >= 3:
        parts.append(f"AUDIT ERR x{aud_fails}")

    return " ".join(parts)


def _cli() -> int:
    p = argparse.ArgumentParser(description="Beast Mode Health CLI")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("show", help="print health.json")
    sub.add_parser("status", help="one-line statusline summary")
    p_reset = sub.add_parser("reset", help="zero counters for a section")
    p_reset.add_argument("--section", required=True, choices=sorted(VALID_SECTIONS))
    args = p.parse_args()

    if args.cmd == "show" or args.cmd is None:
        print(json.dumps(load(), indent=2))
        return 0
    if args.cmd == "status":
        print(status_line())
        return 0
    if args.cmd == "reset":
        data = load()
        data[args.section] = _empty_section()
        _atomic_write(data)
        print(f"reset {args.section}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
