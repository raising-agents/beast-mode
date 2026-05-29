# Beast Mode — Deep Review (2026-05-29)

Reviewer: Claude (Opus 4.7), on Adrian's machine.
Scope: full `beast-mode/` repo + live ledger + receipt store + skills2 (reference only, not template).

---

## 1. Current state — ground truth from ledger

Ledger: `~/.claude/beast-mode/ledger/drift.jsonl` — **20,978 rows**.

Sample of last 200 turns (dim aggregation):

| Dim | BEAST (1) | LEAK (0) | N/A (null) | Leak rate when applicable |
|---|---|---|---|---|
| parallelism | 193 | 3 | 4 | 1.5% |
| scope | 97 | 76 | 27 | **44%** |
| deferrals | 41 | 48 | 111 | 54% (sparse) |
| boldness | 82 | 48 | 70 | 37% |
| depth | 83 | 44 | 73 | 35% |
| sequencing | 59 | 29 | 112 | 33% (sparse) |

Key findings:
- **Parallelism solved** (structural override works).
- **Scope is the worst hot dim** — 44% leak rate, applies in 87% of turns.
- **Sequencing + deferrals are sparse** — N/A more often than they fire. Half the index is mostly inert.
- Score distribution shows wide spread (1/3 to 6/6) — no monotonic improvement trend visible.

Other artifacts:
- `~/.claude/beast-mode/proposals/` — **1 file: `2026-05-24-error.md`**. Opus call failed. Weekly evolution loop has never shipped a successful amendment.
- `~/.claude/beast-mode/receipts/` — **only `2026-05-21.jsonl`**. Typed receipt store essentially inert.
- No calibration signal from Adrian recorded anywhere. Judge marks own homework.

---

## 2. Architectural strengths (keep these)

| Component | Why it works |
|---|---|
| Stop-hook background spawn | Never blocks user. Fail-open. Clean. |
| Structural collector + parallelism gate | Ground-truth signal, no LM. Eliminates judge ambiguity on dim 1. |
| Recursion guards via env vars | Prevents auditor-auditing-auditor loops. |
| Two-tier ledger (legacy drift.jsonl + typed receipts) | Backward compat preserved while v2 evolves. |
| Constitution as single source of truth | Stop hook, MCP, auditor, evolution all read it. |
| Cost discipline (~$1/month) | Haiku for audit, Opus only for evolution. Right model per phase. |
| Coaching detection | Auditor distinguishes autonomous beast vs coached beast. |
| Action_gap detection (heuristic + LM) | Catches "let me X" with no X — a real high-leverage antipattern. |
| Cheap, local, no-API-key, keychain-only | Zero secrets risk. |

These are load-bearing. Do not refactor.

---

## 3. Six root weaknesses

### W1. Open loop — audit informs nothing downstream
Audit fires *after* response. Findings land in ledger. Nothing pipes them into the next turn's prompt. Constitution stays static. Agent re-leaks same patterns. Evidence: scope leaked 76× in 200 consecutive turns despite being the explicit dim 3.

### W2. No Adrian-calibrated ground truth
Haiku judge kernel = constitution. Judge scores response by checking compliance with same doctrine that wrote the response. Self-consistent ≠ correct. No mechanism captures Adrian's actual corrections (when he says "no, stop, don't") as labeled data. System measures itself by its own ruler.

### W3. Evolution loop crashed and stayed crashed
`2026-05-24-error.md` is the only artifact. No retry, no alert, no fallback to Sonnet, no health check. Self-improvement loop is theoretical. Static Constitution v1 forever.

### W4. Judge marks own homework
No cross-LM check, no inverse-prompt test, no eval set for the auditor itself. If Haiku miscalibrates a dim (e.g., scores "let's start with a basic version" as N/A instead of 0), drift is invisible.

### W5. Dim sparsity — half the index inert
sequencing N/A 112/200, deferrals N/A 111/200. Half of every audit prompt budgets tokens for dims that rarely apply. Lower signal density → slower learning → noisier digests.

### W6. Single-knob improvement mechanism — prose-only
Even when amendments land, the only knob is "edit the markdown". No phrase blocklist, no learned-rule table, no per-task-type kernel, no auto-promoted antibodies. Behavior change requires Adrian to read and merge a PR. Coarse instrument, slow cadence.

---

## 4. Reference patterns from skills2 — adopt principles, reject architecture

skills2 (`~/raising-agents/skills2/`) solves a different problem (per-skill contracts) but its *principles* map onto Beast Mode well:

| skills2 principle | Beast Mode adoption |
|---|---|
| Machine-readable manifest, not prose | Constitution rules as queryable table (YAML/TOML/SQLite): `pattern`, `freq`, `accuracy`, `status`, `example_quote`. |
| `@step` decorator → telemetry | Already in place via structural collector. Extend with verification + action_gap structural detectors. |
| 3-axis trust score (FIDELITY × COVERAGE × ANCHORING) | Beast equivalent: parallelism-rate × scope-rate × verification-rate — only on dims with high applicability. |
| Hard BLOCKED gate on trust < threshold | Adopt sparingly. One Stop-hook hard gate for unambiguous structural antipatterns (e.g., final "let me X" with no X). |
| Eval cases gating version bumps | Apply to the **judge itself** — 20–30 Adrian-labeled cases auditor must classify correctly before kernel change ships. |

What NOT to copy:
- Per-skill Pydantic schemas — Beast is one cross-cutting runtime, not N skills.
- Per-skill bundle layout — Constitution is global doctrine.
- `@step` decorator on agent behavior — agent doesn't run Python; agent runs tokens.

---

## 5. Behavior-change theory of operation (proposed)

Three concentric loops with different time constants:

```
┌─ Loop 3 (monthly): Constitution amendment ───────────────────┐
│  evolution.py (Sonnet) digests month → markdown PR → Adrian  │
│  reviews → merge → frozen doctrine layer updated             │
│  Time constant: ~30 days                                     │
│                                                              │
│  ┌─ Loop 2 (daily): Antibody promotion ──────────────────┐  │
│  │  daily_digest.py reads last-7-day ledger →            │  │
│  │  promotes recurring leak phrases to blocklist.yaml →  │  │
│  │  updates MEMORY.md leak summary                       │  │
│  │  Time constant: ~24h                                  │  │
│  │                                                       │  │
│  │  ┌─ Loop 1 (per-turn): Pre-turn injection ────────┐ │  │
│  │  │  UserPromptSubmit hook reads:                  │ │  │
│  │  │   - top-3 active leak dims (last 7d)           │ │  │
│  │  │   - active blocklist phrases                   │ │  │
│  │  │   - last Adrian correction (if recent)         │ │  │
│  │  │  → injects as <system-reminder> in prompt      │ │  │
│  │  │  Time constant: next turn                      │ │  │
│  │  └────────────────────────────────────────────────┘ │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
              │                                ▲
              ▼                                │
   Stop hook auditor (current) → ledger ───────┘
              │
              ▼
   Calibration channel ← Adrian corrections (regex on user prompts)
                       ← /beast confirm|reject (sampled)
```

Loop 1 closes the open loop today. Loop 2 makes behavior change daily, not monthly. Loop 3 keeps doctrine human-reviewed for the hardest changes.

Separation matters:
- **Doctrine layer** (frozen, Adrian-edited): the Constitution. Rare changes. Source of truth for *why*.
- **Antibody layer** (auto-promoted, Adrian-prunable): blocklist.yaml. Frequent changes. Captures *what to avoid* this week.
- **Calibration layer** (Adrian-confirmed): labeled corrections + sampled votes. Source of truth for *judge accuracy*.

---

## 6. Concrete improvements — ranked by leverage

### Tier 1 — Close the loop (no behavior change happens without these)

**T1.1 — Pre-turn injection hook** (NEW `~/.claude/hooks/beast-pre-turn.py`, UserPromptSubmit)
Read last 7d ledger → top-3 leak dims with worst rate + 1-2 quoted example phrases each → inject as `<system-reminder>`. Cost: <50ms read + ~300 tokens. Effect: next-turn behavior change. Closes the open loop.

**T1.2 — Calibration channel from Adrian** (NEW)
Two surfaces, both cheap:
- (a) Correction-detector in UserPromptSubmit: regex on user prompts for "no don't", "stop", "I said", "you missed", "ignore that", "redo", "wrong". Auto-label prior turn's ledger entry as confirmed leak. Write to `~/.claude/beast-mode/calibration/corrections.jsonl`.
- (b) `/beast confirm <turn_id>` / `/beast reject <turn_id>` slash commands for sampled review (1-of-N turns shown to Adrian).

Without this, the audit pipeline is a hall of mirrors.

**T1.3 — Auto-promoted phrase blocklist** (NEW `~/.claude/beast-mode/rules/blocklist.yaml`)
Schema: `phrase`, `dim`, `hits`, `promoted_at`, `status` (candidate / active / retired). Promotion: when leak phrase repeats ≥N times (start N=8) with consistent labeling, promote to `active`. Pre-turn hook injects active blocklist as "DO NOT say: ...". Adrian retires via `/beast retire <phrase>`. This is the actual behavior-change mechanism — Constitution = doctrine, blocklist = learned habits.

### Tier 2 — Make audit honest

**T2.1 — Eval set for the judge** (NEW `~/.claude/beast-mode/eval/judge_cases.jsonl`)
20–30 Adrian-labeled cases: `prompt + response → expected dims`. Bootstrapped from T1.2 corrections. CI on `auditor-worker.py`: kernel change must score eval set ≥ 90% match before shipping. Kills silent judge drift.

**T2.2 — Cross-LM spot check**
5–10% of turns: run audit with Sonnet in addition to Haiku. Disagreements logged to `~/.claude/beast-mode/ledger/judge_drift.jsonl`. If Haiku/Sonnet disagree on dim X repeatedly, retire/refactor dim X. Cost: ~$0.10/month extra.

**T2.3 — Fix + de-risk evolution loop**
- Switch Opus → Sonnet (faster, cheaper, more reliable for this task size).
- Health check: if last 3 runs errored, statusline shows red `EVOL BROKEN` indicator.
- Cadence split: daily lightweight Sonnet digest (updates `MEMORY.md` leak summary + promotes blocklist phrases) + monthly Sonnet/Opus amendment PR (Constitution edits only).

### Tier 3 — Sharpen the instrument

**T3.1 — Reduce dims 10 → 5; expand structural coverage**
- Keep: parallelism [S], action_over_announcement [S+LM], scope [LM], depth [LM], verification_by_evidence [S+LM].
- Retire / merge: sequencing (sparse), deferrals (sparse + overlaps scope), self_direction_over_ask (rarely fires), block_breaking (rarely applies), boldness (subjective).
- Add structural detector for `verification`: did response claim "done/works/found/complete" with no tool result in same turn? Regex + tool-call timestamp diff.

**T3.2 — Action-gap hard gate** (extend `beast-parallelism-gate.py` pattern)
Stop pre-check: if response ends with "let me X / now I'll X" and no tool call followed in same turn, inject `systemMessage` requesting tool call or rewording. One additional hard gate, high-confidence pattern only.

**T3.3 — Per-task-type kernel**
Classify task at UserPromptSubmit: `code-edit` / `research` / `planning` / `chat` / `meta` (config / setup). Each loads tailored dim subset and tailored example phrases. Reduces N/A noise.

### Tier 4 — Visibility = pressure

**T4.1 — Statusline delta + correction-rate**
Add to `statusline.sh`: 7d vs 30d delta arrow (↑/↓), Adrian-correction rate per last 50 turns. Real proxy for "is this working?". If correction-rate down while Beast Index up → real improvement. If correction-rate flat while Beast Index up → judge drift.

**T4.2 — `/beast rules` CLI view**
One screen: top 10 learned blocklist rules, their hit counts, last triggered, age. Adrian prunes via `/beast retire <id>` or promotes candidate → active.

---

## 7. What changes after Tier 1 + Tier 2 ship

| Today | After |
|---|---|
| Audit → ledger → dead | Audit → ledger → next-turn injection + daily promotion + monthly amendment |
| Judge marks own homework | Judge graded against Adrian-labeled eval set + Sonnet spot-check |
| Constitution static prose only | Two layers: frozen doctrine + learned antibodies (table-driven) |
| Evolution loop crashed silently | Daily Sonnet + monthly amendment + EVOL health badge |
| No Adrian calibration signal | Correction detector + sample-vote build labeled set |
| Behavior change ETA: never | Next turn (blocklist) → next day (digest) → next month (doctrine) |
| 10 dims, half sparse | 5 dense dims + 2 structural hard gates |

---

## 8. Risks / caveats

- **Blocklist false positives**: "for now" can be legitimate. Start with N=8 hits + manual Adrian approval for first 4 weeks. Then drop to N=5.
- **Pre-turn injection bloat**: 300 tokens × every turn is fine. Cap at 500. Never grow unbounded — rotate stale entries.
- **Correction detector misfires**: "no, actually I meant..." not always a leak signal. Label as *candidate*, require N=2 same-week or `/beast confirm` to upgrade.
- **`/beast confirm` fatigue**: cap to 1 prompt per session, sampled. Cold-start uses synthetic eval cases.
- **Evolution Sonnet vs Opus**: Sonnet may produce weaker constitutional changes. Worth the reliability tradeoff. Can A/B against Opus on monthly cadence later.
- **N/A vs leak attribution**: current coaching-detection logic can over-N/A. Audit the auditor: how many recent N/A scores should actually have been 0?

---

## 9. Non-goals (explicit cuts)

These are out of scope for Beast Mode self-improvement and should not be conflated:
- Per-skill contract enforcement (that's skills2's job — different problem).
- Replacing the Constitution with a DSL.
- Real-time mid-response intervention beyond the existing parallelism / proposed action_gap gates.
- A web dashboard. Statusline + CLI are sufficient.
- Cross-machine sync of ledger / blocklist. Local-only is fine.

---

## 10. Decisions locked (2026-05-29, Adrian)

- **D1 (blocklist promotion)**: Candidate-only for first 4 weeks. After that: auto-promote with notification to Adrian. Reversible via `/beast retire <id>`.
- **D2 (`/beast confirm` cadence)**: End-of-session batch of 3, opt-in. No mid-session interruption.
- **D3 (dim taxonomy)**: Open schema with two layers.
  - **Core layer (frozen)**: 6 dims. Retire `sequencing` + `deferrals` (logic absorbed into `scope` + `depth`). Keep `boldness` reframed as `reversibility-calibration` (structural input: count irreversible tool calls per turn). Core dims edit only via monthly Constitution amendment.
  - **Emergent layer (mutable)**: starts empty. Auditor emits `free_form_leak` when antipattern doesn't fit core. Daily digest clusters by suggested label + semantic similarity. Cluster ≥10 hits across ≥3 sessions → promote candidate → active emergent dim. Active dim injected into next-turn auditor kernel.
  - **Decay**: emergent dim fires 0× in 4 weeks → demote to retired with Adrian notification. Core dims never auto-decay.
  - Schema lives in `~/.claude/beast-mode/rules/dims.yaml`. New work package WP-11 covers discovery + decay machinery.
- **D4 (evolution model)**: Sonnet for daily digest. Opus for monthly amendment only.
- **D5 (calibration ledger path)**: Separate dir `~/.claude/beast-mode/calibration/`. Clean separation from `ledger/drift.jsonl` (agent self-audit vs Adrian-labeled ground truth).

---

## 11. References

- Constitution: `~/.claude/instructions/beast-mode-constitution.md`
- Ledger: `~/.claude/beast-mode/ledger/drift.jsonl`
- Auditor: `bin/auditor-worker.py`
- Stop hook: `hooks/beast-mode-stop.py`
- Structural collector: `hooks/beast-structural-collector.py`
- Parallelism gate: `hooks/beast-parallelism-gate.py`
- Evolution: `bin/evolution.py` (broken since 2026-05-24)
- Statusline: `bin/statusline.sh`
- Reference (do not copy): `~/raising-agents/skills2/` — adopt principles, not architecture.
