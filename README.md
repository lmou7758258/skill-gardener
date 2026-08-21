# skill-gardener

Automated health audits for your [Hermes Agent](https://hermes-agent.nousresearch.com/docs) skills library — deduplication, staleness detection, and mining new skill candidates from past sessions — plus a defense-in-depth delivery pipeline so findings actually reach you.

> **只读分析，绝不自动改任何 skill / memory。** Read-only analysis; nothing is modified automatically.

## What's inside

| Component | Role |
|---|---|
| `skill/scripts/gardener.py` | Scans `state.db` + `skills/` + `memories/`, emits a health report (§0 trends, §1 hot skills, §2 stale, §3 suspected duplicates, §4 never-loaded, §5 edit history, §6 deposit candidates, §6b hard-task sessions, §7 memory overview, §8 session overview). |
| `skill/scripts/watchdog.py` | Daily `no_agent` cron script: checks (a) the relay still serves the pinned model, (b) the weekly job's `last_status`, (c) staleness. Silent when healthy, prints an alert when not. |
| `skill/scripts/cron-backstop.ps1` | Windows Scheduled Task shim that fires due cron jobs even when the desktop app is closed (the desktop backend's cron ticker dies with the app). |
| `plugin/` (`skill-gardener-reminder`) | A `pre_llm_call` plugin that injects a reminder into your next turn when there's an unhandled `PENDING.md`. |

## Architecture

```
09:50  Windows Scheduled Task (SYSTEM) → hermes cron tick   ← backstop for closed app
09:55  watchdog cron (no_agent)                             ← model-famine + job-health check
10:00  weekly audit cron (gardener.py)                      ← the actual scan
           │  actionable findings
           ▼
       PENDING.md + inbox/ drafts
           │
           ▼  (next session, pre_llm_call)
       skill-gardener-reminder plugin injects a reminder
           │
           ▼  (you approve)
       skill_manage → committed
```

## Install

### 1. Skill
Copy `skill/` to `$HERMES_HOME/skills/productivity/skill-gardener/`.

### 2. Plugin
Copy `plugin/` to `$HERMES_HOME/plugins/skill-gardener-reminder/`, then:

```
hermes plugins enable skill-gardener-reminder
```

### 3. Weekly cron (the audit)
Create a cron job at `0 10 * * 6` that loads the `skill-gardener` skill. Pin a `model`/`provider` your relay actually serves (check `/v1/models` first — provider model lists go stale).

### 4. Watchdog cron (daily)
A `no_agent` cron job at `55 9 * * *` running `skill/scripts/watchdog.py`. It emits empty output when healthy, an alert line when not.

### 5. Windows backstop (desktop only, optional)
```
schtasks /create /tn "HermesCronBackstop" \
  /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"C:\path\to\cron-backstop.ps1\"" \
  /sc daily /st 09:50 /ru SYSTEM /rl HIGHEST /f
```

## Hard rules

1. Scripts are read-only; any create/patch/delete must be user-confirmed first.
2. Drafts go to `$HERMES_HOME/.skill-gardener/inbox/`; committed only after approval.
3. New skills record `source_session` in frontmatter.
4. Stable preferences → memory; reusable workflows → skill.

## Notes

- Pure standard library (Python 3.11+); no third-party deps.
- The `.ps1` shim deliberately uses ASCII comments so Windows PowerShell 5.1 (ANSI codepage) parses it correctly without BOM/encoding pitfalls.
- `gardener.py` and `watchdog.py` locate `HERMES_HOME` via env var → `~/.hermes` → `%LOCALAPPDATA%\hermes` → `%APPDATA%\hermes`, so they work on Windows, macOS, and Linux.
