# CLAUDE.md — Quick Reference

Pipeline reconciliation dashboard for tracking candidates across Slack, Ashby, Gmail, and Google Calendar. Built for David Kimball at Candidate Labs.

## Entry points

| File | Role |
|------|------|
| `serve_dashboard.py` | HTTP server (port 8001) + all API endpoints |
| `dashboard.html` | Single-page dashboard UI (Pipeline + Check-Ins tabs) |
| `src/weekly_slack_recon/cli.py` | CLI entry point |
| `Slack Reconciliation.app` | macOS desktop app — launches `serve_dashboard.py` via Terminal |

## Source layout (`src/weekly_slack_recon/`)

| Module | Purpose |
|--------|---------|
| `config.py` | Env-based config dataclass, `load_config()` reads `.env` |
| `slack_client.py` | Slack API wrapper (channels, messages, threads, reactions) |
| `logic.py` | `build_candidate_submissions()` — LinkedIn extraction, status inference |
| `status_rules.py` | Emoji/keyword classification rules for CLOSED / IN PROCESS |
| `reporting.py` | JSON + Markdown output writers (`write_json`, `write_markdown`) |
| `ashby_importer.py` | Ashby JSON export → unified schema, DK-only filter |
| `calendar_client.py` | Google Calendar API — searches `"{first name} x {client name}"` events |
| `gmail_client.py` | Gmail API — search emails by candidate + inferred client domain |
| `google_auth_helper.py` | Shared Google OAuth2 flow (browser auth on first use, then cached) |
| `context_gatherer.py` | Gathers Slack/Gmail/Calendar context for LLM reasoning |
| `enrichment.py` | Claude-powered candidate summaries (AI enrichment) |
| `status_synthesizer.py` | Claude-powered per-candidate status reasoning |
| `message_composer.py` | Claude-powered check-in message drafting |
| `status_check_runner.py` | Check-Ins tab orchestrator |
| `candidate_outreach.py` | Candidate email outreach: lookup, compose, Gmail send |
| `nudge.py` | Auto-nudge for stale submissions |

## Data flow (Sync Slack)

1. Scan `candidatelabs-*` channels for DK's LinkedIn-containing messages
2. Infer status per submission (emoji reactions + thread keywords)
3. Write to `weekly_slack_reconciliation.json`
4. Enrich with Google Calendar events (`_enrich_with_calendar_events` in `serve_dashboard.py`)
5. Import Ashby candidates and merge into JSON
6. Dashboard reads JSON on load

## Key patterns

- **Channel names**: `candidatelabs-{client-name}` (e.g. `candidatelabs-sequence-holdings` → "Sequence Holdings")
- **Calendar matching**: `CalendarClient.search_events(first_name, client_name)` — matches events titled `"{first name} x {client name}"`
- **Status inference**: Emoji reactions (✅ = in process, ⛔ = closed) + thread keywords (see `status_rules.py`)
- **Lookback window**: `LOOKBACK_DAYS` env var, default 60 days

## Config

All settings via `.env`, loaded through `config.py:load_config()`. Key vars:

```
SLACK_BOT_TOKEN    # xoxp-... User OAuth Token
ANTHROPIC_API_KEY  # For AI enrichment + Check-Ins
ASHBY_JSON_PATH    # Path to Ashby export directory
LOOKBACK_DAYS      # Default 60
GCAL_LOOKBACK_DAYS / GCAL_LOOKAHEAD_DAYS  # Calendar search window
```

## Never commit

`.env`, `credentials.json`, `*_token.json`, `.ashby-session.json`

## Run locally

```bash
source .venv/bin/activate
python serve_dashboard.py
# Opens http://localhost:8001/dashboard.html
```
