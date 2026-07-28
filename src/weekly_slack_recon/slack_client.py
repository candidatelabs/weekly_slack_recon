from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


@dataclass
class SlackMessage:
    channel: str
    ts: str
    user: Optional[str]
    text: str
    thread_ts: Optional[str]
    reactions: List[Dict]


class SlackAPI:
    """Thin wrapper over Slack WebClient for the read-only operations we need."""

    def __init__(self, token: str) -> None:
        self.client = WebClient(token=token)

    def get_user_id_by_email(self, email: str) -> str:
        """Get user ID by email. Tries users.lookupByEmail first, falls back to users.list if scope missing."""
        # Try direct lookup first (requires users:read.email)
        try:
            resp = self.client.users_lookupByEmail(email=email)
            user = resp.get("user")
            if user and "id" in user:
                return user["id"]
        except SlackApiError as e:
            error_code = e.response.get("error", "")
            if error_code == "invalid_auth":
                raise RuntimeError(
                    f"Invalid or expired Slack token. Please check your token at https://api.slack.com/apps "
                    f"and regenerate if needed. Error: {error_code}"
                ) from e
            if error_code == "missing_scope":
                # Fallback: use users.list (requires only users:read)
                try:
                    cursor: Optional[str] = None
                    while True:
                        resp = self.client.users_list(limit=200, cursor=cursor)
                        for user in resp.get("members", []):
                            profile = user.get("profile", {})
                            if profile.get("email") == email:
                                return user["id"]
                        cursor = resp.get("response_metadata", {}).get("next_cursor")
                        if not cursor:
                            break
                    raise RuntimeError(f"No Slack user found for email {email}")
                except SlackApiError as e2:
                    raise RuntimeError(
                        f"Failed to look up user by email {email}. "
                        f"Need either 'users:read.email' scope (for direct lookup) or 'users:read' scope (for list fallback). "
                        f"Error: {e2.response.get('error', 'unknown')}"
                    ) from e2
            else:
                raise RuntimeError(f"Failed to look up user by email {email}: {error_code}") from e

        raise RuntimeError(f"No Slack user found for email {email}")

    def list_candidate_channels_for_user(self, user_id: str) -> List[Dict]:
        """Return public/private channels whose name starts with 'candidatelabs-' and where user is a member.
        
        Uses users_conversations API which directly returns only channels the user is in - much faster
        than listing all channels and checking membership for each one.
        """

        channels: List[Dict] = []
        cursor: Optional[str] = None

        while True:
            try:
                # users_conversations returns only channels the user is a member of
                resp = self.client.users_conversations(
                    user=user_id,
                    types="public_channel,private_channel",
                    limit=1000,
                    cursor=cursor,
                )
            except SlackApiError as e:
                raise RuntimeError(f"Failed to list user channels: {e.response['error']}") from e

            for ch in resp.get("channels", []):
                name = ch.get("name", "")
                # Slack Connect channels created on the CLIENT's side get an
                # "ext-" prefix locally (e.g. ext-candidatelabs-prometheus-
                # engineering) — same client channels, different creator.
                if name.startswith("candidatelabs-") or name.startswith("ext-candidatelabs-"):
                    channels.append(ch)

            cursor = resp.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break

        return channels

    def iter_channel_messages_since(self, channel_id: str, oldest_ts: float) -> Iterable[SlackMessage]:
        """Yield messages in a channel since a given Unix timestamp (inclusive)."""

        cursor: Optional[str] = None
        while True:
            try:
                resp = self.client.conversations_history(
                    channel=channel_id,
                    oldest=str(oldest_ts),
                    limit=200,
                    cursor=cursor,
                    inclusive=True,
                )
            except SlackApiError as e:
                raise RuntimeError(f"Failed to fetch history for channel {channel_id}: {e.response['error']}") from e

            for m in resp.get("messages", []):
                yield SlackMessage(
                    channel=channel_id,
                    ts=m["ts"],
                    user=m.get("user"),
                    text=m.get("text", ""),
                    thread_ts=m.get("thread_ts"),
                    reactions=m.get("reactions", []),
                )

            if not resp.get("has_more"):
                break

            cursor = resp.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break

    def get_thread_messages(self, channel_id: str, thread_ts: str, max_retries: int = 3) -> List[SlackMessage]:
        """Return all messages in a thread (including the parent).
        
        Handles rate limits with exponential backoff retries.
        Returns empty list if all retries fail (caller should continue processing).
        """
        
        for attempt in range(max_retries):
            try:
                resp = self.client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
                
                messages: List[SlackMessage] = []
                for m in resp.get("messages", []):
                    messages.append(
                        SlackMessage(
                            channel=channel_id,
                            ts=m["ts"],
                            user=m.get("user"),
                            text=m.get("text", ""),
                            thread_ts=m.get("thread_ts"),
                            reactions=m.get("reactions", []),
                        )
                    )
                
                return messages
                
            except SlackApiError as e:
                error_code = e.response.get("error", "")
                
                # Handle rate limiting
                if error_code == "ratelimited":
                    retry_after = int(e.response.get("headers", {}).get("Retry-After", "60"))
                    wait_time = retry_after + (attempt * 2)  # Add extra backoff for subsequent retries
                    
                    if attempt < max_retries - 1:
                        print(f"[RATE LIMIT] Waiting {wait_time} seconds before retry {attempt + 2}/{max_retries}...")
                        time.sleep(wait_time)
                        continue
                    else:
                        # All retries exhausted
                        print(f"[WARNING] Rate limited for channel {channel_id} thread {thread_ts} after {max_retries} attempts. Continuing without thread data.")
                        return []
                
                # For other errors, log and return empty list
                if attempt == max_retries - 1:
                    print(f"[WARNING] Failed to fetch thread replies for channel {channel_id} ts {thread_ts}: {error_code}. Continuing without thread data.")
                    return []
                else:
                    # Wait a bit before retrying non-rate-limit errors
                    time.sleep(1 * (attempt + 1))
                    continue
        
        return []

    def send_dm(
        self,
        user_id: str,
        text: str,
        max_retries: int = 3,
    ) -> Optional[str]:
        """Send a direct message to a user. Returns message ts if successful."""
        for attempt in range(max_retries):
            try:
                # Open a DM channel with the user
                resp = self.client.conversations_open(users=[user_id])
                dm_channel = resp.get("channel", {}).get("id")
                if not dm_channel:
                    print(f"[ERROR] Could not open DM channel with user {user_id}")
                    return None
                
                # Send the message
                resp = self.client.chat_postMessage(
                    channel=dm_channel,
                    text=text,
                )
                return resp.get("ts")
                
            except SlackApiError as e:
                error_code = e.response.get("error", "")
                
                if error_code == "ratelimited":
                    retry_after = int(e.response.get("headers", {}).get("Retry-After", "60"))
                    wait_time = retry_after + (attempt * 2)
                    
                    if attempt < max_retries - 1:
                        print(f"[RATE LIMIT] Waiting {wait_time} seconds before retry...")
                        time.sleep(wait_time)
                        continue
                
                print(f"[ERROR] Failed to send DM: {error_code}")
                return None
        
        return None

    def get_workspace_domain(self) -> str:
        """Get the workspace domain for building URLs."""
        try:
            resp = self.client.auth_test()
            url = resp.get("url", "")
            # URL is like https://candidatelabs.slack.com/
            if url:
                return url.rstrip("/").replace("https://", "")
            return "slack.com"
        except SlackApiError:
            return "slack.com"

    def post_thread_reply(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        max_retries: int = 3,
    ) -> Optional[str]:
        """Post a reply to a thread. Returns the message ts if successful, None otherwise.
        
        Args:
            channel_id: The channel ID to post in
            thread_ts: The thread timestamp to reply to
            text: The message text (can include <@USER_ID> mentions)
            max_retries: Number of retry attempts for rate limiting
            
        Returns:
            The message timestamp if successful, None if failed
        """
        for attempt in range(max_retries):
            try:
                resp = self.client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=text,
                )
                return resp.get("ts")
                
            except SlackApiError as e:
                error_code = e.response.get("error", "")
                
                if error_code == "ratelimited":
                    retry_after = int(e.response.get("headers", {}).get("Retry-After", "60"))
                    wait_time = retry_after + (attempt * 2)
                    
                    if attempt < max_retries - 1:
                        print(f"[RATE LIMIT] Waiting {wait_time} seconds before retry {attempt + 2}/{max_retries}...")
                        time.sleep(wait_time)
                        continue
                    else:
                        print(f"[WARNING] Rate limited for posting to {channel_id} thread {thread_ts} after {max_retries} attempts.")
                        return None
                
                print(f"[ERROR] Failed to post thread reply to {channel_id} thread {thread_ts}: {error_code}")
                return None
        
        return None

    def post_channel_message(
        self,
        channel_id: str,
        text: str,
        max_retries: int = 3,
    ) -> Optional[str]:
        """Post a top-level message to a channel. Returns message ts if successful."""
        for attempt in range(max_retries):
            try:
                resp = self.client.chat_postMessage(
                    channel=channel_id,
                    text=text,
                )
                return resp.get("ts")

            except SlackApiError as e:
                error_code = e.response.get("error", "")

                if error_code == "ratelimited":
                    retry_after = int(e.response.get("headers", {}).get("Retry-After", "60"))
                    wait_time = retry_after + (attempt * 2)

                    if attempt < max_retries - 1:
                        print(f"[RATE LIMIT] Waiting {wait_time} seconds before retry {attempt + 2}/{max_retries}...")
                        time.sleep(wait_time)
                        continue
                    else:
                        print(f"[WARNING] Rate limited for posting to {channel_id} after {max_retries} attempts.")
                        return None

                print(f"[ERROR] Failed to post message to {channel_id}: {error_code}")
                return None

        return None

    @staticmethod
    def parse_ts(ts: str) -> datetime:
        # Slack timestamps are like "1701985150.000200" (seconds.micros)
        seconds = float(ts)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)

