# Beast Mode — Work Packages (2026-05-29)

Companion to `2026-05-29-deep-review.md`. Each package is a self-contained, shippable unit. Ordered by dependency, not by effort.

Status legend: `BLOCKED` (waits on prior) · `READY` (independent) · `PARALLEL` (can ship same turn as another READY).

---

## WP-0 — Pre-flight repairs (READY)

**Goal**: stop bleeding from the existing pipeline before adding layers.

**Tasks**:
1. Diagnose `2026-05-24-error.md` — why did Opus call fail? Add to `evolution.py`:
   - subprocess stderr capture into proposal file (not just on success).
   - Retry once with `--model sonnet` if Opus fails.
   - Exit non-zero on persistent failure so launchd surfaces it.
2. Add health file: `~/.claude/beast-mode/health.json` written by `evolution.py` and `auditor-worker.py` each run. Fields: `last_run_ts`, `last_status`, `consecutive_failures`.
3. Statusline reads `health.json` — show `EVOL ERR` badge if `consecutive_failures >= 3`.

**Deliverables**: patched `evolution.py`, new `health.json` contract, updated `statusline.sh`.

**Acceptance**: force-run `evolution.py` → either ship `*-amendment.md` OR write `health.json` with failure cause. Statusline reflects state.

**Files touched**: `bin/evolution.py`, `bin/statusline.sh`, new `bin/health.py` (shared library).

**Risk**: low — defensive only, no behavior change.

---

## WP-1 — Calibration ledger + correction detector (READY, PARALLEL with WP-2)

**Goal**: capture Adrian's actual corrections as labeled ground truth.

**Tasks**:
1. New dir: `~/.claude/beast-mode/calibration/`.
2. New file: `~/.claude/beast-mode/calibration/corrections.jsonl`. Schema:
   ```json
   {"ts": "...", "session": "...", "prior_turn_id": "...",
    "user_quote": "...", "matched_pattern": "no_dont",
    "confidence": "candidate|confirmed", "auto_labeled_dim": "scope"}
   ```
3. New hook: `~/.claude/hooks/beast-correction-detector.py` (UserPromptSubmit).
   - Regex set: `^(no|stop|don't|wait|wrong|undo|redo|ignore)\b`, `\byou (missed|forgot|ignored)\b`, `\bI (said|told you|asked for)\b`.
   - On match: look up prior turn's audit entry, write candidate correction.
4. Promotion rule: candidate → confirmed when same dim leak appears in ≥2 corrections within 7 days, OR Adrian runs `/beast confirm <turn_id>`.

**Deliverables**: hook script, calibration dir, schema doc in `notes/calibration-schema.md`.

**Acceptance**: trigger detector with synthetic user prompt "no, you missed the auth case" — verify candidate correction lands in JSONL with correct prior_turn_id.

**Files touched**: new `hooks/beast-correction-detector.py`, `~/.claude/settings.json` (hook registration).

**Risk**: medium — false positives possible. Mitigation: candidate-only for first 4 weeks, no auto-action until Adrian audits the candidate stream.

---

## WP-2 — Phrase blocklist (READY, PARALLEL with WP-1)

**Goal**: build the auto-promoted "do not say" layer.

**Tasks**:
1. New file: `~/.claude/beast-mode/rules/blocklist.yaml`. Initial schema:
   ```yaml
   - id: ab001
     phrase: "let's start with a basic"
     pattern_type: substring  # substring|regex
     dim: scope
     hits: 0
     status: candidate  # candidate|active|retired
     promoted_at: null
     last_triggered: null
     example_quote: "let's start with a basic version and iterate"
     notes: "..."
   ```
2. New script: `bin/blocklist_promoter.py`.
   - Reads last 7-day ledger leaks.
   - Extracts leak `quote` field, normalizes (lowercase, trim).
   - Counts repeated substrings (≥4-word phrases).
   - Promotes `candidate → active` when hits ≥ 8 AND dim consistent across all hits.
   - Logs all promotions to `~/.claude/beast-mode/rules/promotion.log`.
3. CLI: `/beast rules` shows table. `/beast retire <id>` flips status to `retired`.

**Deliverables**: blocklist.yaml seed (top 10 phrases from current ledger), `blocklist_promoter.py`, CLI wiring.

**Acceptance**: run promoter on existing ledger → produces blocklist.yaml with realistic candidates (likely "let's start with", "for now", "we can revisit", etc.). Adrian reviews seed list manually first time.

**Files touched**: new `bin/blocklist_promoter.py`, new `rules/blocklist.yaml`, new `bin/beast_cli.py` (or extend existing).

**Risk**: low for candidate generation. Medium for active promotion — guard with manual review for first month.

---

## WP-3 — Pre-turn injection hook (BLOCKED by WP-2)

**Goal**: close the open loop. Inject leak summary + active blocklist into every new user prompt.

**Tasks**:
1. New hook: `~/.claude/hooks/beast-pre-turn.py` (UserPromptSubmit).
2. Reads:
   - Last 7d ledger → top-3 leak dims by rate (only dims with ≥10 applicable scores).
   - Up to 2 quoted example phrases per dim.
   - Active blocklist phrases from `rules/blocklist.yaml`.
3. Emits as `<system-reminder>`:
   ```
   <system-reminder>
   BEAST RECENT DRIFT (last 7d):
   - scope (leak rate 44%): "let's start with a basic" (14×), "for now" (9×)
   - depth (leak rate 35%): "I'll add a try/except" (6×)
   DO NOT use these phrases. Cite (a)-(d) or do the work.
   </system-reminder>
   ```
4. Cap: 500 tokens. Cap: 3 dims + 5 blocklist phrases per turn.
5. Idle guard: skip injection if zero leaks in last 7d (cold-start session).

**Deliverables**: hook script, settings.json registration, output cap test.

**Acceptance**: empty ledger → no injection. Synthetic ledger with 14 scope leaks → injection contains top quote.

**Files touched**: new `hooks/beast-pre-turn.py`, `~/.claude/settings.json`.

**Risk**: medium — wrong phrases could overcorrect agent. Mitigation: dry-run mode first (write injection to log, don't actually inject) for 3 days; then enable.

**Depends on**: WP-2 (needs `blocklist.yaml`).

---

## WP-4 — Daily digest (BLOCKED by WP-2, parallel with WP-3)

**Goal**: tighten Loop 2 from weekly → daily. Lightweight.

**Tasks**:
1. New script: `bin/daily_digest.py`.
   - Reads last 24h ledger.
   - Calls Sonnet with smaller kernel (not Opus, not weekly digest).
   - Writes to `~/.claude/beast-mode/digests/{YYYY-MM-DD}-digest.md`.
   - Updates `MEMORY.md`-style pointer: `~/.claude/beast-mode/digests/LATEST.md` (single-page rolling summary).
   - Triggers `blocklist_promoter.py` after writing.
2. New launchd plist: `bin/com.adrian.beast-mode-daily.plist`. Runs 06:30 local.
3. Cost cap: skip if <10 ledger entries in last 24h.

**Deliverables**: `daily_digest.py`, plist, sample output.

**Acceptance**: manual run → produces digest + updates LATEST.md + triggers promoter without error.

**Files touched**: new `bin/daily_digest.py`, new plist.

**Risk**: low. Sonnet calls reliable.

**Depends on**: WP-2 (promoter).

---

## WP-5 — Eval set for the judge (BLOCKED by WP-1)

**Goal**: ground-truth for the auditor. Prevent silent judge drift.

**Tasks**:
1. New file: `~/.claude/beast-mode/eval/judge_cases.jsonl`. Schema:
   ```json
   {"case_id": "...", "user_prompt": "...", "agent_response": "...",
    "expected_dims": {"scope": 0, "depth": 1, ...},
    "labeled_by": "adrian|auto", "ts": "..."}
   ```
2. Seed set (20 cases): pull from corrections.jsonl (WP-1) + Adrian-curated edge cases.
3. New script: `bin/judge_eval.py`.
   - Runs current `auditor-worker.call_haiku()` against each case.
   - Computes per-dim accuracy.
   - Writes `~/.claude/beast-mode/eval/results-{ts}.json`.
   - Returns non-zero exit if accuracy < 0.90.
4. CI hook: before any kernel/Constitution change ships, run `judge_eval.py`. Block on regression.

**Deliverables**: eval format, seed cases, runner script, gate documentation in `notes/judge-eval.md`.

**Acceptance**: run against current Haiku kernel → produces accuracy score. Mutate kernel (remove one rule) → accuracy drops. Gate triggers.

**Files touched**: new `eval/`, `bin/judge_eval.py`, README update.

**Risk**: medium — Adrian-labeling 20 cases takes real wall-clock time. Bootstrap from WP-1 corrections to reduce manual labeling burden.

**Depends on**: WP-1 (corrections feed seed cases).

---

## WP-6 — Cross-LM spot check (BLOCKED by WP-5)

**Goal**: detect judge drift in production, not just at kernel-change time.

**Tasks**:
1. Modify `auditor-worker.py`: 5–10% sampling (env var `BEAST_CROSS_JUDGE_RATE=0.07`).
2. When sampled, also call Sonnet with same prompt.
3. Write disagreements to `~/.claude/beast-mode/ledger/judge_drift.jsonl`. Schema:
   ```json
   {"ts": "...", "turn_id": "...",
    "haiku_dims": {...}, "sonnet_dims": {...},
    "disagreed_dims": ["scope", "depth"]}
   ```
4. Daily digest (WP-4) surfaces disagreement-rate-per-dim.

**Deliverables**: patched auditor, drift jsonl, digest extension.

**Acceptance**: 100 turns → ~7 cross-checked, disagreement rate logged.

**Files touched**: `bin/auditor-worker.py`, `bin/daily_digest.py`.

**Risk**: low. Cost: ~$0.10/month extra.

**Depends on**: WP-5 (eval set exists to interpret the disagreements).

---

## WP-7 — Dim reduction + structural verification detector (READY after WP-0)

**Goal**: cut noise. 10 dims → 5 dense dims. Add structural verification detector.

**Tasks**:
1. Constitution amendment (manual, via PR): retire `sequencing` + `deferrals` (move logic into `scope`). Keep `boldness`, `depth`, `scope`, `parallelism`, `action_over_announcement`, `verification_by_evidence`. Document rationale.
2. Patch `auditor-worker.py` KERNEL: 6 dims max.
3. New structural detector in `beast-structural-collector.py`:
   - After Stop, parse final assistant text for claim phrases (`done|works|found|complete|fixed|approved`).
   - Check if any tool call returned in same turn within 30s before that text.
   - Score `verification_by_evidence` structurally (1 if tool result preceded claim, 0 otherwise, N/A if no claim).
4. Auditor still calls Haiku for the LM dims, but verification dim becomes structural-overridden (same pattern as parallelism).
5. Run WP-5 eval before/after to confirm accuracy held.

**Deliverables**: amended Constitution, patched auditor, patched structural collector, eval before/after.

**Acceptance**: kernel size reduced. Eval accuracy steady (±0.02). Per-dim applicability rates rise.

**Files touched**: `constitution/beast-mode-constitution.md`, `bin/auditor-worker.py`, `hooks/beast-structural-collector.py`.

**Risk**: medium — retiring dims loses signal on edge cases. Mitigation: WP-5 eval gate + Adrian review of cuts.

**Depends on**: WP-0 (health infra) + WP-5 (eval gate). Can ship before WP-3/4 if needed.

---

## WP-8 — Action-gap hard gate (READY after WP-0)

**Goal**: second mid-turn hard gate beyond parallelism. Highest-confidence structural antipattern.

**Tasks**:
1. New hook: `~/.claude/hooks/beast-action-gap-gate.py` (Stop, pre-finalize — confirm Stop hook supports systemMessage injection or use PostToolUse / Stop combo).
2. On Stop: if final text matches intent regex AND no tool call in last 60s of the turn, inject `systemMessage`: "Action-gap: final intent phrase with no executed tool call. Either execute or remove intent phrase."
3. Fires at most 1×/session/pattern (same approach as parallelism gate).

**Deliverables**: gate hook, settings registration, test fixtures.

**Acceptance**: synthetic transcript ending in "let me check the config" with no Read call → gate fires once.

**Files touched**: new `hooks/beast-action-gap-gate.py`, `~/.claude/settings.json`.

**Risk**: medium — false positives if Stop fires before final tool call recorded. Mitigation: 60s grace window; dry-run mode first.

**Depends on**: WP-0 only.

---

## WP-9 — Per-task-type kernel (BLOCKED by WP-7)

**Goal**: reduce N/A noise by tailoring dim set to task class.

**Tasks**:
1. Task classifier at UserPromptSubmit:
   - Regex + small Haiku classifier on first user message (cached per session).
   - Classes: `code-edit` / `research` / `planning` / `chat` / `meta`.
2. Kernel manifest: `~/.claude/beast-mode/kernels/`:
   - `code-edit.json` — dims: scope, depth, verification, parallelism, action_over_announcement.
   - `research.json` — dims: scope, depth, verification (no parallelism, no action_over_announcement).
   - `planning.json` — dims: scope, depth, boldness.
   - `chat.json` — dims: action_over_announcement only.
3. Auditor reads task class from session state, loads appropriate kernel.

**Deliverables**: classifier, kernel JSONs, auditor patch.

**Acceptance**: research task → no parallelism scoring. Code-edit task → all five fire.

**Files touched**: new `bin/task_classifier.py`, new `kernels/*.json`, `bin/auditor-worker.py`.

**Risk**: medium — classifier mistakes muddy data. Mitigation: log classifications; Adrian audits monthly.

**Depends on**: WP-7 (dim set stable first).

---

## WP-11 — Emergent dim discovery + decay (BLOCKED by WP-4 + WP-7)

**Goal**: open dim schema. Discover new antipattern categories from leak clusters. Decay silent ones.

**Theory of operation**:

Core dims are frozen doctrine. Emergent dims are observation — what's actually leaking in real turns that the core dims don't capture. New behaviors leak in ways the original Constitution never anticipated; the schema has to grow.

**Tasks**:

1. **Dim manifest** (`~/.claude/beast-mode/rules/dims.yaml`):
   ```yaml
   - id: scope
     layer: core
     status: active
     promoted_at: 2026-05-20
     last_fired_ts: 2026-05-28T09:05:05Z
     fire_count_7d: 76
     fire_count_30d: 312
     doctrine_section: "III. Scope-shrinking"
     decay_protected: true  # core dims never auto-decay

   - id: emergent_premature_abstraction
     layer: emergent
     status: candidate  # candidate | active | retired
     discovered_at: 2026-05-25
     promoted_at: null
     last_fired_ts: 2026-05-28T...
     cluster_size: 7
     example_quotes:
       - "let me extract this into a helper"
       - "I'll create a base class for..."
       - "let's add an abstraction here"
     suggested_kernel_text: "premature_abstraction — adding abstraction layers (helpers, base classes, factories) without ≥2 concrete consumers"
     decay_protected: false
   ```

2. **Patch auditor kernel** (`bin/auditor-worker.py` KERNEL string):
   Add `free_form_leak` field to output JSON:
   ```
   "free_form_leaks": [
     {"quote": "...", "suggested_dim_name": "short-snake-case",
      "rationale": "why this doesn't fit existing dims", "fix": "..."}
   ]
   ```
   Haiku emits this when a leak is clear but no core dim labels it.

3. **Discovery pass** (extend `bin/daily_digest.py` or new `bin/dim_discoverer.py`):
   - Read last 7d ledger free_form_leaks.
   - Cluster by `suggested_dim_name` (exact match first; Sonnet pass for semantic merge of synonyms).
   - For each cluster ≥10 hits AND ≥3 distinct sessions AND coherent (Sonnet validates):
     - Write as `candidate` to `dims.yaml`.
     - Generate `suggested_kernel_text` (Sonnet, 1 sentence).
     - Notify Adrian via daily digest section "Emergent dim candidates".
   - Adrian promotes via `/beast dim promote <id>` or rejects via `/beast dim reject <id>`.
   - Auto-promote: candidate → active after 4 weeks if hit count keeps growing AND Adrian hasn't rejected (mirrors D1 blocklist policy).

4. **Active emergent dim injection** (modify `auditor-worker.py`):
   - Before calling Haiku, read `dims.yaml` for `status: active` entries.
   - Append emergent dim definitions to kernel dynamically.
   - Haiku scores them alongside core dims. Format: prefix with `e_` so ledger/digest can distinguish from core.

5. **Decay sweep** (weekly, in `evolution.py` or new `bin/dim_decay.py`):
   - For each emergent dim where `decay_protected: false`:
     - If `last_fired_ts` > 28 days ago → status: `retired`. Notify Adrian.
     - If retired ≥ 90 days → archive to `dims.archive.yaml`, remove from active list.
   - Core dims: log fire counts but never auto-decay. Constitution amendment only.

6. **CLI** (extend `bin/beast_cli.py`):
   - `/beast dim list` — show all dims with layer, status, fire count, age.
   - `/beast dim promote <id>` — candidate → active.
   - `/beast dim reject <id>` — candidate → retired.
   - `/beast dim revive <id>` — retired → active (manual override).
   - `/beast dim doctrine <id>` — show suggested_kernel_text for review.

**Deliverables**: dims.yaml schema, patched auditor kernel, discoverer script, decay sweep, CLI subcommands.

**Acceptance**:
- Seed dims.yaml with 6 core dims (post WP-7).
- Run auditor on synthetic leak outside core → free_form_leak emitted.
- Synthetic ledger with 12 free_form_leaks suggesting same name across 4 sessions → candidate emergent dim appears in dims.yaml.
- Synthetic dim with `last_fired_ts` 30 days ago → decay sweep retires it.

**Files touched**: new `rules/dims.yaml`, `bin/auditor-worker.py`, `bin/daily_digest.py` or new `bin/dim_discoverer.py`, new `bin/dim_decay.py`, `bin/beast_cli.py`.

**Risk**:
- **Dim sprawl** — Haiku invents many small dim names. Mitigation: semantic clustering merges synonyms; high promotion threshold (10 hits, 3 sessions); Adrian-prunable.
- **Overlap with core** — emergent dim duplicates a core dim. Mitigation: discoverer checks similarity to existing dims before promotion; auto-flag for Adrian.
- **Kernel bloat** — too many active emergent dims inflate every audit prompt. Cap: max 4 active emergent dims at any time. Highest fire_count wins ties; rest stay candidate.
- **False positives in discovery** — Haiku emits free_form_leaks for things that aren't really leaks. Mitigation: cluster threshold + Sonnet validation + Adrian gate.

**Depends on**: WP-4 (daily digest infra) + WP-7 (core dim cut shipped first — so emergent layer doesn't overlap with retired dims).

**Why this matters**: closed schema = system can only catch antipatterns Adrian + initial doctrine already named. Open schema with discovery = system catches *new* leak modes as agent behavior evolves, model versions change, or Adrian's work domain shifts. Decay prevents historical baggage. Same compounding logic as the blocklist, applied one level up the taxonomy.

---

## WP-10 — Statusline + `/beast rules` view (BLOCKED by WP-2 + WP-1)

**Goal**: visibility. Delta + correction-rate + rule view.

**Tasks**:
1. Statusline (`bin/statusline.sh`):
   - Show 7d Beast Index + delta vs 30d rolling.
   - Show Adrian-correction rate last 50 turns.
   - Show EVOL health badge (from WP-0).
2. `/beast rules` (CLI in `bin/beast_cli.py`):
   - Table: id, phrase, dim, hits, status, last_triggered, age.
   - Sort by hits desc.
   - Supports `/beast retire <id>` and `/beast promote <id>`.
3. `/beast confirm <turn_id>` / `/beast reject <turn_id>` — write Adrian-labeled correction to calibration ledger.

**Deliverables**: extended statusline, CLI script, skill update (`~/.claude/skills/beast/SKILL.md`).

**Acceptance**: `/beast rules` shows current blocklist. `/beast retire <id>` flips status. Statusline shows delta.

**Files touched**: `bin/statusline.sh`, new `bin/beast_cli.py`, `~/.claude/skills/beast/SKILL.md`.

**Risk**: low.

**Depends on**: WP-1 + WP-2.

---

## Dependency graph

```
WP-0 (pre-flight) ──┬─→ WP-7 (dim cut)        ──┬─→ WP-9 (per-task kernel)
                    │                           │
                    │                           └─→ WP-11 (emergent dims) ←─┐
                    └─→ WP-8 (action-gap gate)                              │
                                                                            │
WP-1 (calibration) ──┬─→ WP-5 (judge eval)    ──→ WP-6 (cross-LM)           │
                     │                                                      │
                     └─→ WP-10 (statusline + CLI)                           │
                              ▲                                             │
WP-2 (blocklist) ──┬─→ WP-3 (pre-turn inject) ─┘                            │
                   │                                                        │
                   └─→ WP-4 (daily digest) ─────────────────────────────────┘
```

---

## Suggested execution order (dependency-only, no human-effort framing)

**Wave A** (parallel, all READY):
- WP-0 (pre-flight repairs)
- WP-1 (calibration ledger + detector)
- WP-2 (blocklist scaffolding)

**Wave B** (after A):
- WP-3 (pre-turn injection) ← depends on WP-2
- WP-4 (daily digest) ← depends on WP-2
- WP-5 (judge eval) ← depends on WP-1
- WP-7 (dim cut) ← depends on WP-0
- WP-8 (action-gap gate) ← depends on WP-0

Wave B is internally parallel — all five can ship same session.

**Wave C** (after B):
- WP-6 (cross-LM) ← WP-5
- WP-9 (per-task kernel) ← WP-7
- WP-10 (statusline + CLI) ← WP-1 + WP-2
- WP-11 (emergent dims) ← WP-4 + WP-7

Wave C parallel.

---

## Definition of done (overall)

- Pre-turn injection live for ≥7 days.
- Blocklist has ≥10 active rules promoted from ledger.
- Calibration ledger has ≥5 Adrian-confirmed corrections.
- Judge eval set ≥20 cases, runs in CI on kernel change.
- Daily digest shipping for ≥14 consecutive days.
- Evolution loop produces monthly amendment without error 2 cycles in a row.
- Statusline shows correction-rate + EVOL health.
- Scope-leak rate drops below 25% (current 44%) in 30-day window.

The last metric is the actual success criterion. Everything else is plumbing.

---

## Out of scope (do not bundle into these packages)

- Web dashboard.
- Multi-machine sync.
- Replacing Constitution with DSL.
- Per-skill bundle adoption from skills2.
- Mid-response streaming intervention.
- Plugin system.

Defer to explicit later decision.
