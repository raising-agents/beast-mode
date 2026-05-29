#!/usr/bin/env python3
"""
Beast Mode Blocklist Manager — Adrian-facing CLI.

Owns lifecycle of ~/.claude/beast-mode/rules/blocklist.yaml and its structured
event log at rules/blocklist-log.jsonl. All state mutations go through this CLI
so the log stays machine-parseable.

Subcommands:
  seed                       one-shot init (creates yaml + log)
  list                       show phrases (filter by status/source)
  show <id>                  full record + last 10 log events
  promote <id>               candidate -> active
  retire <id> [--reason]     -> retired
  revive <id>                retired -> candidate
  add <phrase> --dim <dim>   manual add as candidate
  log tail [--n] [--event] [--id] [--since] [--format]
  log append --event --id [--actor] [--details]
  stats [--days N]           summary
  run-promoter [--dry-run]   convenience: invoke promoter

Event vocabulary (strict):
  seed_initialized, candidate_added, auto_promoted, manual_promoted,
  manual_retired, auto_retired, revived, triggered, hits_updated

Actor vocabulary (strict):
  promoter, adrian, pre-turn-hook, agent, manager
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required. Install via pip in /opt/miniconda3.", file=sys.stderr)
    sys.exit(2)

HOME = Path.home()
RULES_DIR = HOME / ".claude" / "beast-mode" / "rules"
BLOCKLIST_PATH = RULES_DIR / "blocklist.yaml"
LOG_PATH = RULES_DIR / "blocklist-log.jsonl"

SCHEMA_VERSION = 2

VALID_EVENTS = {
    "seed_initialized", "candidate_added", "auto_promoted", "manual_promoted",
    "manual_retired", "auto_retired", "revived", "triggered", "hits_updated",
}
VALID_ACTORS = {"promoter", "adrian", "pre-turn-hook", "agent", "manager"}
VALID_STATUSES = {"candidate", "active", "retired"}
VALID_SOURCES = {"haiku", "manual"}


# ─── time helpers ─────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_since(spec: str) -> datetime:
    """Parse '24h', '7d', '30m', or ISO datetime → UTC datetime."""
    s = spec.strip().lower()
    m = re.match(r"^(\d+)([smhd])$", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]
        return datetime.now(timezone.utc) - timedelta(**{delta: n})
    # try ISO
    try:
        dt = datetime.fromisoformat(spec.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise ValueError(f"unparseable --since: {spec!r}. Use '7d', '24h', '30m', or ISO.")


# ─── blocklist I/O ────────────────────────────────────────────────────────────

def load_blocklist() -> dict:
    if not BLOCKLIST_PATH.exists():
        return {"schema_version": SCHEMA_VERSION, "updated_ts": None, "phrases": []}
    with BLOCKLIST_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(f"schema_version mismatch: {data.get('schema_version')} != {SCHEMA_VERSION}")
    data.setdefault("phrases", [])
    return data


def save_blocklist(data: dict) -> None:
    """Atomic write via tmp file + rename."""
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_ts"] = now_iso()
    # ensure deterministic ordering: phrases sorted by id
    data["phrases"] = sorted(data.get("phrases", []), key=lambda p: p.get("id", ""))
    fd, tmp = tempfile.mkstemp(prefix=".blocklist-", suffix=".yaml", dir=str(RULES_DIR))
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
        os.replace(tmp, BLOCKLIST_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── log API ──────────────────────────────────────────────────────────────────

def log_append(event: str, entry_id: str | None, actor: str, details: dict | None = None,
               *, strict: bool = True) -> dict:
    if strict:
        if event not in VALID_EVENTS:
            raise ValueError(f"invalid event {event!r}. Valid: {sorted(VALID_EVENTS)}")
        if actor not in VALID_ACTORS:
            raise ValueError(f"invalid actor {actor!r}. Valid: {sorted(VALID_ACTORS)}")
    obj = {
        "ts": now_iso(),
        "event": event,
        "id": entry_id,
        "actor": actor,
        "details": details or {},
    }
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")
    return obj


def log_iter():
    if not LOG_PATH.exists():
        return
    with LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def log_filter(*, event: str | None = None, entry_id: str | None = None,
               since: datetime | None = None) -> list[dict]:
    out = []
    for ev in log_iter():
        if event and ev.get("event") != event:
            continue
        if entry_id and ev.get("id") != entry_id:
            continue
        if since:
            try:
                ts = datetime.fromisoformat(ev.get("ts", "").replace("Z", "+00:00"))
                if ts < since:
                    continue
            except ValueError:
                continue
        out.append(ev)
    return out


# ─── phrase helpers ───────────────────────────────────────────────────────────

def make_id(label_or_phrase: str) -> str:
    h = hashlib.sha1(label_or_phrase.encode("utf-8")).hexdigest()[:6]
    return f"ph_{h}"


def find_phrase(data: dict, entry_id: str) -> dict | None:
    for p in data.get("phrases", []):
        if p.get("id") == entry_id:
            return p
    return None


# ─── subcommands ──────────────────────────────────────────────────────────────

def cmd_seed(args) -> int:
    if BLOCKLIST_PATH.exists() or LOG_PATH.exists():
        print(f"refuse to seed: blocklist.yaml or blocklist-log.jsonl already exists in {RULES_DIR}",
              file=sys.stderr)
        return 2
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    save_blocklist({"schema_version": SCHEMA_VERSION, "updated_ts": now_iso(), "phrases": []})
    log_append("seed_initialized", None, "manager",
               {"schema_version": SCHEMA_VERSION, "path": str(BLOCKLIST_PATH)})
    print(f"seeded {BLOCKLIST_PATH} and {LOG_PATH}")
    return 0


def cmd_list(args) -> int:
    data = load_blocklist()
    phrases = data.get("phrases", [])
    if args.status != "all":
        wanted = set(args.status.split(","))
        phrases = [p for p in phrases if p.get("status") in wanted]
    if args.source != "all":
        wanted = set(args.source.split(","))
        phrases = [p for p in phrases if p.get("source") in wanted]

    if args.format == "json":
        print(json.dumps(phrases, indent=2))
        return 0

    if not phrases:
        print("(no matching phrases)")
        return 0

    rows = []
    for p in phrases:
        rows.append({
            "id": p.get("id", "")[:14],
            "status": (p.get("status") or "")[:9],
            "src": (p.get("source") or "")[:6],
            "dim": (p.get("dim_primary") or "")[:24],
            "hits": p.get("hits", 0),
            "trig": p.get("trigger_count", 0),
            "phrase": (p.get("phrase") or "")[:60],
        })
    # widths
    cols = ["id", "status", "src", "dim", "hits", "trig", "phrase"]
    widths = {c: max(len(c), max(len(str(r[c])) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    return 0


def cmd_show(args) -> int:
    data = load_blocklist()
    p = find_phrase(data, args.id)
    if not p:
        print(f"id not found: {args.id}", file=sys.stderr)
        return 1
    print(yaml.safe_dump(p, sort_keys=False, default_flow_style=False, allow_unicode=True))
    print("--- last 10 log events for this id ---")
    events = log_filter(entry_id=args.id)
    for ev in events[-10:]:
        print(json.dumps(ev, separators=(",", ":")))
    return 0


def _flip_status(args, target_status: str, event: str, allowed_from: set[str],
                 actor: str = "adrian") -> int:
    data = load_blocklist()
    p = find_phrase(data, args.id)
    if not p:
        print(f"id not found: {args.id}", file=sys.stderr)
        return 1
    if p["status"] not in allowed_from:
        print(f"cannot {event}: current status is {p['status']!r}, must be one of {sorted(allowed_from)}",
              file=sys.stderr)
        return 1
    p["status"] = target_status
    if target_status == "active":
        p["promoted_ts"] = now_iso()
    details: dict = {}
    reason = getattr(args, "reason", None)
    if reason:
        details["reason"] = reason
    save_blocklist(data)
    log_append(event, args.id, actor, details)
    print(f"{args.id}: status -> {target_status}")
    return 0


def cmd_promote(args) -> int:
    return _flip_status(args, "active", "manual_promoted", {"candidate"})


def cmd_retire(args) -> int:
    return _flip_status(args, "retired", "manual_retired", {"candidate", "active"})


def cmd_revive(args) -> int:
    return _flip_status(args, "candidate", "revived", {"retired"})


def cmd_add(args) -> int:
    data = load_blocklist()
    phrase = args.phrase.strip()
    if not phrase:
        print("phrase empty", file=sys.stderr)
        return 1
    eid = make_id(phrase)
    if find_phrase(data, eid):
        print(f"already exists: {eid}", file=sys.stderr)
        return 1
    entry = {
        "id": eid,
        "phrase": phrase,
        "examples": [phrase],
        "dim_primary": args.dim,
        "dim_counts": {args.dim: 0},
        "cluster_label": args.label,
        "cluster_rationale": args.rationale or "manual add",
        "status": "candidate",
        "hits": 0,
        "first_seen_ts": now_iso(),
        "last_seen_ts": now_iso(),
        "promoted_ts": None,
        "last_triggered_ts": None,
        "trigger_count": 0,
        "source": "manual",
        "notes": "",
    }
    data["phrases"].append(entry)
    save_blocklist(data)
    log_append("candidate_added", eid, "adrian",
               {"phrase": phrase, "dim_primary": args.dim, "via": "manual_add"})
    print(f"added {eid}: {phrase}")
    return 0


def cmd_log_tail(args) -> int:
    since = parse_since(args.since) if args.since else None
    events = log_filter(event=args.event, entry_id=args.id, since=since)
    events = events[-args.n:] if args.n > 0 else events
    if args.format == "jsonl":
        for ev in events:
            print(json.dumps(ev, separators=(",", ":")))
        return 0
    # table
    if not events:
        print("(no events)")
        return 0
    for ev in events:
        ts = ev.get("ts", "")[:19]
        e = (ev.get("event") or "")[:18]
        eid = (ev.get("id") or "-")[:14]
        actor = (ev.get("actor") or "")[:14]
        det = json.dumps(ev.get("details") or {}, separators=(",", ":"))
        print(f"{ts}  {e:<18}  {eid:<14}  {actor:<14}  {det[:80]}")
    return 0


def cmd_log_append(args) -> int:
    details = {}
    if args.details:
        try:
            details = json.loads(args.details)
        except json.JSONDecodeError as e:
            print(f"--details must be valid JSON: {e}", file=sys.stderr)
            return 1
    try:
        obj = log_append(args.event, args.id, args.actor, details)
    except ValueError as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 1
    print(json.dumps(obj, separators=(",", ":")))
    return 0


def cmd_stats(args) -> int:
    data = load_blocklist()
    phrases = data.get("phrases", [])
    by_status = {"candidate": 0, "active": 0, "retired": 0}
    by_source = {"haiku": 0, "manual": 0}
    total_hits = 0
    total_triggers = 0
    candidates_ready: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for p in phrases:
        by_status[p.get("status", "candidate")] = by_status.get(p.get("status", "candidate"), 0) + 1
        by_source[p.get("source", "haiku")] = by_source.get(p.get("source", "haiku"), 0) + 1
        total_hits += int(p.get("hits", 0) or 0)
        total_triggers += int(p.get("trigger_count", 0) or 0)
        if p.get("status") == "candidate":
            try:
                fs = datetime.fromisoformat((p.get("first_seen_ts") or "").replace("Z", "+00:00"))
                if fs <= cutoff:
                    candidates_ready.append({"id": p["id"], "phrase": p.get("phrase", ""), "first_seen_ts": p.get("first_seen_ts")})
            except (ValueError, AttributeError):
                pass

    # log event count window
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    log_events = log_filter(since=since)
    log_event_counts: dict = {}
    for ev in log_events:
        e = ev.get("event", "unknown")
        log_event_counts[e] = log_event_counts.get(e, 0) + 1

    out = {
        "phrases_total": len(phrases),
        "by_status": by_status,
        "by_source": by_source,
        "total_hits": total_hits,
        "total_triggers": total_triggers,
        "candidates_ready_for_auto_promote": candidates_ready,
        "log_events_window_days": args.days,
        "log_event_counts": log_event_counts,
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_run_promoter(args) -> int:
    promoter = Path(__file__).parent / "blocklist_promoter.py"
    if not promoter.exists():
        print(f"promoter not found: {promoter}", file=sys.stderr)
        return 1
    cmd = ["/opt/miniconda3/bin/python", str(promoter), "--once"]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.days:
        cmd.extend(["--days", str(args.days)])
    return subprocess.call(cmd)


# ─── argparse ─────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Beast Mode Blocklist Manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed", help="one-shot init")

    p_list = sub.add_parser("list", help="show phrases")
    p_list.add_argument("--status", default="candidate,active",
                        help="all | candidate | active | retired | comma-separated")
    p_list.add_argument("--source", default="all", help="all | haiku | manual")
    p_list.add_argument("--format", choices=("table", "json"), default="table")

    p_show = sub.add_parser("show", help="full record + recent log events")
    p_show.add_argument("id")

    p_promote = sub.add_parser("promote", help="candidate -> active")
    p_promote.add_argument("id")

    p_retire = sub.add_parser("retire", help="-> retired")
    p_retire.add_argument("id")
    p_retire.add_argument("--reason", default=None)

    p_revive = sub.add_parser("revive", help="retired -> candidate")
    p_revive.add_argument("id")

    p_add = sub.add_parser("add", help="manually add phrase as candidate")
    p_add.add_argument("phrase")
    p_add.add_argument("--dim", required=True)
    p_add.add_argument("--label", default=None)
    p_add.add_argument("--rationale", default=None)

    p_log = sub.add_parser("log", help="structured event log")
    log_sub = p_log.add_subparsers(dest="log_cmd", required=True)

    p_lt = log_sub.add_parser("tail", help="read events")
    p_lt.add_argument("--n", type=int, default=20)
    p_lt.add_argument("--event", default=None)
    p_lt.add_argument("--id", default=None)
    p_lt.add_argument("--since", default=None, help="e.g. 24h, 7d, ISO datetime")
    p_lt.add_argument("--format", choices=("table", "jsonl"), default="table")

    p_la = log_sub.add_parser("append", help="write event (validates vocab)")
    p_la.add_argument("--event", required=True)
    p_la.add_argument("--id", default=None)
    p_la.add_argument("--actor", required=True)
    p_la.add_argument("--details", default=None, help="JSON object")

    p_stats = sub.add_parser("stats", help="summary")
    p_stats.add_argument("--days", type=int, default=7)

    p_run = sub.add_parser("run-promoter", help="invoke promoter")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--days", type=int, default=None)

    return p


COMMAND_TABLE = {
    "seed": cmd_seed,
    "list": cmd_list,
    "show": cmd_show,
    "promote": cmd_promote,
    "retire": cmd_retire,
    "revive": cmd_revive,
    "add": cmd_add,
    "stats": cmd_stats,
    "run-promoter": cmd_run_promoter,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "log":
        return cmd_log_tail(args) if args.log_cmd == "tail" else cmd_log_append(args)
    return COMMAND_TABLE[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
