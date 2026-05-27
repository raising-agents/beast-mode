#!/usr/bin/env python3
"""
Beast Mode Receipt Store — shared library.

Unified typed receipt store. All producers write here; all consumers read here.
drift.jsonl is preserved separately for backward compat.

Receipt types: structural | behavioral | meta | evolution
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator

RECEIPT_DIR = Path.home() / ".claude" / "beast-mode" / "receipts"
_write_lock = threading.Lock()

DIMS_STRUCTURAL = {"parallelism", "action_gap", "coverage", "self_direction"}
DIMS_BEHAVIORAL = {
    "scope", "depth", "deferrals", "boldness",
    "verification_by_evidence", "action_over_announcement",
    "block_breaking", "self_direction_over_ask",
}
DIMS_META = {"coaching_dependency", "self_audit_rate", "in_session_compounding", "recovery_posture"}

ALL_DIMS = DIMS_STRUCTURAL | DIMS_BEHAVIORAL | DIMS_META


def _turn_id(session: str, ts: str) -> str:
    raw = f"{session}:{ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def write_receipt(receipt: dict) -> None:
    """Append one receipt to today's JSONL file."""
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    receipt.setdefault("ts", datetime.now(timezone.utc).isoformat())
    session = receipt.get("session", "unknown")
    ts = receipt.get("ts", "")
    receipt.setdefault("turn_id", _turn_id(session, ts))
    today = datetime.now(timezone.utc).date().isoformat()
    path = RECEIPT_DIR / f"{today}.jsonl"
    with _write_lock:
        with path.open("a") as f:
            f.write(json.dumps(receipt, separators=(",", ":")) + "\n")


def _iter_receipts(days: int = 30) -> Iterator[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for path in sorted(RECEIPT_DIR.glob("*.jsonl")):
        try:
            file_date = datetime.fromisoformat(path.stem).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if file_date < cutoff - timedelta(days=1):
            continue
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    ts = datetime.fromisoformat(r.get("ts", "").replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
                yield r


def read_receipts(
    days: int = 7,
    receipt_type: str | None = None,
    session: str | None = None,
) -> list[dict]:
    results = []
    for r in _iter_receipts(days=days):
        if receipt_type and r.get("receipt_type") != receipt_type:
            continue
        if session and r.get("session") != session:
            continue
        results.append(r)
    return results


def session_receipts(session_id: str, days: int = 90) -> dict[str, list[dict]]:
    """All receipts for one session, grouped by receipt_type."""
    by_type: dict[str, list[dict]] = {}
    for r in _iter_receipts(days=days):
        if r.get("session") != session_id:
            continue
        t = r.get("receipt_type", "unknown")
        by_type.setdefault(t, []).append(r)
    return by_type


def dim_history(dim: str, days: int = 30) -> list[dict]:
    """Time-series of {ts, session, score, method} for one dimension."""
    results = []
    for r in _iter_receipts(days=days):
        dims = r.get("dims", {})
        if dim not in dims:
            continue
        d = dims[dim]
        results.append({
            "ts": r.get("ts"),
            "session": r.get("session"),
            "score": d.get("score"),
            "method": d.get("method"),
            "receipt_type": r.get("receipt_type"),
        })
    return results


def receipt_stats(days: int = 7) -> dict:
    """Aggregate stats for dashboard display."""
    by_type: dict[str, int] = {}
    dim_totals: dict[str, dict] = {}  # dim → {beast, total, method}

    for r in _iter_receipts(days=days):
        t = r.get("receipt_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        for dim, d in (r.get("dims") or {}).items():
            if not isinstance(d, dict):
                continue
            score = d.get("score")
            if score not in (0, 1):
                continue
            if dim not in dim_totals:
                dim_totals[dim] = {"beast": 0, "total": 0, "method": d.get("method", "unknown")}
            dim_totals[dim]["total"] += 1
            if score == 1:
                dim_totals[dim]["beast"] += 1

    return {"by_type": by_type, "dims": dim_totals}


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Beast Mode Receipt Store CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="List recent receipts")
    p_list.add_argument("--days", type=int, default=7)
    p_list.add_argument("--type", dest="receipt_type")
    p_list.add_argument("--session")
    p_list.add_argument("--limit", type=int, default=20)

    p_stats = sub.add_parser("stats", help="Aggregate stats")
    p_stats.add_argument("--days", type=int, default=7)

    p_dim = sub.add_parser("dim", help="Time-series for one dim")
    p_dim.add_argument("dim_name")
    p_dim.add_argument("--days", type=int, default=30)

    args = parser.parse_args()

    if args.cmd == "list":
        rs = read_receipts(days=args.days, receipt_type=args.receipt_type, session=args.session)
        for r in rs[-args.limit:]:
            print(json.dumps(r, indent=2))
    elif args.cmd == "stats":
        print(json.dumps(receipt_stats(days=args.days), indent=2))
    elif args.cmd == "dim":
        history = dim_history(args.dim_name, days=args.days)
        for h in history:
            print(json.dumps(h))
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
