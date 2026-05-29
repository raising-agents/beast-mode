#!/usr/bin/env python3
"""
Beast Mode Blocklist Promoter — Haiku-driven cluster discovery.

Reads recent leak quotes from ~/.claude/beast-mode/ledger/drift.jsonl, calls
Haiku once to cluster paraphrastic variants into named patterns, merges new
candidates into ~/.claude/beast-mode/rules/blocklist.yaml, and runs the
auto-promote / decay sweeps.

All state mutations go through blocklist_manager.log_append() so the
event log stays machine-parseable.

Run modes:
  --once              process now and exit
  --dry-run           call Haiku but write nothing
  --days N            lookback window (default 14)
  --max-quotes N      cap quotes sent to Haiku (default 200)
  --auto-promote-days N   age at which candidates auto-promote (default 7)
  --decay-days N      silence period before active → retired (default 28)

Idempotent. Two back-to-back runs produce 0 new candidates the second time.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path.home()
BIN_DIR = HOME / ".claude" / "beast-mode" / "bin"
LEDGER_PATH = HOME / ".claude" / "beast-mode" / "ledger" / "drift.jsonl"
ERROR_LOG = HOME / ".claude" / "beast-mode" / "rules" / "promoter-errors.log"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local" / "bin" / "claude"))

sys.path.insert(0, str(BIN_DIR))
try:
    import blocklist_manager as bm  # type: ignore
except ImportError as e:
    print(f"ERROR: cannot import blocklist_manager: {e}", file=sys.stderr)
    sys.exit(2)


HAIKU_SYSTEM = """\
You cluster verbatim leak quotes from a coding agent into named antipattern groups.

Two quotes belong in the same cluster if they express the same drift behavior, regardless of surface wording.

Constraints:
- Each cluster must contain >=3 distinct quotes (or it isn't a real pattern).
- Skip quotes that semantically match any of the "existing_phrases" below — those are already covered.
- The representative_phrase MUST be a short literal phrase (5-8 words) that downstream code will substring-match against agent text. It must NOT be a regex. It must NOT contain placeholders like "X" or "<verb>". Use a concrete prototypical example.
- label is snake_case, 2-4 words.
- dim_primary is the most common dim across the cluster's examples.
- Return JSON only. No prose, no markdown fences.

Output schema (exact):
{
  "clusters": [
    {
      "label": "snake_case_name",
      "representative_phrase": "short literal phrase",
      "rationale": "one short sentence",
      "dim_primary": "scope|depth|deferrals|boldness|sequencing|parallelism|action_over_announcement|verification_by_evidence|block_breaking|self_direction_over_ask",
      "examples": ["verbatim quote 1", "verbatim quote 2", "verbatim quote 3"]
    }
  ]
}
"""


# ─── normalize ────────────────────────────────────────────────────────────────

def normalize(quote: str) -> str:
    s = (quote or "").strip().lower()
    # collapse internal whitespace
    s = " ".join(s.split())
    # loop strip until stable: surrounding quote-likes AND trailing terminal punctuation
    chars_to_peel = set("`\"'“”‘’.!?")
    prev = None
    while prev != s and s:
        prev = s
        if s[0] in chars_to_peel:
            s = s[1:]
        if s and s[-1] in chars_to_peel:
            s = s[:-1]
    return s.strip()


# ─── ledger window read ───────────────────────────────────────────────────────

def read_ledger_window(days: int) -> list[dict]:
    """Return all rows from drift.jsonl within last `days` days."""
    if not LEDGER_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # We don't know how far back to seek. Use byte-seek heuristic + walk: read
    # last 4 MB, parse, filter by ts. 4 MB ~= 30 days of dense audit at current rate.
    file_size = LEDGER_PATH.stat().st_size
    read_bytes = min(file_size, 4 * 1024 * 1024)
    with LEDGER_PATH.open("rb") as f:
        f.seek(file_size - read_bytes)
        chunk = f.read().decode("utf-8", errors="ignore")
    lines = chunk.splitlines()
    # trim partial first line if mid-file
    if file_size > read_bytes and lines:
        lines = lines[1:]
    rows = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            r = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts_str = r.get("ts") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= cutoff:
            rows.append(r)
    return rows


def extract_quotes(rows: list[dict]) -> list[dict]:
    """Return list of {quote_raw, quote_norm, dim, ts}."""
    out = []
    for r in rows:
        ts = r.get("ts")
        for leak in (r.get("leaks") or []):
            q = leak.get("quote") or ""
            dim = leak.get("dim")
            n = normalize(q)
            if 4 <= len(n) <= 120 and dim:
                out.append({"quote_raw": q, "quote_norm": n, "dim": dim, "ts": ts})
    return out


def dedupe_quotes(quotes: list[dict], max_quotes: int) -> list[dict]:
    """Dedupe by normalized form. Keep representative + count + dim distribution."""
    bucket: dict[str, dict] = {}
    for q in quotes:
        n = q["quote_norm"]
        b = bucket.setdefault(n, {
            "quote_norm": n,
            "representative_raw": q["quote_raw"],
            "hits": 0,
            "dims": {},
            "first_ts": q["ts"],
            "last_ts": q["ts"],
        })
        b["hits"] += 1
        b["dims"][q["dim"]] = b["dims"].get(q["dim"], 0) + 1
        if q["ts"] and (not b["last_ts"] or q["ts"] > b["last_ts"]):
            b["last_ts"] = q["ts"]
        if q["ts"] and (not b["first_ts"] or q["ts"] < b["first_ts"]):
            b["first_ts"] = q["ts"]
    # sort by hits desc, then by quote norm for stability
    sorted_b = sorted(bucket.values(), key=lambda x: (-x["hits"], x["quote_norm"]))
    return sorted_b[:max_quotes]


# ─── Haiku call ───────────────────────────────────────────────────────────────

def call_haiku(quotes: list[dict], existing_phrases: list[str], timeout: int = 120) -> dict | None:
    if not quotes:
        return {"clusters": []}
    if os.environ.get("BEAST_MODE_INTERPRETER_RUNNING") == "1":
        log_error("recursion guard tripped (BEAST_MODE_INTERPRETER_RUNNING=1) — skipping")
        return None
    env = os.environ.copy()
    env["BEAST_MODE_INTERPRETER_RUNNING"] = "1"

    payload_obj = {
        "existing_phrases": existing_phrases,
        "leak_quotes_with_dims": [
            {"quote": q["representative_raw"], "dim": _primary_dim(q["dims"]), "hits": q["hits"]}
            for q in quotes
        ],
    }
    payload = (
        "INPUT:\n"
        + json.dumps(payload_obj, indent=2)
        + "\n\nReturn the JSON object now."
    )

    cmd = [CLAUDE_BIN, "-p", "--model", "haiku",
           "--append-system-prompt", HAIKU_SYSTEM, payload]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        log_error("haiku timeout")
        return None
    except Exception as e:
        log_error(f"haiku subprocess error: {e}")
        return None

    if result.returncode != 0:
        log_error(f"haiku rc={result.returncode} stderr={result.stderr[:300]}")
        return None

    raw = result.stdout.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        nl = raw.find("\n")
        if nl >= 0:
            raw = raw[nl + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        log_error(f"no JSON in haiku output: {raw[:300]}")
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        log_error(f"haiku JSON parse failed: {e} :: {raw[start:end+1][:300]}")
        return None


def _primary_dim(dim_counts: dict[str, int]) -> str:
    if not dim_counts:
        return "scope"
    return max(dim_counts.items(), key=lambda kv: kv[1])[0]


# ─── log errors ───────────────────────────────────────────────────────────────

def log_error(msg: str) -> None:
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except Exception:
        pass


# ─── merge clusters into blocklist ────────────────────────────────────────────

def merge_clusters(data: dict, clusters: list[dict], min_cluster_size: int,
                   dry_run: bool) -> tuple[int, int]:
    """Return (added, updated)."""
    added = 0
    updated = 0
    by_id = {p["id"]: p for p in data.get("phrases", [])}

    for c in clusters:
        examples = c.get("examples") or []
        if len(examples) < min_cluster_size:
            continue
        label = (c.get("label") or "").strip()
        rep = (c.get("representative_phrase") or "").strip()
        rationale = (c.get("rationale") or "").strip()
        dim_primary = c.get("dim_primary") or "scope"
        if not label or not rep:
            continue
        eid = bm.make_id(label)

        if eid in by_id:
            p = by_id[eid]
            # refresh examples (union, capped 5)
            seen = set(p.get("examples", []))
            for ex in examples:
                if ex not in seen and len(p.get("examples", [])) < 5:
                    p.setdefault("examples", []).append(ex)
                    seen.add(ex)
            p["hits"] = max(p.get("hits", 0), len(examples))
            p["last_seen_ts"] = bm.now_iso()
            # update dim_counts: add new dim hits
            dc = p.setdefault("dim_counts", {})
            dc[dim_primary] = dc.get(dim_primary, 0) + len(examples)
            p["dim_primary"] = max(dc.items(), key=lambda kv: kv[1])[0]
            if not dry_run:
                bm.log_append("hits_updated", eid, "promoter",
                              {"new_examples_seen": len(examples), "dim_primary": p["dim_primary"]})
            updated += 1
        else:
            entry = {
                "id": eid,
                "phrase": rep,
                "examples": examples[:5],
                "dim_primary": dim_primary,
                "dim_counts": {dim_primary: len(examples)},
                "cluster_label": label,
                "cluster_rationale": rationale,
                "status": "candidate",
                "hits": len(examples),
                "first_seen_ts": bm.now_iso(),
                "last_seen_ts": bm.now_iso(),
                "promoted_ts": None,
                "last_triggered_ts": None,
                "trigger_count": 0,
                "source": "haiku",
                "notes": "",
            }
            data["phrases"].append(entry)
            by_id[eid] = entry
            if not dry_run:
                bm.log_append("candidate_added", eid, "promoter",
                              {"hits": len(examples), "cluster_label": label,
                               "dim_primary": dim_primary, "phrase": rep})
            added += 1
    return added, updated


# ─── auto-promote sweep ───────────────────────────────────────────────────────

def auto_promote(data: dict, age_days: int, dry_run: bool) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)
    promoted = 0
    # build set of ids that were manually retired then revived — current status candidate covers them
    # check log for any manual_retired event we should respect
    retired_ids = {
        ev["id"] for ev in bm.log_filter(event="manual_retired") if ev.get("id")
    }
    for p in data.get("phrases", []):
        if p.get("status") != "candidate":
            continue
        if p["id"] in retired_ids:
            continue
        try:
            fs = datetime.fromisoformat((p.get("first_seen_ts") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if fs > cutoff:
            continue
        p["status"] = "active"
        p["promoted_ts"] = bm.now_iso()
        if not dry_run:
            bm.log_append("auto_promoted", p["id"], "promoter",
                          {"reason": f"candidate_age_{age_days}d_no_rejection"})
        promoted += 1
    return promoted


# ─── apply triggers (consume blocklist-log.jsonl) ─────────────────────────────

WATERMARK_PATH = HOME / ".claude" / "beast-mode" / "rules" / ".triggers-watermark"


def _load_watermark() -> str | None:
    if not WATERMARK_PATH.exists():
        return None
    try:
        return WATERMARK_PATH.read_text().strip() or None
    except Exception:
        return None


def _save_watermark(ts: str) -> None:
    try:
        WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        WATERMARK_PATH.write_text(ts)
    except Exception as e:
        log_error(f"watermark save failed: {e}")


def apply_triggers(data: dict, dry_run: bool) -> int:
    """
    Read triggered events from blocklist-log.jsonl since last watermark.
    Bump trigger_count + last_triggered_ts on matching phrases.
    Returns count of trigger events applied.
    """
    last_ts = _load_watermark()
    by_id = {p["id"]: p for p in data.get("phrases", [])}
    applied = 0
    max_seen_ts = last_ts or ""
    for ev in bm.log_iter():
        if ev.get("event") != "triggered":
            continue
        ev_ts = ev.get("ts") or ""
        if last_ts and ev_ts <= last_ts:
            continue
        eid = ev.get("id")
        if not eid or eid not in by_id:
            continue
        p = by_id[eid]
        p["trigger_count"] = int(p.get("trigger_count") or 0) + 1
        # only advance last_triggered_ts forward
        cur = p.get("last_triggered_ts") or ""
        if ev_ts > cur:
            p["last_triggered_ts"] = ev_ts
        applied += 1
        if ev_ts > max_seen_ts:
            max_seen_ts = ev_ts
    if applied and not dry_run and max_seen_ts:
        _save_watermark(max_seen_ts)
    return applied


# ─── decay sweep ──────────────────────────────────────────────────────────────

def decay_sweep(data: dict, decay_days: int, dry_run: bool) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=decay_days)
    retired = 0
    for p in data.get("phrases", []):
        if p.get("status") != "active":
            continue
        # silence reference: prefer last_triggered_ts; fall back to last_seen_ts
        silence_ref = p.get("last_triggered_ts") or p.get("last_seen_ts")
        if not silence_ref:
            continue
        try:
            sr = datetime.fromisoformat(silence_ref.replace("Z", "+00:00"))
        except ValueError:
            continue
        if sr > cutoff:
            continue
        p["status"] = "retired"
        if not dry_run:
            bm.log_append("auto_retired", p["id"], "promoter",
                          {"reason": f"no_triggers_{decay_days}d",
                           "silence_ref": silence_ref})
        retired += 1
    return retired


# ─── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Beast Mode Blocklist Promoter")
    p.add_argument("--once", action="store_true", help="process now and exit (required)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--days", type=int, default=14, help="ledger lookback window")
    p.add_argument("--max-quotes", type=int, default=200)
    p.add_argument("--min-cluster-size", type=int, default=5)
    p.add_argument("--auto-promote-days", type=int, default=7)
    p.add_argument("--decay-days", type=int, default=28)
    p.add_argument("--mock-haiku-output", default=None,
                   help="path to JSON file with canned Haiku response (test mode)")
    args = p.parse_args(argv)

    if not args.once:
        print("must pass --once (single-shot only in v1)", file=sys.stderr)
        return 2

    if not bm.BLOCKLIST_PATH.exists():
        print(f"blocklist not seeded. Run: {bm.__file__.replace('blocklist_manager.py', '')}blocklist_manager.py seed",
              file=sys.stderr)
        return 1

    data = bm.load_blocklist()

    rows = read_ledger_window(args.days)
    quotes_raw = extract_quotes(rows)
    quotes = dedupe_quotes(quotes_raw, args.max_quotes)

    print(f"[promoter] ledger rows in {args.days}d: {len(rows)}")
    print(f"[promoter] leak quotes extracted: {len(quotes_raw)}")
    print(f"[promoter] deduped (top {args.max_quotes}): {len(quotes)}")

    existing_phrases = [p["phrase"] for p in data.get("phrases", []) if p.get("status") in ("candidate", "active")]

    if args.mock_haiku_output:
        with open(args.mock_haiku_output) as f:
            haiku_resp = json.load(f)
    else:
        haiku_resp = call_haiku(quotes, existing_phrases)

    if haiku_resp is None:
        print("[promoter] Haiku call failed — see promoter-errors.log", file=sys.stderr)
        # still run apply_triggers + auto-promote + decay even if Haiku fails
        applied = apply_triggers(data, args.dry_run)
        promoted = auto_promote(data, args.auto_promote_days, args.dry_run)
        retired = decay_sweep(data, args.decay_days, args.dry_run)
        if not args.dry_run and (applied or promoted or retired):
            bm.save_blocklist(data)
        summary = {"added": 0, "updated": 0, "applied_triggers": applied,
                   "promoted": promoted, "retired": retired, "haiku_failed": True}
        print(json.dumps(summary))
        return 1

    clusters = haiku_resp.get("clusters") or []
    print(f"[promoter] Haiku returned {len(clusters)} clusters")

    added, updated = merge_clusters(data, clusters, args.min_cluster_size, args.dry_run)
    applied = apply_triggers(data, args.dry_run)
    promoted = auto_promote(data, args.auto_promote_days, args.dry_run)
    retired = decay_sweep(data, args.decay_days, args.dry_run)

    if not args.dry_run:
        bm.save_blocklist(data)

    by_status: dict[str, int] = {"candidate": 0, "active": 0, "retired": 0}
    for ph in data.get("phrases", []):
        by_status[ph.get("status", "candidate")] = by_status.get(ph.get("status", "candidate"), 0) + 1

    summary = {
        "added": added,
        "updated": updated,
        "applied_triggers": applied,
        "promoted": promoted,
        "retired": retired,
        "total_phrases": len(data.get("phrases", [])),
        "by_status": by_status,
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
