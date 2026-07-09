"""
Import and normalize candidates from the Ashby Pipeline API (Railway backend)
or a cached JSON export into the unified submission format used by the Weekly
Slack Recon dashboard.
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


ASHBY_API_URL = "https://ashby-automation-production.up.railway.app/api/extract"

# Cookie storage file (project-local)
ASHBY_COOKIE_PATH = Path(__file__).resolve().parents[2].parent / ".ashby-session.json"


def save_ashby_cookie(cookie: str) -> None:
    """Persist the Ashby session cookie to disk."""
    ASHBY_COOKIE_PATH.write_text(
        json.dumps({"cookie": cookie, "saved_at": datetime.now(tz=timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def load_ashby_cookie() -> Optional[str]:
    """Load a previously saved Ashby session cookie, or None."""
    if not ASHBY_COOKIE_PATH.exists():
        return None
    try:
        data = json.loads(ASHBY_COOKIE_PATH.read_text(encoding="utf-8"))
        return data.get("cookie") or None
    except Exception:
        return None


def extract_from_api(cookie: str, *, timeout: int = 300) -> List[Dict[str, Any]]:
    """
    Call the Ashby Pipeline Railway API to extract candidates.

    Args:
        cookie: The ashby_session_token cookie value (bare token or full header).
        timeout: Request timeout in seconds (extraction can be slow).

    Returns:
        List of raw candidate dicts as returned by the API.

    Raises:
        RuntimeError on auth failure (401) or other API errors.
    """
    payload = json.dumps({"cookie": cookie}).encode("utf-8")
    req = urllib.request.Request(
        ASHBY_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("Ashby session expired — paste a fresh cookie.") from e
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Ashby API returned {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Ashby API: {e.reason}") from e

    candidates = data.get("candidates") if isinstance(data, dict) else data
    if isinstance(candidates, list):
        return candidates
    if isinstance(data, list):
        return data
    return []


def extract_and_save(cookie: str, output_dir: str, *, timeout: int = 300) -> str:
    """
    Extract candidates from the Railway API and save to a dated JSON file.

    Returns the path to the saved JSON file.
    Raises RuntimeError on failure.
    """
    raw_candidates = extract_from_api(cookie, timeout=timeout)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = out_dir / f"ashby_pipeline_{today}.json"

    # Save in the new API format (flat candidates array)
    out_path.write_text(
        json.dumps({"candidates": raw_candidates, "format": "api"}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[ASHBY] Saved {len(raw_candidates)} candidates to {out_path}")
    return str(out_path)


def find_latest_ashby_export(path: str) -> str:
    """
    Given a path that is either a JSON file or a directory, return the path to
    the JSON file to use. If a directory is given, returns the most recently
    modified .json file in that directory.

    Raises FileNotFoundError if nothing suitable is found.
    """
    p = Path(path)
    if p.is_file():
        return str(p)
    if p.is_dir():
        json_files = sorted(p.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not json_files:
            raise FileNotFoundError(f"No JSON files found in directory: {path}")
        return str(json_files[0])
    raise FileNotFoundError(f"Path does not exist: {path}")


_DK_NAMES: set = {
    "david", "dk", "david kimball", "david cl",
    "dkimball", "dkimball@candidatelabs.com",
}


def _is_dk_credited(candidate: Dict[str, Any]) -> bool:
    """Return True if this candidate is credited to David Kimball / DK."""
    # Handle both old camelCase format and new snake_case API format
    credited = (
        candidate.get("credited_to")
        or candidate.get("creditedTo")
        or ""
    ).strip().lower()
    return credited in _DK_NAMES


def load_ashby_export(json_path: str) -> List[Dict[str, Any]]:
    """
    Load an Ashby JSON export (either new API format or legacy format) and
    return a list of normalized submission dicts compatible with
    weekly_slack_reconciliation.json.

    Only candidates credited to David Kimball / DK are included.
    """
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"Ashby JSON export not found: {json_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Detect format: new API format has "format": "api" or snake_case fields
    is_api_format = (
        data.get("format") == "api"
        or (data.get("candidates") and not data.get("jobs"))
    )

    if is_api_format:
        return _normalize_api_candidates(data.get("candidates", []))
    else:
        return _normalize_legacy_candidates(data)


def _normalize_api_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize candidates from the Railway API (snake_case format)."""
    now = datetime.now(tz=timezone.utc)
    normalized: List[Dict[str, Any]] = []

    for c in candidates:
        if not _is_dk_credited(c):
            continue

        # Parse last activity timestamp
        last_activity_raw = c.get("last_activity_at", "")
        try:
            last_activity_dt = datetime.fromisoformat(
                last_activity_raw.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            last_activity_dt = now

        days_since = max(0, (now - last_activity_dt).days)

        normalized.append({
            # Source marker
            "source": "ashby",

            # ── Common fields ──────────────────────────────────────────────
            "candidate_name": c.get("candidate_name") or "Unknown",
            "linkedin_url": None,
            "email": None,
            "submitted_at": last_activity_dt.isoformat(),
            "days_since_submission": days_since,
            "status": _map_ashby_status_api(c),
            "status_reason": c.get("pipeline_stage") or c.get("decision_status") or None,
            "needs_followup": bool(c.get("needs_scheduling", False)),
            "ai_summary": None,
            "ai_enriched_at": None,

            # ── Slack-specific (always null for Ashby candidates) ──────────
            "channel_name": None,
            "channel_id": None,
            "slack_url": None,

            # ── Ashby-specific fields ──────────────────────────────────────
            "company_name": c.get("company_name") or None,
            "job_title": c.get("job_title") or None,
            "pipeline_stage": c.get("pipeline_stage") or None,
            "stage_progress": c.get("stage_progress") or None,
            "days_in_stage": c.get("days_in_stage"),
            "needs_scheduling": c.get("needs_scheduling"),
            "latest_recommendation": c.get("latest_recommendation") or None,
            "latest_feedback_author": c.get("latest_feedback_author") or None,
            "ashby_application_id": None,
            "ashby_candidate_id": c.get("candidate_id") or None,
            "credited_to": c.get("credited_to") or None,

            # ── Rich interview data (for LLM synthesis) ───────────────────
            "decision_status": c.get("decision_status") or None,
            "latest_feedback_date": c.get("latest_feedback_date") or None,
            "current_stage_date": c.get("current_stage_date") or None,
            "current_stage_interviews": c.get("current_stage_interviews") or None,
            "current_stage_avg_score": c.get("current_stage_avg_score") or None,
            "interview_history_summary": c.get("interview_history_summary") or None,
            "interview_events": c.get("interview_events") or [],
        })

    return normalized


def _normalize_legacy_candidates(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize candidates from the legacy Node.js export (camelCase format)."""
    jobs: Dict[str, Dict] = {j["id"]: j for j in data.get("jobs", [])}
    candidates = data.get("candidates", [])

    now = datetime.now(tz=timezone.utc)
    normalized: List[Dict[str, Any]] = []

    for candidate in candidates:
        if not _is_dk_credited(candidate):
            continue

        job = jobs.get(candidate.get("jobId", ""), {})

        last_activity_raw = candidate.get("lastActivityAt", "")
        try:
            last_activity_dt = datetime.fromisoformat(
                last_activity_raw.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            last_activity_dt = now

        days_since = max(0, (now - last_activity_dt).days)

        linkedin_url = (
            candidate.get("linkedInUrl")
            or candidate.get("linkedinUrl")
            or None
        )

        company_name = candidate.get("orgName") or None

        normalized.append({
            "source": "ashby",
            "candidate_name": candidate.get("name") or "Unknown",
            "linkedin_url": linkedin_url,
            "email": candidate.get("primaryEmailAddress") or candidate.get("email"),
            "submitted_at": last_activity_dt.isoformat(),
            "days_since_submission": days_since,
            "status": _map_ashby_status_legacy(candidate),
            "status_reason": (
                candidate.get("pipelineStage")
                or candidate.get("currentStage")
                or None
            ),
            "needs_followup": bool(candidate.get("needsScheduling", False)),
            "ai_summary": None,
            "ai_enriched_at": None,
            "channel_name": None,
            "channel_id": None,
            "slack_url": None,
            "company_name": company_name,
            "job_title": job.get("title") or None,
            "pipeline_stage": candidate.get("pipelineStage") or None,
            "stage_progress": candidate.get("stageProgress") or None,
            "days_in_stage": candidate.get("daysInStage"),
            "needs_scheduling": candidate.get("needsScheduling"),
            "latest_recommendation": candidate.get("latestOverallRecommendation") or None,
            "latest_feedback_author": candidate.get("latestFeedbackAuthor") or None,
            "ashby_application_id": candidate.get("applicationId") or None,
            "ashby_candidate_id": candidate.get("id") or None,
            "credited_to": candidate.get("creditedTo") or None,
            "decision_status": candidate.get("decisionStatus") or None,
            "latest_feedback_date": candidate.get("latestFeedbackDate") or None,
            "current_stage_date": candidate.get("currentStageDate") or None,
            "current_stage_interviews": candidate.get("currentStageInterviews") or None,
            "current_stage_avg_score": candidate.get("currentStageAvgScore") or None,
            "interview_history_summary": candidate.get("interviewHistorySummary") or None,
            "interview_events": candidate.get("interviewEvents") or [],
        })

    return normalized


def _normalize_url(url: Optional[str]) -> str:
    if not url:
        return ""
    return url.strip().rstrip("/").lower()


def _normalize_name_key(name: Optional[str]) -> str:
    """
    Reduce a candidate name to a "first last" matching key so that middle
    names don't block a match ("Manoj Kumar Panguluru" == "Manoj Panguluru").
    """
    if not name:
        return ""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    tokens = cleaned.split()
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return f"{tokens[0]} {tokens[-1]}"


def _channel_company_key(channel_name: Optional[str]) -> str:
    """
    Reduce a Slack channel name to lowercase client-name tokens:
    "candidatelabs-langchain-eng" -> "langchain eng".

    Mirrors _channel_to_client_name in status_check_runner.py (which imports
    this module, so it can't be reused here without a circular import).
    """
    if not channel_name:
        return ""
    name = channel_name.lstrip("#").lower()
    name = re.sub(r"^candidatelabs-?", "", name)
    name = re.sub(r"-(fwd|forward|submissions)$", "", name)
    return " ".join(name.replace("-", " ").split())


def _company_key(company_name: Optional[str]) -> str:
    """Normalize an Ashby company name to lowercase tokens for matching."""
    if not company_name:
        return ""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", company_name.lower().replace("-", " "))
    return " ".join(cleaned.split())


# Minimum length before a compact (space-stripped) prefix match is allowed,
# so "Lang" can't swallow "langchain eng". Mirrors the frontend's MIN_FUZZY_LEN.
_MIN_COMPACT_LEN = 5


def _company_matches(channel_name: Optional[str], company_name: Optional[str]) -> bool:
    """
    True when the Slack channel and the Ashby company refer to the same client.
    Token-prefix match in either direction: company "Langchain" matches channel
    tokens ["langchain", "eng"], but "Lang" does not (whole tokens only).
    Falls back to comparing space-stripped forms so spacing variants match
    ("Preference Model" vs channel "candidatelabs-preferencemodel").
    """
    chan_tokens = _channel_company_key(channel_name).split()
    comp_tokens = _company_key(company_name).split()
    if not chan_tokens or not comp_tokens:
        return False
    if (
        chan_tokens[: len(comp_tokens)] == comp_tokens
        or comp_tokens[: len(chan_tokens)] == chan_tokens
    ):
        return True
    chan_compact = "".join(chan_tokens)
    comp_compact = "".join(comp_tokens)
    if chan_compact == comp_compact:
        return True
    if min(len(chan_compact), len(comp_compact)) < _MIN_COMPACT_LEN:
        return False
    return chan_compact.startswith(comp_compact) or comp_compact.startswith(chan_compact)


# Ashby fields grafted onto a Slack record when the two sources merge.
ASHBY_MERGE_FIELDS = (
    "company_name", "job_title", "pipeline_stage", "stage_progress",
    "days_in_stage", "needs_scheduling", "latest_recommendation",
    "latest_feedback_author", "ashby_application_id", "ashby_candidate_id",
    "credited_to", "decision_status", "latest_feedback_date",
    "current_stage_date", "current_stage_interviews",
    "current_stage_avg_score", "interview_history_summary", "interview_events",
)


def _apply_ashby_to_slack(slack_rec: Dict[str, Any], ashby_rec: Dict[str, Any]) -> None:
    """
    Graft Ashby pipeline data onto a Slack submission (the Slack record stays
    the base row: name, LinkedIn, channel, thread link, submitted_at all kept).
    The original Slack status is preserved in `slack_status`; the visible
    `status` becomes the Ashby one (pipeline truth wins, same vocabulary).
    """
    slack_rec["slack_status"] = slack_rec.get("status")
    slack_rec["status"] = ashby_rec.get("status")
    slack_rec["email"] = slack_rec.get("email") or ashby_rec.get("email")
    for field in ASHBY_MERGE_FIELDS:
        slack_rec[field] = ashby_rec.get(field)
    slack_rec["also_in_ashby"] = True


def merge_ashby_into_submissions(
    existing: List[Dict[str, Any]],
    ashby_candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Merge Ashby candidates into an existing submissions list.

    - Removes any previously imported Ashby candidates (clean re-import)
    - Matches Ashby candidates to Slack submissions by LinkedIn URL, or by
      normalized name + company (channel name vs Ashby company)
    - Matched pairs collapse into a single row: the Slack record grafted with
      the Ashby pipeline fields (see _apply_ashby_to_slack)
    - Unmatched Ashby candidates are appended as their own rows

    Idempotent: merged rows keep their Slack `source`, so on re-entry they are
    reset (Ashby fields stripped, Slack status restored) and re-merged fresh.
    """
    # Keep only Slack candidates (drop any stale Ashby imports)
    slack_candidates = [
        s for s in existing if s.get("source", "slack") != "ashby"
    ]

    # Reset pass: strip any previously merged Ashby data so re-runs refresh
    # instead of stacking, and candidates archived out of Ashby revert cleanly.
    for s in slack_candidates:
        if "slack_status" in s:
            s["status"] = s.pop("slack_status")
        for field in ASHBY_MERGE_FIELDS:
            s.pop(field, None)
        s["also_in_ashby"] = False

    # Index Slack rows for matching
    by_linkedin: Dict[str, Dict[str, Any]] = {}
    by_name_key: Dict[str, List[Dict[str, Any]]] = {}
    for s in slack_candidates:
        url = _normalize_url(s.get("linkedin_url"))
        if url:
            by_linkedin[url] = s
        name_key = _normalize_name_key(s.get("candidate_name"))
        if name_key:
            by_name_key.setdefault(name_key, []).append(s)

    consumed: set = set()
    unmatched_ashby: List[Dict[str, Any]] = []

    for c in ashby_candidates:
        target: Optional[Dict[str, Any]] = None

        # LinkedIn match first (exact; available in legacy exports)
        url = _normalize_url(c.get("linkedin_url"))
        if url and url in by_linkedin and id(by_linkedin[url]) not in consumed:
            target = by_linkedin[url]
        else:
            # Name + company match (both must hold)
            name_key = _normalize_name_key(c.get("candidate_name"))
            matches = [
                s for s in by_name_key.get(name_key, [])
                if id(s) not in consumed
                and _company_matches(s.get("channel_name"), c.get("company_name"))
            ]
            if matches:
                target = max(matches, key=lambda s: s.get("submitted_at") or "")

        if target is not None:
            consumed.add(id(target))
            _apply_ashby_to_slack(target, c)
        else:
            c["also_in_slack"] = False
            unmatched_ashby.append(c)

    return slack_candidates + unmatched_ashby


def _map_ashby_status_api(candidate: Dict[str, Any]) -> str:
    """Map status from API format (snake_case fields)."""
    stage = (candidate.get("decision_status") or "").lower()
    stage_type = (candidate.get("stage_type") or "").lower()
    pipeline = (candidate.get("pipeline_stage") or "").lower()

    rejection_keywords = {"reject", "declined", "archived", "withdraw", "no hire"}
    if any(k in stage for k in rejection_keywords) or any(
        k in pipeline for k in rejection_keywords
    ):
        return "CLOSED"

    if stage_type in ("offer", "hired") or "offer" in stage or "hired" in stage:
        return "IN PROCESS — explicit"

    if candidate.get("pipeline_stage"):
        return "IN PROCESS — explicit"

    if candidate.get("decision_status"):
        return "IN PROCESS — unclear"

    return "IN PROCESS — unclear"


def _map_ashby_status_legacy(candidate: Dict[str, Any]) -> str:
    """Map status from legacy format (camelCase fields)."""
    stage = (candidate.get("currentStage") or "").lower()
    stage_type = (candidate.get("stageType") or "").lower()
    pipeline = (candidate.get("pipelineStage") or "").lower()

    rejection_keywords = {"reject", "declined", "archived", "withdraw", "no hire"}
    if any(k in stage for k in rejection_keywords) or any(
        k in pipeline for k in rejection_keywords
    ):
        return "CLOSED"

    if stage_type in ("offer", "hired") or "offer" in stage or "hired" in stage:
        return "IN PROCESS — explicit"

    if candidate.get("pipelineStage"):
        return "IN PROCESS — explicit"

    if candidate.get("currentStage"):
        return "IN PROCESS — unclear"

    return "IN PROCESS — unclear"
