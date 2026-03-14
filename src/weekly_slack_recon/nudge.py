"""Slack Nudge functionality for following up on stale candidate submissions.

This module provides the logic for:
1. Identifying submissions that need a nudge (no ✅ or ⛔ for N days)
2. Tracking which threads have already been nudged
3. Posting nudge messages to threads
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from .config import Config
from .logic import CandidateSubmission, build_candidate_submissions
from .slack_client import SlackAPI
from .status_rules import StatusCategory


@dataclass
class NudgeRecord:
    """Record of a nudge sent to a thread."""
    channel_id: str
    thread_ts: str
    nudged_at: str  # ISO format timestamp
    candidate_name: str
    linkedin_url: str


class NudgeTracker:
    """Tracks which threads have been nudged and when, to support re-nudging."""

    def __init__(self, tracker_path: str) -> None:
        self.tracker_path = Path(tracker_path)
        self._nudged: Dict[str, NudgeRecord] = {}
        self._load()

    def _make_key(self, channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def _load(self) -> None:
        """Load existing nudge records from disk."""
        if not self.tracker_path.exists():
            return
        try:
            with open(self.tracker_path, "r") as f:
                data = json.load(f)
                for key, record in data.items():
                    self._nudged[key] = NudgeRecord(**record)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[WARNING] Could not load nudge tracker: {e}")

    def _save(self) -> None:
        """Persist nudge records to disk."""
        data = {key: asdict(record) for key, record in self._nudged.items()}
        with open(self.tracker_path, "w") as f:
            json.dump(data, f, indent=2)

    def is_nudged(self, channel_id: str, thread_ts: str) -> bool:
        """Check if a thread has already been nudged."""
        key = self._make_key(channel_id, thread_ts)
        return key in self._nudged

    def get_last_nudged_at(self, channel_id: str, thread_ts: str) -> Optional[datetime]:
        """Return the datetime of the last nudge for a thread, or None if never nudged."""
        key = self._make_key(channel_id, thread_ts)
        record = self._nudged.get(key)
        if record is None:
            return None
        return datetime.fromisoformat(record.nudged_at)

    def mark_nudged(
        self,
        channel_id: str,
        thread_ts: str,
        candidate_name: str,
        linkedin_url: str,
    ) -> None:
        """Mark a thread as nudged."""
        key = self._make_key(channel_id, thread_ts)
        self._nudged[key] = NudgeRecord(
            channel_id=channel_id,
            thread_ts=thread_ts,
            nudged_at=datetime.now(tz=timezone.utc).isoformat(),
            candidate_name=candidate_name,
            linkedin_url=linkedin_url,
        )
        self._save()

    def get_all_nudged(self) -> List[NudgeRecord]:
        """Return all nudge records."""
        return list(self._nudged.values())

    def clear_old_records(self, days: int = 30) -> int:
        """Remove records older than N days. Returns count removed."""
        now = datetime.now(tz=timezone.utc)
        to_remove = []
        for key, record in self._nudged.items():
            nudged_at = datetime.fromisoformat(record.nudged_at)
            if (now - nudged_at).days > days:
                to_remove.append(key)
        
        for key in to_remove:
            del self._nudged[key]
        
        if to_remove:
            self._save()
        
        return len(to_remove)


def find_submissions_needing_nudge(
    cfg: Config,
    submissions: List[CandidateSubmission],
    tracker: NudgeTracker,
) -> List[CandidateSubmission]:
    """Find submissions that need a nudge (or re-nudge).
    
    Criteria:
    1. Status is IN_PROCESS_UNCLEAR (no ✅ or ⛔)
    2. Days since submission >= nudge_days
    3. Either never nudged, or last nudge was >= nudge_days ago
    """
    now = datetime.now(tz=timezone.utc)
    needing_nudge = []
    
    for sub in submissions:
        # Only nudge unclear submissions (no explicit status)
        if sub.status != StatusCategory.IN_PROCESS_UNCLEAR:
            continue
        
        # Check if enough days have passed since submission
        if sub.days_since_submission < cfg.nudge_days:
            continue
        
        thread_ts = str(sub.submitted_at.timestamp())
        last_nudged = tracker.get_last_nudged_at(sub.channel_id, thread_ts)
        
        if last_nudged is not None:
            # Already nudged — only re-nudge if enough days have passed since last nudge
            days_since_nudge = (now - last_nudged).days
            if days_since_nudge < cfg.nudge_days:
                continue
        
        needing_nudge.append(sub)
    
    return needing_nudge


def send_nudge(
    slack: SlackAPI,
    cfg: Config,
    submission: CandidateSubmission,
    tracker: NudgeTracker,
    dk_user_id: str,
) -> bool:
    """Send a nudge message to a submission thread.
    
    Returns True if nudge was sent successfully.
    """
    # Convert submission time to Slack timestamp format
    thread_ts = f"{submission.submitted_at.timestamp():.6f}"
    
    # Build the nudge message with user mention
    message = f"autoreminder: check status -- <@{dk_user_id}> to check if any updates needed"
    
    # Post the reply
    result = slack.post_thread_reply(
        channel_id=submission.channel_id,
        thread_ts=thread_ts,
        text=message,
    )
    
    if result:
        # Mark as nudged
        tracker.mark_nudged(
            channel_id=submission.channel_id,
            thread_ts=thread_ts,
            candidate_name=submission.candidate_name,
            linkedin_url=submission.linkedin_url,
        )
        print(f"[NUDGE] Sent nudge for {submission.candidate_name} in #{submission.channel_name}")
        return True
    else:
        print(f"[ERROR] Failed to send nudge for {submission.candidate_name} in #{submission.channel_name}")
        return False


def run_nudge_check(
    cfg: Config,
    slack: Optional[SlackAPI] = None,
    dry_run: bool = False,
    dm_only: Optional[bool] = None,
) -> Dict:
    """Run a full nudge check across all candidate channels.
    
    Args:
        cfg: Configuration object
        slack: Optional SlackAPI instance (created if not provided)
        dry_run: If True, don't actually send nudges, just report what would be sent
        dm_only: If True, only send DM summary (no thread replies). Defaults to cfg.nudge_dm_only.
        
    Returns:
        Dict with results: submissions_checked, nudges_needed, nudges_sent
    """
    import sys
    
    if slack is None:
        slack = SlackAPI(cfg.slack_bot_token)
        print("[INFO] Slack client initialized", flush=True)
    
    tracker = NudgeTracker(cfg.nudge_tracker_path)
    
    # Get or look up DK's user ID
    dk_user_id = cfg.dk_user_id
    if not dk_user_id:
        print(f"[INFO] Looking up user ID for {cfg.dk_email}...", flush=True)
        dk_user_id = slack.get_user_id_by_email(cfg.dk_email)
        print(f"[INFO] Found user ID: {dk_user_id}", flush=True)
    
    # Get candidate channels
    print("[INFO] Fetching candidate channels...", flush=True)
    channels = slack.list_candidate_channels_for_user(dk_user_id)
    print(f"[INFO] Found {len(channels)} candidate channels", flush=True)
    
    # Build submissions (this scans all channels - can take a while)
    print(f"[INFO] Scanning {len(channels)} channels for candidate submissions (this may take a few minutes)...", flush=True)
    submissions, debug_info = build_candidate_submissions(
        cfg, slack, dk_user_id, channels
    )
    print(f"[INFO] Found {len(submissions)} total submissions", flush=True)
    
    # Find those needing nudges
    needing_nudge = find_submissions_needing_nudge(cfg, submissions, tracker)
    print(f"[INFO] {len(needing_nudge)} submissions need a nudge", flush=True)
    
    results = {
        "submissions_checked": len(submissions),
        "nudges_needed": len(needing_nudge),
        "nudges_sent": 0,
        "dry_run": dry_run,
        "submissions_needing_nudge": [
            {
                "candidate_name": s.candidate_name,
                "channel_name": s.channel_name,
                "days_since_submission": s.days_since_submission,
                "linkedin_url": s.linkedin_url,
            }
            for s in needing_nudge
        ],
    }
    
    # Resolve dm_only: CLI flag overrides, then fall back to config
    if dm_only is None:
        dm_only = cfg.nudge_dm_only

    if dry_run:
        print("[DRY RUN] Would send the following nudges:")
        if dm_only:
            print("[DRY RUN] Mode: DM-only (no thread replies)")
        for sub in needing_nudge:
            print(f"  - {sub.candidate_name} in #{sub.channel_name} ({sub.days_since_submission} days)")
    elif dm_only:
        # DM-only mode: skip thread replies, send DM summary, still mark as nudged
        print("[DM-ONLY] Skipping thread replies, will send DM summary only")
        for sub in needing_nudge:
            thread_ts = f"{sub.submitted_at.timestamp():.6f}"
            tracker.mark_nudged(
                channel_id=sub.channel_id,
                thread_ts=thread_ts,
                candidate_name=sub.candidate_name,
                linkedin_url=sub.linkedin_url,
            )
            results["nudges_sent"] += 1
            print(f"[DM-ONLY] Marked nudge for {sub.candidate_name} in #{sub.channel_name}")

        if needing_nudge:
            _send_nudge_summary_dm(slack, dk_user_id, needing_nudge)
            _send_nudge_summary_email(cfg, needing_nudge, slack)
    else:
        # Full mode: post thread replies + DM summary
        nudged_submissions = []
        for sub in needing_nudge:
            if send_nudge(slack, cfg, sub, tracker, dk_user_id):
                results["nudges_sent"] += 1
                nudged_submissions.append(sub)

        # Send DM summary with links to all nudged threads
        if nudged_submissions:
            _send_nudge_summary_dm(slack, dk_user_id, nudged_submissions)
            _send_nudge_summary_email(cfg, nudged_submissions, slack)

    # Clean up old tracker records
    removed = tracker.clear_old_records(days=cfg.lookback_days)
    if removed:
        print(f"[INFO] Cleaned up {removed} old nudge records")
    
    return results


def _send_nudge_summary_dm(
    slack: SlackAPI,
    dk_user_id: str,
    submissions: List[CandidateSubmission],
) -> None:
    """Send a DM to DK with links to all nudged threads."""
    
    # Get workspace domain for building URLs
    domain = slack.get_workspace_domain()
    
    # Build the summary message
    lines = [f"*Nudge Summary*: {len(submissions)} candidates need follow-up\n"]
    
    for sub in submissions:
        # Build Slack thread URL: https://workspace.slack.com/archives/CHANNEL/pTIMESTAMP
        # Thread ts like "1768335306.014279" becomes "p1768335306014279"
        thread_ts_for_url = sub.submitted_at.timestamp()
        ts_str = f"{thread_ts_for_url:.6f}".replace(".", "")
        thread_url = f"https://{domain}/archives/{sub.channel_id}/p{ts_str}"
        
        lines.append(f"• <{thread_url}|{sub.candidate_name}> in #{sub.channel_name} ({sub.days_since_submission} days)")
    
    message = "\n".join(lines)
    
    result = slack.send_dm(dk_user_id, message)
    if result:
        print(f"[INFO] Sent DM summary with {len(submissions)} nudge links", flush=True)
    else:
        print("[WARNING] Failed to send DM summary", flush=True)


def _send_nudge_summary_email(
    cfg: Config,
    submissions: List[CandidateSubmission],
    slack: SlackAPI,
) -> None:
    """Email the nudge summary to DK via Gmail, with hyperlinked Slack threads."""
    from .candidate_outreach import send_email_via_gmail

    domain = slack.get_workspace_domain()

    lines = [f"<b>Nudge Summary:</b> {len(submissions)} candidates need follow-up<br><br>"]
    for sub in submissions:
        thread_ts_for_url = sub.submitted_at.timestamp()
        ts_str = f"{thread_ts_for_url:.6f}".replace(".", "")
        thread_url = f"https://{domain}/archives/{sub.channel_id}/p{ts_str}"
        lines.append(
            f'• <a href="{thread_url}">{sub.candidate_name}</a>'
            f" in #{sub.channel_name} ({sub.days_since_submission} days)<br>"
        )

    subject = f"Nudge Summary — {len(submissions)} candidates need follow-up"
    body = "\n".join(lines)

    try:
        send_email_via_gmail(
            to=cfg.dk_email,
            subject=subject,
            body=body,
            credentials_path=cfg.gmail_credentials_path,
            token_path=cfg.gmail_token_path,
        )
        print(f"[INFO] Sent nudge summary email to {cfg.dk_email}", flush=True)
    except Exception as e:
        print(f"[WARNING] Failed to send nudge email: {e}", flush=True)
