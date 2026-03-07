# Quick Start

## Launch the dashboard

Double-click **`Slack Reconciliation.app`** on the Desktop.

This opens the Pipeline Reconciliation Dashboard at `http://localhost:8001/dashboard.html`. The last synced data loads immediately — the header shows how fresh it is (`Slack synced 2h ago · Ashby imported 45m ago`).

---

## Daily usage

You don't need to sync every time you open the dashboard. Check the freshness timestamps in the header and sync only when the data feels stale.

1. **Sync Slack** — click **Sync Slack** when you want fresh data (~1–2 min). This also automatically runs a fresh Ashby extraction at the end.
2. **If a yellow banner appears** — your Ashby session cookie expired. Follow the on-screen steps to paste a fresh cookie. The server re-authenticates and re-syncs automatically.
3. **Review candidates** — filter by status, company/channel, or source. Click stage links to open a candidate directly in Ashby.

---

## First-time setup

### 1. Install dependencies

```bash
cd /Users/david/Desktop/weekly_slack_recon
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Also requires **Node.js** and the **Ashby Automation** tool at `~/Desktop/Ashby automation/` (separate project).

### 2. Create `.env`

```env
SLACK_BOT_TOKEN=xoxp-...            # User OAuth Token from api.slack.com/apps
DK_EMAIL=dkimball@candidatelabs.com
LOOKBACK_DAYS=60
UNCLEAR_FOLLOWUP_DAYS=7
INACTIVITY_DAYS=5
ANTHROPIC_API_KEY=sk-ant-api03-...  # From console.anthropic.com
ASHBY_JSON_PATH=/Users/david/Desktop/Ashby automation/output
```

### 3. Run

```bash
python serve_dashboard.py
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Yellow "Ashby session expired" banner | Open DevTools on `app.ashbyhq.com` → Application → Cookies → copy `sessionToken` value → paste into banner |
| Slack sync times out | Normal — Slack rate limits are slow. Wait for it to finish. |
| "Import Ashby" shows stale file age | Click **Sync Slack** instead — it fetches a fresh Ashby export automatically |
| App won't open | Run `python serve_dashboard.py` from the terminal to see error output |

See **README.md** for full documentation.
