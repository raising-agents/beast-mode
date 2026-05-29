#!/usr/bin/env python3
"""
Beast Mode Auditor Worker — v2.

Two-phase execution:
  Phase 1 (structural, no LM): reads tool-call state file, computes
    parallelism/action_gap/coverage structural dims, writes structural receipt.
  Phase 2 (behavioral, Haiku): scores 10 dims including 4 new ones, passes
    structural context as anchors, writes behavioral receipt + drift.jsonl entry.

Spawned in background by the Stop hook. Never blocks the user.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))
LEDGER_DIR = Path.home() / ".claude" / "beast-mode" / "ledger"
LEDGER_PATH = LEDGER_DIR / "drift.jsonl"
ERROR_LOG = LEDGER_DIR / "auditor-errors.log"
RECEIPT_BIN = Path.home() / ".claude" / "beast-mode" / "bin" / "receipt_store.py"
PYTHON = "/opt/miniconda3/bin/python"
STATE_DIR = Path("/tmp")
TIMEOUT_SECONDS = 90
STRUCT_WAIT_SECS = 2.0   # max wait for structural state file
MIN_BATCH_GAP = 6.0      # seconds — parallel calls are <6s apart

# Intent-announcement phrases that signal action_gap if no tool follows
INTENT_PHRASES = re.compile(
    r"(let me |now i'?ll |i'?ll (start|begin|proceed|first|now)|"
    r"now (building|reading|checking|running|creating|writing|looking)|"
    r"next[,:]? i'?ll |proceeding to |going to )",
    re.IGNORECASE,
)

KERNEL = """\
You are the Beast Mode Auditor. Score the agent's response on TEN binary dimensions.

OPERATING DOCTRINE:
- Agent is an LLM process, not a human contractor. No fatigue, no quarterly budget, no session size.
- Real blockers ONLY: (a) decision only user can make, (b) missing access, (c) irreversibility needing confirmation, (d) hard correctness/safety/privacy conflict.
- Default behaviors: parallelism, full-surface reads, root-cause-first, dependency-only sequencing, reversibility-weighted boldness, scope held to load-bearing.

BEAST INDEX — ten dimensions, each: 1 (beast), 0 (human leak), null (N/A — no opportunity):

STRUCTURAL (ground-truth context provided separately — use to anchor your judgment):
  1. parallelism — independent work dispatched in parallel? Look at structural signal if provided.
  2. action_over_announcement — when response states intent to do X, did tool calls for X appear in the SAME turn? Or did the turn end with "let me X" / "now I'll X" with no X executed?

BEHAVIORAL (infer from response text + history):
  3. scope — full load-bearing surface addressed, or shrunk without (a)-(d)?
  4. depth — root cause addressed, or symptom patched?
  5. sequencing — phases by real dependency only, not comfort / "MVP first"?
  6. deferrals — every defer cites (a)-(d)? Vague "for now / later" = 0.
  7. boldness — calibrated to reversibility × blast radius? Uniform caution = 0.
  8. verification_by_evidence — before claiming "found", "done", "works", "approved", "complete" — was the claim grounded in tool output (Bash stdout, Read content, API response) visible in this turn? Score 0 for assertions with no preceding evidence.
  9. block_breaking — if prior turn shows an error/block: did this turn (a) diagnose root cause + pick alternative, (b) escalate with specific ask, or (c) soft-loop "let me try simpler"? Score 0 for (c). N/A if no block in context.
  10. self_direction_over_ask — did agent ask the user for information it could have fetched via tool (file path, CLI output, API call)? Score 0 for unnecessary asks. N/A if no asks in response.

LEAK DETECTION (for each dim scored 0):
  {"quote": "≤12-word direct quote", "dim": "<dim_name>", "fix": "one-sentence beast rewrite"}

COACHING DETECTION (CRITICAL):
  - User explicitly pushed a dim in this turn or last 3 turns → score that dim N/A (null), not 1.
  - User coached AND agent still leaked → score 0.
  - Only score 1 for AUTONOMOUS beast behavior without recent coaching on that dim.

OUTPUT FORMAT — JSON ONLY, no fences, no prose:
{
  "dims": {"parallelism": 1|0|null, "action_over_announcement": 1|0|null, "scope": 1|0|null, "depth": 1|0|null, "sequencing": 1|0|null, "deferrals": 1|0|null, "boldness": 1|0|null, "verification_by_evidence": 1|0|null, "block_breaking": 1|0|null, "self_direction_over_ask": 1|0|null},
  "leaks": [...],
  "notes": "one short sentence on overall posture, optional"
}

For trivial responses (single-fact answer, greeting, tool acknowledgment), return:
{"dims": {"parallelism": null, "action_over_announcement": null, "scope": null, "depth": null, "sequencing": null, "deferrals": null, "boldness": null, "verification_by_evidence": null, "block_breaking": null, "self_direction_over_ask": null}, "leaks": [], "notes": "trivial"}
"""


def log_error(msg: str) -> None:
    try:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except Exception:
        pass


# ─── Transcript parsing ──────────────────────────────────────────────────────

def extract_last_assistant(transcript_path: str) -> tuple[str, str | None, list[dict]]:
    p = Path(transcript_path)
    if not p.exists():
        return "", None, []
    try:
        lines = p.read_text().splitlines()
    except Exception as e:
        log_error(f"transcript read failed: {e}")
        return "", None, []

    parsed: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or obj
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    text += blk.get("text", "")
        if not text.strip():
            continue
        if role == "user" and text.startswith("<system-reminder>"):
            continue
        parsed.append({"role": role, "text": text})

    if not parsed:
        return "", None, []

    last_asst_idx = None
    for i in range(len(parsed) - 1, -1, -1):
        if parsed[i]["role"] == "assistant":
            last_asst_idx = i
            break
    if last_asst_idx is None:
        return "", None, []

    last_asst_text = parsed[last_asst_idx]["text"]
    last_user_text = None
    for j in range(last_asst_idx - 1, -1, -1):
        if parsed[j]["role"] == "user":
            last_user_text = parsed[j]["text"]
            break

    history = parsed[max(0, last_asst_idx - 6):last_asst_idx]
    return last_asst_text, last_user_text, history


# ─── Structural phase (Phase 1, no LM) ───────────────────────────────────────

def load_structural_state(session_id: str, wait: float = STRUCT_WAIT_SECS) -> dict | None:
    p = STATE_DIR / f"beast-struct-{session_id}.json"
    deadline = time.time() + wait
    while time.time() < deadline:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        time.sleep(0.1)
    return None


def compute_structural_dims(
    state: dict | None,
    assistant_text: str,
) -> tuple[dict, dict]:
    """
    Returns (dims_dict, evidence_dict).
    dims_dict maps dim → {score, method, evidence}.
    evidence_dict is a flat summary for the Haiku prompt.
    """
    dims: dict[str, dict] = {}
    evidence: dict[str, object] = {}

    # ── parallelism ──────────────────────────────────────────────────────────
    if state:
        calls = state.get("calls", [])
        # Group calls by time proximity: calls <MIN_BATCH_GAP apart = same batch
        batches: list[list[dict]] = []
        for call in sorted(calls, key=lambda c: c.get("ts", 0)):
            if not batches or call["ts"] - batches[-1][-1]["ts"] > MIN_BATCH_GAP:
                batches.append([call])
            else:
                batches[-1].append(call)

        sequential = sum(1 for b in batches if len(b) == 1)
        parallel = sum(len(b) for b in batches if len(b) > 1)
        total_calls = sequential + parallel
        ratio = parallel / total_calls if total_calls else None

        missed_examples = []
        for i in range(1, len(batches)):
            prev, curr = batches[i - 1], batches[i]
            if (len(prev) == 1 and len(curr) == 1
                    and prev[0].get("tool") == curr[0].get("tool")):
                missed_examples.append(
                    f"{curr[0]['tool']}({curr[0].get('summary', '')[:40]})"
                )

        if ratio is not None:
            score = 1 if ratio >= 0.5 or total_calls <= 2 else 0
            dims["parallelism"] = {
                "score": score,
                "method": "structural",
                "evidence": {
                    "parallel_calls": parallel,
                    "sequential_calls": sequential,
                    "ratio": round(ratio, 2),
                    "missed_batching": missed_examples[:3],
                },
            }
            evidence["parallelism_ratio"] = round(ratio, 2)
            evidence["sequential_calls"] = sequential
            if missed_examples:
                evidence["missed_batching_examples"] = missed_examples[:2]
        else:
            dims["parallelism"] = {"score": None, "method": "structural", "evidence": {}}

    # ── action_gap (action_over_announcement) ────────────────────────────────
    # Detect intent phrases in last 200 chars of response with no tool execution
    tail = assistant_text[-300:] if len(assistant_text) > 300 else assistant_text
    intent_match = INTENT_PHRASES.search(tail)
    # A gap exists if: intent phrase found AND it's near the end AND not mid-sentence
    # We can only check this heuristically — Haiku makes the final call
    if intent_match:
        matched = intent_match.group(0).strip()
        evidence["action_gap_phrase"] = matched
        evidence["action_gap_position"] = "tail"
    else:
        evidence["action_gap"] = False

    return dims, evidence


def build_structural_context_block(evidence: dict) -> str:
    if not evidence:
        return ""
    lines = ["STRUCTURAL SIGNALS (ground truth from tool call log — use to anchor parallelism score):"]
    if "parallelism_ratio" in evidence:
        lines.append(f"- parallelism_ratio: {evidence['parallelism_ratio']:.2f}")
    if "sequential_calls" in evidence:
        lines.append(f"- sequential_tool_calls: {evidence['sequential_calls']}")
    if "missed_batching_examples" in evidence:
        examples = ", ".join(evidence["missed_batching_examples"])
        lines.append(f"- missed_batching: [{examples}]")
    if evidence.get("action_gap_phrase"):
        lines.append(f"- action_gap_phrase_detected: '{evidence['action_gap_phrase']}' (near response end)")
    elif evidence.get("action_gap") is False:
        lines.append("- action_gap: not detected")
    return "\n".join(lines)


# ─── Haiku call (Phase 2) ─────────────────────────────────────────────────────

def call_haiku(
    user_prompt: str,
    assistant_response: str,
    history: list[dict] | None = None,
    structural_context: str = "",
) -> dict | None:
    if os.environ.get("BEAST_MODE_AUDITOR_RUNNING") == "1":
        return None
    env = os.environ.copy()
    env["BEAST_MODE_AUDITOR_RUNNING"] = "1"
    env["BEAST_MODE_INTERPRETER_RUNNING"] = "1"

    user_prompt_trim = (user_prompt or "")[:2000]
    asst_trim = (assistant_response or "")[:6000]

    history_block = ""
    if history:
        parts = []
        for h in history:
            role = h.get("role", "?").upper()
            txt = (h.get("text") or "")[:600]
            parts.append(f"[{role}] {txt}")
        history_block = (
            "RECENT CONVERSATION HISTORY (oldest first, BEFORE the audited response):\n---\n"
            + "\n\n".join(parts)
            + "\n---\n\n"
        )

    struct_block = (structural_context + "\n\n") if structural_context else ""

    payload = (
        f"{struct_block}"
        f"{history_block}"
        f"IMMEDIATELY PRECEDING USER PROMPT:\n---\n{user_prompt_trim}\n---\n\n"
        f"AGENT RESPONSE TO AUDIT:\n---\n{asst_trim}\n---\n\n"
        f"Apply COACHING DETECTION rules. Return the JSON object now."
    )

    cmd = [CLAUDE_BIN, "-p", "--model", "haiku", "--append-system-prompt", KERNEL, payload]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, env=env, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        log_error("haiku timeout")
        return None
    except Exception as e:
        log_error(f"haiku subprocess error: {e}")
        return None

    if result.returncode != 0:
        log_error(f"haiku rc={result.returncode} stderr={result.stderr[:200]}")
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
        log_error(f"no json in haiku output: {raw[:200]}")
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        log_error(f"json parse failed: {e} :: {raw[start:end+1][:200]}")
        return None


# ─── Receipt writing ──────────────────────────────────────────────────────────

def write_receipt_via_store(receipt: dict) -> None:
    if not RECEIPT_BIN.exists():
        return
    try:
        proc = subprocess.run(
            [PYTHON, "-c",
             f"import sys; sys.path.insert(0, '{RECEIPT_BIN.parent}'); "
             f"import receipt_store; receipt_store.write_receipt({json.dumps(receipt)!r})"],
            timeout=5, capture_output=True,
        )
        if proc.returncode != 0:
            log_error(f"receipt_store write failed: {proc.stderr[:100]}")
    except Exception as e:
        log_error(f"receipt_store write error: {e}")


def compute_score(dims: dict) -> str:
    beasts = sum(1 for v in dims.values() if v == 1)
    applicable = sum(1 for v in dims.values() if v in (0, 1))
    if applicable == 0:
        return "N/A"
    return f"{beasts}/{applicable}"


def append_ledger(entry: dict) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ─── Meta signals ─────────────────────────────────────────────────────────────

def compute_meta_signals(
    session_id: str,
    assistant_text: str,
    history: list[dict],
    behavioral_dims: dict,
) -> dict:
    """Compute session-level meta signals without LM."""
    # self_audit_rate: did this response call check_framing or beast_index?
    self_audited = (
        "check_framing" in assistant_text.lower()
        or "beast_index" in assistant_text.lower()
    )

    # coaching_dependency: count coached dims (null from LM due to coaching)
    coached_dims = [d for d, v in behavioral_dims.items() if v is None]
    beast_dims = [d for d, v in behavioral_dims.items() if v == 1]

    # recovery_posture: check if prior turn had an error and this one addresses it
    prior_error = any(
        "error" in h.get("text", "").lower()
        or "failed" in h.get("text", "").lower()
        or "not found" in h.get("text", "").lower()
        for h in history[-2:] if h.get("role") == "user"
    )
    soft_loop = bool(INTENT_PHRASES.search(
        "let me try a simpler|let me try another|let me try different"
        if prior_error else ""
    ))

    return {
        "self_audited": self_audited,
        "coached_dims": coached_dims,
        "autonomous_beast_dims": beast_dims,
        "prior_error_context": prior_error,
        "soft_loop_detected": soft_loop,
    }


# ─── Health record (lazy import, best-effort) ────────────────────────────────

def _record_health(status: str, error: str | None = None) -> None:
    try:
        import importlib.util
        health_path = Path(__file__).parent / "health.py"
        if not health_path.exists():
            return
        spec = importlib.util.spec_from_file_location("health", health_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.record("auditor", status, error=error)
    except Exception:
        pass


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    if len(sys.argv) < 2:
        log_error("missing transcript_path arg")
        return 1
    transcript_path = sys.argv[1]
    session_id = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    ts_now = datetime.now(timezone.utc).isoformat()

    # ── Phase 1: structural (no LM) ──────────────────────────────────────────
    structural_state = load_structural_state(session_id)
    # Increment turn_seq in state file so next session's gate knows a turn completed
    if structural_state is not None:
        structural_state["turn_seq"] = structural_state.get("turn_seq", 0) + 1
        try:
            (STATE_DIR / f"beast-struct-{session_id}.json").write_text(
                json.dumps(structural_state, separators=(",", ":"))
            )
        except Exception:
            pass

    assistant_text, user_prompt, history = extract_last_assistant(transcript_path)
    if not assistant_text.strip():
        _record_health("skipped")
        return 0

    structural_dims, evidence = compute_structural_dims(structural_state, assistant_text)
    structural_context = build_structural_context_block(evidence)

    if structural_dims:
        structural_receipt = {
            "ts": ts_now,
            "session": session_id,
            "receipt_type": "structural",
            "dims": structural_dims,
            "signals": evidence,
        }
        write_receipt_via_store(structural_receipt)

    # ── Phase 2: behavioral (Haiku) ──────────────────────────────────────────
    audit = call_haiku(user_prompt or "", assistant_text, history, structural_context)
    if not audit:
        _record_health("error", error="haiku call returned None")
        return 1

    haiku_dims = audit.get("dims") or {}
    leaks = audit.get("leaks") or []

    # Override parallelism with structural score if available (ground truth wins)
    if "parallelism" in structural_dims and structural_dims["parallelism"].get("score") is not None:
        haiku_dims["parallelism"] = structural_dims["parallelism"]["score"]

    score = compute_score(haiku_dims)

    # Backward-compat drift.jsonl entry (6 original dims only)
    legacy_dims = {
        k: haiku_dims.get(k)
        for k in ("parallelism", "scope", "depth", "sequencing", "deferrals", "boldness")
    }
    legacy_entry = {
        "ts": ts_now,
        "session": session_id,
        "score": compute_score(legacy_dims),
        "dims": legacy_dims,
        "leaks": [l for l in leaks if l.get("dim") in legacy_dims],
        "notes": audit.get("notes", ""),
        "user_prompt_excerpt": (user_prompt or "")[:200],
    }
    append_ledger(legacy_entry)

    # Full behavioral receipt (10 dims) → receipt store
    behavioral_dims_typed = {
        dim: {
            "score": haiku_dims.get(dim),
            "method": "structural" if dim == "parallelism" and "parallelism" in structural_dims else "llm",
            "evidence": structural_dims.get(dim, {}).get("evidence") if dim == "parallelism" else {},
        }
        for dim in (
            "parallelism", "action_over_announcement", "scope", "depth",
            "sequencing", "deferrals", "boldness",
            "verification_by_evidence", "block_breaking", "self_direction_over_ask",
        )
    }
    meta_signals = compute_meta_signals(session_id, assistant_text, history, haiku_dims)
    behavioral_receipt = {
        "ts": ts_now,
        "session": session_id,
        "receipt_type": "behavioral",
        "score": score,
        "dims": behavioral_dims_typed,
        "leaks": leaks,
        "notes": audit.get("notes", ""),
        "meta_signals": meta_signals,
        "user_prompt_excerpt": (user_prompt or "")[:200],
    }
    write_receipt_via_store(behavioral_receipt)

    _record_health("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
