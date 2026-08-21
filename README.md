# skill-gardener

Automated health audits for your [Hermes Agent](https://hermes-agent.nousresearch.com/docs) skills library — deduplication, staleness detection, and mining new skill candidates from past sessions — plus a defense-in-depth delivery pipeline so findings actually reach you.

> **只读分析，绝不自动改任何 skill / memory。** Read-only analysis; nothing is modified automatically.

## What's inside

| Component | Role |
|---|---|
| `skill/scripts/gardener.py` | Scans `state.db` + `skills/` + `memories/`, emits a health report (§0–§8) plus a **fail-closed schema self-check** — missing/renamed columns are flagged explicitly instead of silently producing empty sections. Configurable via `--home` `--stale-days` `--top` `--sediment-kw`. |
| `skill/scripts/watchdog.py` | Daily `no_agent` cron script: checks (a) the relay still serves the pinned model, (b) the weekly job's `last_status`, (c) staleness. Silent when healthy, prints an alert when not. Target job name configurable via `SKILL_GARDENER_JOB_NAME`. |
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

> **The model check assumes an OpenAI-compatible relay.** The "model famine" check calls `GET /v1/models` and expects the model your cron job relies on to be in that list. If you use a **first-party model provider** (OpenAI, Anthropic, Gemini, xAI, …) or a **custom/self-hosted gateway** that doesn't serve a standard `/v1/models`, this check will false-alarm ("unreachable" / "model missing"). Set `SKILL_GARDENER_SKIP_MODEL_CHECK=1` to disable just that check — the job-health and staleness checks still run regardless.

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
- **Fail-closed schema check**: `gardener.py` verifies the `state.db` columns it needs before querying. If a future Hermes migration renames a column, the report flags it up top instead of emitting empty sections — a watchdog that never fakes "all good".
- **Configurable, not hardcoded**: sediment keywords via `--sediment-kw "remember,next time"` (default list is Chinese — English-language sessions should override it); watchdog target job via `SKILL_GARDENER_JOB_NAME` (default `skill-gardener-weekly`).
- **Tests**: `python skill/scripts/test_gardener.py` (stdlib `unittest`, no deps). `schema_fixture.json` snapshots the verified real `state.db` column names; a contract test pins `REQUIRED_COLS` to it, and a live check runs against your actual `state.db` when present.
- **Watchdog assumptions**: `watchdog.py` expects an OpenAI-compatible relay exposing `GET /v1/models`, and a `config.yaml` whose `model:` block uses 2-space indentation (`default:` / `provider:` / `base_url:` / `key_env:`). A different layout will surface as "can't parse model/base_url" — that's the parser's assumption, not a relay outage.
