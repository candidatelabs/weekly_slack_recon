"""
FastAPI web UI for Weekly Slack Pipeline Reconciliation Tool
"""
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response, FileResponse
from datetime import datetime, timezone
import os
import sys
from pathlib import Path
import json
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from weekly_slack_recon.config import Config
from weekly_slack_recon.slack_client import SlackAPI
from weekly_slack_recon.logic import build_candidate_submissions
from weekly_slack_recon.reporting import group_by_channel, generate_csv_string
from weekly_slack_recon.status_rules import StatusCategory

app = FastAPI(title="Slack Pipeline Reconciliation")

# Hardcoded token from .env (will be reloaded on each request)
DEFAULT_SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()


@app.get("/", response_class=HTMLResponse)
async def index():
    """Main UI page."""
    html_path = Path(__file__).parent / "templates" / "index.html"
    with open(html_path, "r") as f:
        html_content = f.read()
        # Inject the default token
        html_content = html_content.replace("{{ default_token }}", DEFAULT_SLACK_TOKEN)
        return HTMLResponse(content=html_content)


@app.post("/api/load")
async def load_data(
    slack_token: str = Form(None),
    dk_email: str = Form("dkimball@candidatelabs.com"),
    lookback_days: int = Form(60),
    unclear_followup_days: int = Form(7),
    inactivity_days: int = Form(5),
    include_confused_close: bool = Form(False),
):
    """Load and process submissions from Slack."""
    # Use hardcoded token if not provided
    # Reload .env in case it was updated
    load_dotenv()
    # Get token from form, or fall back to .env, or use default
    form_token = (slack_token or "").strip() if slack_token else ""
    env_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    token = form_token or env_token or DEFAULT_SLACK_TOKEN.strip()
    
    if not token:
        return {"error": "Slack token is required. Please check your .env file or enter a token.", "submissions": [], "stats": {}}
    
    # Debug: log token prefix (first 10 chars) to help diagnose
    print(f"[DEBUG] Using token: {token[:10]}... (length: {len(token)})")
    
    try:
        cfg = Config(
            slack_bot_token=token,
            dk_email=dk_email,
            lookback_days=lookback_days,
            unclear_followup_days=unclear_followup_days,
            inactivity_days=inactivity_days,
            include_confused_close=include_confused_close,
        )

        slack = SlackAPI(token=cfg.slack_bot_token)
        
        # Test token validity first
        try:
            dk_user_id = slack.get_user_id_by_email(cfg.dk_email)
        except Exception as e:
            error_msg = str(e)
            if "invalid_auth" in error_msg.lower():
                return {
                    "error": f"Invalid or expired Slack token. Please check your token in .env file or get a new one from https://api.slack.com/apps. Error: {error_msg}",
                    "submissions": [],
                    "stats": {}
                }
            raise
        channels = slack.list_candidate_channels_for_user(dk_user_id)

        if not channels:
            return {"error": "No matching channels found", "submissions": [], "stats": {}}

        now = datetime.now(tz=timezone.utc)
        submissions, stats = build_candidate_submissions(cfg, slack, dk_user_id, channels, now=now)

        # Convert to JSON-serializable format
        submissions_data = []
        for s in submissions:
            submissions_data.append({
                "candidate_name": s.candidate_name,
                "linkedin_url": s.linkedin_url,
                "channel_name": s.channel_name,
                "status": s.status,
                "status_reason": s.status_reason or "",
                "submitted_at": s.submitted_at.isoformat(),
                "days_since_submission": s.days_since_submission,
                "needs_followup": s.needs_followup,
            })

        return {
            "submissions": submissions_data,
            "stats": stats,
            "error": None,
        }
    except Exception as e:
        return {"error": str(e), "submissions": [], "stats": {}}


@app.post("/api/export/csv")
async def export_csv(
    submissions_json: str = Form(...),
):
    """Export submissions as CSV."""
    submissions = json.loads(submissions_json)
    csv_data = generate_csv_string_from_dict(submissions)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="slack_reconciliation_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv"'},
    )


def generate_csv_string_from_dict(submissions: list) -> str:
    """Generate CSV from dict submissions."""
    import csv
    import io
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Channel",
        "Candidate Name",
        "LinkedIn URL",
        "Status",
        "Status Reason",
        "Submitted At",
        "Days Since Submission",
        "Needs Follow-up",
    ])
    for s in submissions:
        writer.writerow([
            s["channel_name"],
            s["candidate_name"],
            s["linkedin_url"],
            s["status"],
            s["status_reason"],
            s["submitted_at"],
            s["days_since_submission"],
            "Yes" if s["needs_followup"] else "No",
        ])
    return output.getvalue()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)

