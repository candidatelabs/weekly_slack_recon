"""
Candidate-facing email outreach: look up all pipeline opportunities for a
candidate across every client and compose a personalised check-in email.
"""
from __future__ import annotations

import base64
import json
import re
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

# OAuth scope required for sending email (separate from the readonly search scope)
_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


# ── Company name helpers ───────────────────────────────────────────────────────

def _format_company_name(submission: dict) -> str:
    """Return a clean display name for the company in a submission record."""
    # Ashby records carry explicit company / org fields
    if submission.get("company_name"):
        return submission["company_name"]
    if submission.get("orgName"):
        return submission["orgName"]
    # Slack records: derive from channel name (e.g. "candidatelabs-agave" → "Agave")
    channel = submission.get("channel_name", "")
    name = re.sub(r"^candidatelabs-", "", channel, flags=re.I)
    name = re.sub(r"-(engineers?|engineering|candidates?|labs?)$", "", name, flags=re.I)
    return name.replace("-", " ").title()


# ── Candidate lookup ───────────────────────────────────────────────────────────

def search_candidates(query: str, data_path: str) -> list[dict]:
    """
    Return unique candidate names that contain *query* (case-insensitive).

    Args:
        query:      Partial or full name to search for.
        data_path:  Path to weekly_slack_reconciliation.json.

    Returns:
        List of dicts: [{"name": str, "email": str | None}, ...]
        Sorted alphabetically, deduplicated.
    """
    if not query or len(query) < 2:
        return []

    p = Path(data_path)
    if not p.exists():
        return []

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    q = query.strip().lower()
    seen: dict[str, Optional[str]] = {}  # name → email

    for sub in data.get("submissions", []):
        name = sub.get("candidate_name", "").strip()
        if not name:
            continue
        if q not in name.lower():
            continue
        # Use the first non-null email we encounter for this name
        email = seen.get(name)
        if email is None:
            email = sub.get("email") or sub.get("primaryEmailAddress") or None
        seen[name] = email

    return [
        {"name": name, "email": email}
        for name, email in sorted(seen.items(), key=lambda x: x[0].lower())
    ]


def get_candidate_opportunities(candidate_name: str, data_path: str) -> list[dict]:
    """
    Find all pipeline entries for a candidate across every client / org.

    Args:
        candidate_name: Exact candidate name to look up (case-insensitive).
        data_path:      Path to weekly_slack_reconciliation.json.

    Returns:
        List of opportunity dicts sorted active-first then alphabetically.
        Each dict: {
            "company":   str,
            "status":    str,
            "stage":     str | None,
            "job_title": str | None,
            "is_active": bool,
            "source":    str,
        }
    """
    p = Path(data_path)
    if not p.exists():
        return []

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    name_lower = candidate_name.strip().lower()
    matching = [
        s for s in data.get("submissions", [])
        if s.get("candidate_name", "").strip().lower() == name_lower
    ]

    # Deduplicate by company; prefer Ashby records (more structured data)
    seen: dict[str, dict] = {}
    for sub in matching:
        company = _format_company_name(sub)
        existing = seen.get(company)
        if existing is None or sub.get("source") == "ashby":
            seen[company] = sub

    opportunities = []
    for company, sub in seen.items():
        status = sub.get("status", "")
        is_active = status != "CLOSED"
        stage = sub.get("currentStage") or sub.get("pipeline_stage") or None
        job_title = sub.get("job_title") or sub.get("jobTitle") or None
        opportunities.append({
            "company": company,
            "status": status,
            "stage": stage,
            "job_title": job_title,
            "is_active": is_active,
            "source": sub.get("source", "slack"),
        })

    # Active first, then closed; within each group sort alphabetically
    opportunities.sort(key=lambda o: (0 if o["is_active"] else 1, o["company"].lower()))
    return opportunities


# ── Message composer ───────────────────────────────────────────────────────────

def compose_candidate_message(first_name: str, opportunities: list[dict]) -> str:
    """
    Build the pre-populated candidate check-in email body.

    Status mapping:
        CLOSED             → "no longer moving forward"
        Active + stage     → stage name
        Active, no stage   → "in process"

    Args:
        first_name:     Candidate's first name.
        opportunities:  From get_candidate_opportunities().

    Returns:
        Full email body as a plain-text string.
    """
    bullets = []
    for opp in opportunities:
        company = opp["company"]
        if not opp["is_active"]:
            detail = "no longer moving forward"
        elif opp.get("stage"):
            detail = opp["stage"]
        else:
            detail = "in process"
        bullets.append(f"• {company} — {detail}")

    bullet_block = "\n".join(bullets) if bullets else "• (no opportunities found)"

    return (
        f"Hi {first_name},\n\n"
        "I just wanted to check in with you to see how your interviews are "
        "coming along. Here are the latest updates I have on each opportunity below:\n\n"
        f"{bullet_block}\n\n"
        "Let me know if you have any questions along the way!\n\n"
        "Best,\nDK"
    )


# ── Gmail email lookup ─────────────────────────────────────────────────────────

def lookup_candidate_email(
    candidate_name: str,
    credentials_path: str,
    token_path: str,
) -> Optional[str]:
    """
    Try to find a candidate's personal email address by searching Gmail for
    messages they sent to DK.

    Args:
        candidate_name:   Full name of the candidate.
        credentials_path: Path to credentials.json.
        token_path:       Path to gmail_token.json.

    Returns:
        Email address string if found, else None.
    """
    try:
        from googleapiclient.discovery import build
        from .google_auth_helper import get_credentials

        SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
        creds = get_credentials(credentials_path, token_path, SCOPES)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        parts = candidate_name.strip().split()
        first = parts[0] if parts else candidate_name
        last = parts[-1] if len(parts) > 1 else ""

        # Try most-specific query first, then fall back to first name only
        queries = []
        if first and last:
            queries.append(f'from:"{first} {last}"')
        queries.append(f'from:"{first}"')

        for query in queries:
            results = service.users().messages().list(
                userId="me", q=query, maxResults=5
            ).execute()
            for msg_ref in results.get("messages", []):
                msg = service.users().messages().get(
                    userId="me",
                    id=msg_ref["id"],
                    format="metadata",
                    metadataHeaders=["From"],
                ).execute()
                headers = {
                    h["name"]: h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                from_val = headers.get("From", "")
                # Extract address from "Name <email@domain>" format
                m = re.search(r"<([^>]+@[^>]+)>", from_val)
                if m:
                    return m.group(1).strip()
                if "@" in from_val:
                    return from_val.strip()

    except Exception as e:
        print(f"[OUTREACH] Gmail lookup failed for '{candidate_name}': {e}")

    return None


# ── Gmail send ─────────────────────────────────────────────────────────────────

def send_email_via_gmail(
    to: str,
    subject: str,
    body: str,
    credentials_path: str,
    token_path: str,
) -> dict:
    """
    Send an email via the Gmail API as the authenticated user (DK).

    Uses a dedicated token file (derived from token_path) with the gmail.send
    scope so the existing read-only token is not affected.  On first use, this
    will open a browser window for a one-time authorisation of the send scope.

    Args:
        to:               Recipient email address.
        subject:          Email subject line.
        body:             Plain-text email body.
        credentials_path: Path to credentials.json.
        token_path:       Path to the base gmail token file; the send token
                          is stored alongside it as 'gmail_send_token.json'.

    Returns:
        Dict with "ok": True and "message_id" on success, or raises on error.
    """
    from googleapiclient.discovery import build
    from .google_auth_helper import get_credentials

    # Store send token separately so it doesn't affect the read token
    send_token_path = str(Path(token_path).parent / "gmail_send_token.json")
    creds = get_credentials(credentials_path, send_token_path, [_GMAIL_SEND_SCOPE])
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    mime_msg = MIMEText(body, "plain")
    mime_msg["to"] = to
    mime_msg["subject"] = subject
    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()

    result = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()

    return {"ok": True, "message_id": result.get("id")}
