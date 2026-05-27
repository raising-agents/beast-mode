# Beast Mode

Coding-agent runtime that grounds Claude Code as a machine (not a human contractor), automatically audits every response, and self-improves via a weekly evolution loop.

## Architecture

```
Layer                          Mechanism                               When it runs
─────────────────────────────────────────────────────────────────────────────────────
1. Constitution                ~/.claude/instructions/                 Always in context
                                 beast-mode-constitution.md            (pointer in ~/.claude/CLAUDE.md)
2. Self-audit (on-demand)      MCP machine-grounding                   When agent calls
                                 check_framing(draft_text)             check_framing
                                 beast_index()
3. Post-response audit         Stop hook → beast-mode-stop.py          Every assistant turn
                                 → spawns auditor-worker.py            (async, never blocks)
                                 → Haiku scores 6 dims
                                 → appends to drift ledger
4. Drift ledger                ~/.claude/beast-mode/ledger/            Append-only JSONL
                                 drift.jsonl                           one entry per turn
5. Statusline                  ~/.claude/beast-mode/bin/statusline.sh  Every UI tick
                                 reads ledger, shows rolling Beast     (<100ms)
                                 Index + top leak dims
6. Evolution loop              ~/.claude/beast-mode/bin/evolution.py   Weekly
                                 Opus reads ledger, proposes           (launchd plist
                                 Constitution amendments               in bin/, not loaded
                                                                       by default)
7. Manual orientation          /beast skill                            On user invocation
                                 (~/.claude/skills/beast/SKILL.md)     ("/beast" or trigger)
```

## Files

| Path | Purpose |
|------|---------|
| `~/.claude/instructions/beast-mode-constitution.md` | Operating doctrine, six Beast Index dimensions |
| `~/.claude/hooks/beast-mode-stop.py` | Stop hook — spawns auditor worker, never blocks |
| `~/.claude/beast-mode/bin/auditor-worker.py` | Reads transcript, calls Haiku, writes ledger |
| `~/.claude/beast-mode/bin/evolution.py` | Weekly Opus amendment proposer |
| `~/.claude/beast-mode/bin/statusline.sh` | Beast Index display |
| `~/.claude/beast-mode/bin/com.adrian.beast-mode-evolution.plist` | launchd plist (not loaded by default) |
| `~/.claude/beast-mode/ledger/drift.jsonl` | Audit history |
| `~/.claude/beast-mode/proposals/` | Weekly amendment proposals |
| `~/machine-grounding-mcp/server.py` | MCP server (`check_framing`, `beast_index`, `lookup`, `ground_decision`, `ground_scope`, `reframe`) |
| `~/.claude/skills/beast/SKILL.md` | `/beast` manual orientation skill |

## Backend

All LLM calls use `claude -p --model haiku` (auditor) or `--model opus` (evolution). Uses keychain OAuth — no API key needed. No external network beyond Anthropic.

## Cost (estimated)

- Auditor: ~$0.0005/turn × 50 turns/day = ~$0.75/month
- Evolution: ~$0.05 × 4/month = ~$0.20/month
- Total: ~$1/month for self-improving beast-mode runtime

## Enable weekly evolution

Not loaded by default. To enable:

```bash
cp ~/.claude/beast-mode/bin/com.adrian.beast-mode-evolution.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$UID" ~/Library/LaunchAgents/com.adrian.beast-mode-evolution.plist
# Verify
launchctl list | grep beast-mode-evolution
```

To disable:

```bash
launchctl bootout "gui/$UID" ~/Library/LaunchAgents/com.adrian.beast-mode-evolution.plist
rm ~/Library/LaunchAgents/com.adrian.beast-mode-evolution.plist
```

Default schedule: Sunday 08:57 local.

## Disable beast mode

Disable Stop-hook auditor — comment out the beast-mode entry in `~/.claude/settings.json` → `hooks.Stop`. Statusline keeps reading the ledger but the ledger stops growing.

To uninstall completely: remove the Stop hook entry, remove the statusLine block, run `claude mcp remove machine-grounding`, delete `~/.claude/beast-mode/`.

## Manual checks

```bash
# Inspect recent drift
tail -5 ~/.claude/beast-mode/ledger/drift.jsonl | jq .

# Run auditor on a transcript manually
python ~/.claude/beast-mode/bin/auditor-worker.py /path/to/transcript.jsonl session-id

# Force a weekly evolution run
python ~/.claude/beast-mode/bin/evolution.py
ls ~/.claude/beast-mode/proposals/

# MCP health
claude mcp list | grep machine-grounding
```
