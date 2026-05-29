# Beast Mode v2 — Dim Taxonomy Cutover Notes (2026-05-30)

## What changed

The auditor kernel went from **10 binary dimensions** (v1) to **6** (v2). Constitution §IV + §V rewritten. Old ledger rows preserved as-is.

| Dim | v1 method | v2 method | v2 status |
|---|---|---|---|
| parallelism | structural | structural | kept |
| action_over_announcement | LM | LM (structural evidence) | kept |
| scope | LM | LM | kept |
| depth | LM | LM | kept |
| verification_by_evidence | LM | **structural** (NEW) | kept + upgraded |
| boldness | LM | LM | kept (reframed as reversibility-calibration) |
| sequencing | LM | — | **retired** (45% N/A; overlaps scope) |
| deferrals | LM | — | **retired** (45% N/A; absorbs into scope) |
| block_breaking | LM | — | **retired** (rarely fires) |
| self_direction_over_ask | LM | — | **retired** (rarely fires) |

## What stays the same

- Doctrine antipatterns in Constitution §III: all categories preserved as prose reference. Retired dims have no dedicated scoring slot but their phrase patterns still surface via the blocklist (WP-2) and will surface via the emergent layer (WP-11) when that ships.
- Historical drift.jsonl rows: unmodified. ~20K rows under v1 schema stay queryable.
- All aggregation tools: they iterate `(dims or {}).items()` and tolerate mixed shapes.

## What's new

**Structural `verification_by_evidence` detector** in `bin/auditor-worker.py`:

- `CLAIM_PHRASES` regex matches narrow set: `done | works | found | fixed | complete | approved | passing | verified | all tests pass`.
- Scans last 800 chars of assistant text (tail only — avoids matching phrases inside earlier quoted context).
- If matched: looks at `/tmp/beast-struct-{session}.json` for a tool call timestamped within 30s of turn end.
  - Tool result present → score 1
  - No tool result → score 0
- If no claim phrase → N/A (score `None`).
- Method = `structural` in receipts. Overrides any LM judgement.

Same pattern as `parallelism` ground-truth scoring. Zero Haiku tokens spent on this dim now.

## Ledger row shape (v2)

New rows look like:

```json
{
  "ts": "2026-05-30T14:32:11Z",
  "session": "...",
  "score": "4/6",
  "dims": {
    "parallelism": 1,
    "action_over_announcement": 1,
    "verification_by_evidence": 0,
    "scope": 0,
    "depth": 1,
    "boldness": 1
  },
  "leaks": [...],
  "notes": "...",
  "user_prompt_excerpt": "...",
  "schema_version": 2
}
```

Old rows (pre-cutover) keep their 10-key `dims` dict and no `schema_version` field.

## Trend query across cutover

Quick sanity check that mixed shapes coexist:

```bash
LEDGER=~/.claude/beast-mode/ledger/drift.jsonl
jq -r '.dims | keys | length' $LEDGER | sort | uniq -c
```

Expect to see counts at `6` (post-cutover) and `10` (pre-cutover). Both valid.

```bash
# Schema_version distribution (v2 rows only)
jq -r '.schema_version // "v1"' $LEDGER | sort | uniq -c
```

## Tools verified tolerant of mixed shapes

- `bin/statusline.sh` — iterates `dims.items()`. Last 30 rows show mixed counts cleanly.
- `bin/daily_digest.py` — `aggregate()` uses dict iteration. Top-3 dims computed off whatever's present.
- `hooks/beast-pre-turn.py` — `rank_dims()` is dim-set-agnostic.
- `bin/evolution.py` — `build_digest()` iterates `Counter` over leaks.

## Rollback plan

If v2 needs to revert:

1. `cp ~/.claude/instructions/beast-mode-constitution.v1.md ~/.claude/instructions/beast-mode-constitution.md`
2. `cp ~/beast-mode/constitution/beast-mode-constitution.v1.md ~/beast-mode/constitution/beast-mode-constitution.md`
3. `git revert <wp-7-commit-sha>` in `~/beast-mode/`.

Retired dims keep scoring N/A on existing rows (no migration needed). New rows after rollback regain the 10-dim shape.

## Verification at cutover

- `bin/test_auditor_kernel.py` — 55/55 tests pass.
- Constitution §IV grep clean for orphan retired dim refs (only the explanatory "Retired dims" paragraph mentions them by name, by design).
- statusline + daily digest smoke-tested against current mixed-shape ledger.

## Open follow-ups

- **Structural input for `boldness`** (reversibility-calibration): counter of irreversible tool calls per turn. Deferred — natural follow-up when WP-9 per-task-type kernel lands.
- **WP-11 emergent dim discovery** — adds an emergent layer above the 6-dim core. Data-blocked; needs free-form leaks from auditor to accumulate first.
- **Pre/post cutover trend visualization** — WP-10 statusline polish can plot Beast Index across the v1→v2 break with a marker.
