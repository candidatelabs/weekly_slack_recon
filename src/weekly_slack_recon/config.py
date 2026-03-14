from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import json
import os
from typing import Optional

from dotenv import load_dotenv


@dataclass
class Config:
    slack_bot_token: str
    dk_email: str = "dkimball@candidatelabs.com"
    lookback_days: int = 60
    unclear_followup_days: int = 7
    inactivity_days: int = 5
    include_confused_close: bool = False
    output_markdown_path: Optional[str] = "weekly_slack_reconciliation.md"
    # Nudge feature settings
    nudge_days: int = 3  # Days without ✅ or ⛔ before nudging
    slack_app_token: Optional[str] = None  # App-level token (xapp-...) for Socket Mode
    dk_user_id: Optional[str] = None  # David Kimball's Slack user ID for tagging
    nudge_tracker_path: str = ".nudge_tracker.json"  # Track nudged threads
    nudge_dm_only: bool = False  # If True, send DM summary only (no thread replies)
    # LLM enrichment settings
    anthropic_api_key: Optional[str] = None  # Anthropic API key for Claude
    enrichment_model: str = "claude-sonnet-4-20250514"  # Claude model to use
    enrichment_max_tokens: int = 500  # Max tokens per candidate summary
    # Ashby integration
    ashby_json_path: Optional[str] = None  # Path to Ashby JSON export file
    # Pipeline Status Check settings
    gmail_credentials_path: str = "./credentials.json"
    gmail_token_path: str = "./gmail_token.json"
    gcal_token_path: str = "./gcal_token.json"
    gcal_lookback_days: int = 7
    gcal_lookahead_days: int = 14
    status_check_model: str = "claude-sonnet-4-6"
    client_contact_map: dict = field(default_factory=dict)  # {"Agave": "Akshay", "Charta Health": "Alex"}

    @property
    def lookback_timedelta(self) -> timedelta:
        return timedelta(days=self.lookback_days)

    @property
    def unclear_followup_timedelta(self) -> timedelta:
        return timedelta(days=self.unclear_followup_days)

    @property
    def inactivity_timedelta(self) -> timedelta:
        return timedelta(days=self.inactivity_days)


def load_config() -> Config:
    """Load configuration from environment variables / .env file."""

    load_dotenv(override=True)

    slack_bot_token = os.getenv("SLACK_BOT_TOKEN")
    if not slack_bot_token:
        raise RuntimeError(
            "SLACK_BOT_TOKEN must be set in environment or .env file. "
            "This can be either a User OAuth Token (xoxp-...) or Bot Token (xoxb-...)."
        )

    dk_email = os.getenv("DK_EMAIL", "dkimball@candidatelabs.com")

    def _int_env(name: str, default: int) -> int:
        val = os.getenv(name)
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    lookback_days = _int_env("LOOKBACK_DAYS", 60)
    unclear_followup_days = _int_env("UNCLEAR_FOLLOWUP_DAYS", 7)
    inactivity_days = _int_env("INACTIVITY_DAYS", 5)

    include_confused_close = os.getenv("INCLUDE_CONFUSED_CLOSE", "false").lower() in {"1", "true", "yes", "y"}

    output_markdown_path = os.getenv("OUTPUT_MARKDOWN_PATH", "weekly_slack_reconciliation.md")

    # Nudge feature settings
    nudge_days = _int_env("NUDGE_DAYS", 3)
    slack_app_token = os.getenv("SLACK_APP_TOKEN")  # xapp-... for Socket Mode
    dk_user_id = os.getenv("DK_USER_ID")  # Can be set directly or looked up from email
    nudge_tracker_path = os.getenv("NUDGE_TRACKER_PATH", ".nudge_tracker.json")
    nudge_dm_only = os.getenv("NUDGE_DM_ONLY", "false").lower() in {"1", "true", "yes", "y"}

    # LLM enrichment settings
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
    enrichment_model = os.getenv("ENRICHMENT_MODEL", "claude-sonnet-4-20250514")
    enrichment_max_tokens = _int_env("ENRICHMENT_MAX_TOKENS", 500)

    # Ashby integration
    ashby_json_path = os.getenv("ASHBY_JSON_PATH") or None

    # Pipeline Status Check settings
    gmail_credentials_path = os.getenv("GMAIL_CREDENTIALS_PATH", "./credentials.json")
    gmail_token_path = os.getenv("GMAIL_TOKEN_PATH", "./gmail_token.json")
    gcal_token_path = os.getenv("GCAL_TOKEN_PATH", "./gcal_token.json")
    gcal_lookback_days = _int_env("GCAL_LOOKBACK_DAYS", 7)
    gcal_lookahead_days = _int_env("GCAL_LOOKAHEAD_DAYS", 14)
    status_check_model = os.getenv("STATUS_CHECK_MODEL", "claude-sonnet-4-6")
    client_contact_map_raw = os.getenv("CLIENT_CONTACT_MAP", "{}")
    try:
        client_contact_map = json.loads(client_contact_map_raw)
    except Exception:
        client_contact_map = {}

    return Config(
        slack_bot_token=slack_bot_token,
        dk_email=dk_email,
        lookback_days=lookback_days,
        unclear_followup_days=unclear_followup_days,
        inactivity_days=inactivity_days,
        include_confused_close=include_confused_close,
        output_markdown_path=output_markdown_path,
        nudge_days=nudge_days,
        slack_app_token=slack_app_token,
        dk_user_id=dk_user_id,
        nudge_tracker_path=nudge_tracker_path,
        nudge_dm_only=nudge_dm_only,
        anthropic_api_key=anthropic_api_key,
        enrichment_model=enrichment_model,
        enrichment_max_tokens=enrichment_max_tokens,
        ashby_json_path=ashby_json_path,
        gmail_credentials_path=gmail_credentials_path,
        gmail_token_path=gmail_token_path,
        gcal_token_path=gcal_token_path,
        gcal_lookback_days=gcal_lookback_days,
        gcal_lookahead_days=gcal_lookahead_days,
        status_check_model=status_check_model,
        client_contact_map=client_contact_map,
    )
