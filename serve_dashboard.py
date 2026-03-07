#!/usr/bin/env python3
"""
HTTP server to serve the dashboard with API endpoints for data generation
"""
import http.server
import socketserver
import traceback
import webbrowser
import os
import sys
import json
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv
from weekly_slack_recon.config import load_config
from weekly_slack_recon.slack_client import SlackAPI
from weekly_slack_recon.logic import build_candidate_submissions, CandidateSubmission
from weekly_slack_recon.reporting import write_markdown, write_json
from weekly_slack_recon.enrichment import enrich_submissions
from weekly_slack_recon.ashby_importer import load_ashby_export, merge_ashby_into_submissions, find_latest_ashby_export
from weekly_slack_recon.status_check_runner import run_status_check
from weekly_slack_recon.calendar_client import CalendarClient
from weekly_slack_recon.message_composer import DraftMessage
from weekly_slack_recon.candidate_outreach import (
    search_candidates,
    get_candidate_opportunities,
    compose_candidate_message,
    lookup_candidate_email,
    send_email_via_gmail,
)

# Cached SlackAPI instance for follow-up sends
_slack_instance: SlackAPI = None

# Cache for Slack user ID -> display name (shared across thread fetches)
_user_display_cache: dict = {}

PORT = 8001
DIRECTORY = Path(__file__).parent
load_dotenv()

# Global state for generation progress
generation_status = {
    "running": False,
    "progress": "",
    "error": None,
    "completed": False,
    "ashby_auth_required": False,   # True when session cookie has expired
}

# Global state for Ashby sync (extraction + import)
ashby_sync_status = {
    "running": False,
    "detail": "",
    "error": None,
    "completed": False,
    "auth_required": False,
    "imported": 0,
}

# Global state for Pipeline Status Check
status_check_status = {
    "running": False,
    "phase": "",        # "scanning", "gathering", "composing", "complete"
    "current": 0,
    "total": 0,
    "detail": "",
    "error": None,
    "completed": False,
}
_status_check_drafts: list = []  # List of DraftMessage dicts

# Global state for enrichment progress
enrichment_status = {
    "running": False,
    "phase": "",        # "gathering", "analyzing", "complete"
    "current": 0,
    "total": 0,
    "detail": "",
    "error": None,
    "completed": False,
    "results": None,    # List of enrichment result dicts when done
}


class DashboardRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIRECTORY), **kwargs)
    
    def end_headers(self):
        # Add CORS headers
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()
    
    def do_GET(self):
        parsed_path = urlparse(self.path)

        if parsed_path.path == '/api/status':
            self.handle_api_status()
        elif parsed_path.path == '/api/generate':
            self.handle_api_generate()
        elif parsed_path.path == '/api/enrich/status':
            self.handle_api_enrich_status()
        elif parsed_path.path == '/api/enrich/results':
            self.handle_api_enrich_results()
        elif parsed_path.path == '/api/thread':
            self.handle_api_thread()
        elif parsed_path.path == '/api/channel-members':
            self.handle_api_channel_members()
        elif parsed_path.path == '/api/ashby/status':
            self.handle_api_ashby_status()
        elif parsed_path.path == '/api/ashby/sync/status':
            self.handle_api_ashby_sync_status()
        elif parsed_path.path == '/api/status-check/status':
            self.handle_api_status_check_status()
        elif parsed_path.path == '/api/status-check/drafts':
            self.handle_api_status_check_drafts()
        elif parsed_path.path == '/api/candidate-outreach/search':
            self.handle_api_candidate_outreach_search()
        else:
            # Serve static files
            super().do_GET()
    
    def do_POST(self):
        parsed_path = urlparse(self.path)

        if parsed_path.path == '/api/generate':
            self.handle_api_generate_post()
        elif parsed_path.path == '/api/send-followup':
            self.handle_api_send_followup()
        elif parsed_path.path == '/api/send-thread-reply':
            self.handle_api_send_thread_reply()
        elif parsed_path.path == '/api/enrich':
            self.handle_api_enrich()
        elif parsed_path.path == '/api/enrich/clear':
            self.handle_api_enrich_clear()
        elif parsed_path.path == '/api/ashby/import':
            self.handle_api_ashby_import()
        elif parsed_path.path == '/api/ashby/set-cookie':
            self.handle_api_ashby_set_cookie()
        elif parsed_path.path == '/api/ashby/login':
            self.handle_api_ashby_login()
        elif parsed_path.path == '/api/ashby/sync':
            self.handle_api_ashby_sync()
        elif parsed_path.path == '/api/status-check/generate':
            self.handle_api_status_check_generate()
        elif parsed_path.path == '/api/status-check/approve':
            self.handle_api_status_check_approve()
        elif parsed_path.path == '/api/candidate-outreach/compose':
            self.handle_api_candidate_outreach_compose()
        elif parsed_path.path == '/api/candidate-outreach/lookup-email':
            self.handle_api_candidate_outreach_lookup_email()
        elif parsed_path.path == '/api/candidate-outreach/send-email':
            self.handle_api_candidate_outreach_send_email()
        else:
            self.send_error(404)

    def do_PUT(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path.startswith('/api/status-check/drafts/'):
            self.handle_api_status_check_draft_update()
        else:
            self.send_error(404)
    
    def handle_api_status(self):
        """Return current generation status"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        response = json.dumps(generation_status)
        self.wfile.write(response.encode())
    
    def handle_api_generate(self):
        """Start generation in background (GET request)"""
        if generation_status["running"]:
            self.send_response(409)  # Conflict - already running
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Generation already in progress"}).encode())
            return
        
        # Start generation in background thread
        thread = threading.Thread(target=run_generation, daemon=True)
        thread.start()
        
        self.send_response(202)  # Accepted
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "started"}).encode())
    
    def handle_api_generate_post(self):
        """Start generation with POST body (for future use with settings)"""
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        
        try:
            settings = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            settings = {}
        
        if generation_status["running"]:
            self.send_response(409)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Generation already in progress"}).encode())
            return
        
        # Start generation with settings
        thread = threading.Thread(target=run_generation, args=(settings,), daemon=True)
        thread.start()
        
        self.send_response(202)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "started"}).encode())


    def handle_api_send_followup(self):
        """Send a follow-up message to a Slack channel."""
        global _slack_instance

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        try:
            payload = json.loads(body)
        except Exception:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        channel_id = payload.get("channel_id")
        message = payload.get("message")

        if not channel_id or not message:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "channel_id and message are required"}).encode())
            return

        try:
            if _slack_instance is None:
                cfg = load_config()
                _slack_instance = SlackAPI(token=cfg.slack_bot_token)

            ts = _slack_instance.post_channel_message(channel_id, message)

            if ts:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "ts": ts}).encode())
            else:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Failed to send message"}).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())


    def handle_api_thread(self):
        """Fetch a Slack thread's messages and return as JSON."""
        global _slack_instance, _user_display_cache

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        channel_id = params.get("channel_id", [None])[0]
        thread_ts = params.get("thread_ts", [None])[0]

        if not channel_id or not thread_ts:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "channel_id and thread_ts are required"}).encode())
            return

        try:
            if _slack_instance is None:
                cfg = load_config()
                _slack_instance = SlackAPI(token=cfg.slack_bot_token)

            messages = _slack_instance.get_thread_messages(channel_id, thread_ts)

            result = []
            for msg in messages:
                # Resolve user display name
                author = self._resolve_user(msg.user)
                ts_dt = datetime.fromtimestamp(float(msg.ts), tz=timezone.utc)
                result.append({
                    "author": author,
                    "text": msg.text,
                    "timestamp": ts_dt.isoformat(),
                    "is_parent": msg.ts == thread_ts or (msg.thread_ts and msg.ts == msg.thread_ts),
                    "user_id": msg.user,
                })

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "messages": result}).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _resolve_user(self, user_id):
        """Resolve a Slack user ID to display name, with caching."""
        global _user_display_cache, _slack_instance
        if not user_id:
            return "unknown"
        if user_id in _user_display_cache:
            return _user_display_cache[user_id]
        try:
            resp = _slack_instance.client.users_info(user=user_id)
            user = resp.get("user", {})
            profile = user.get("profile", {})
            display = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("name")
                or user_id
            )
            _user_display_cache[user_id] = display
            return display
        except Exception:
            _user_display_cache[user_id] = user_id
            return user_id

    def handle_api_send_thread_reply(self):
        """Send a reply to a Slack thread."""
        global _slack_instance

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        try:
            payload = json.loads(body)
        except Exception:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        channel_id = payload.get("channel_id")
        thread_ts = payload.get("thread_ts")
        message = payload.get("message")

        if not channel_id or not thread_ts or not message:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "channel_id, thread_ts, and message are required"}).encode())
            return

        try:
            if _slack_instance is None:
                cfg = load_config()
                _slack_instance = SlackAPI(token=cfg.slack_bot_token)

            ts = _slack_instance.post_thread_reply(channel_id, thread_ts, message)

            if ts:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "ts": ts}).encode())
            else:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Failed to send message"}).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_api_channel_members(self):
        """Get channel members for mention autocomplete."""
        global _slack_instance

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        channel_id = params.get("channel_id", [None])[0]

        if not channel_id:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "channel_id is required"}).encode())
            return

        try:
            if _slack_instance is None:
                cfg = load_config()
                _slack_instance = SlackAPI(token=cfg.slack_bot_token)

            # Get channel members
            resp = _slack_instance.client.conversations_members(channel=channel_id, limit=1000)
            member_ids = resp.get("members", [])

            # Resolve member IDs to names
            members = []
            for user_id in member_ids:
                display_name = self._resolve_user(user_id)
                members.append({
                    "id": user_id,
                    "name": display_name
                })

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "members": members}).encode())

        except Exception as e:
            print(f"[ERROR] Failed to fetch channel members: {e}")
            traceback.print_exc()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_api_enrich_status(self):
        """Return current enrichment status."""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        # Don't send full results in status poll (too large); client fetches them separately
        status_copy = {k: v for k, v in enrichment_status.items() if k != "results"}
        status_copy["has_results"] = enrichment_status["results"] is not None
        response = json.dumps(status_copy)
        self.wfile.write(response.encode())

    def handle_api_enrich(self):
        """Start enrichment in background. POST body can specify candidate filters."""
        global enrichment_status

        if enrichment_status["running"]:
            self.send_response(409)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Enrichment already in progress"}).encode())
            return

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}

        # Start enrichment in background thread
        thread = threading.Thread(target=run_enrichment, args=(payload,), daemon=True)
        thread.start()

        self.send_response(202)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "started"}).encode())

    def handle_api_enrich_results(self):
        """Return enrichment results (called after enrichment completes)."""
        if enrichment_status["results"] is None:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "No enrichment results available"}).encode())
            return

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"results": enrichment_status["results"]}).encode())

    def handle_api_enrich_clear(self):
        """Clear all AI summaries from the JSON data file."""
        try:
            json_path = DIRECTORY / "weekly_slack_reconciliation.json"
            if not json_path.exists():
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No data file found"}).encode())
                return

            with open(json_path, "r") as f:
                data = json.load(f)

            cleared = 0
            for sub in data.get("submissions", []):
                if sub.get("ai_summary") or sub.get("ai_enriched_at"):
                    sub["ai_summary"] = None
                    sub["ai_enriched_at"] = None
                    cleared += 1

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "cleared": cleared}).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())


    def handle_api_ashby_status(self):
        """Return info about the configured Ashby JSON export path."""
        try:
            cfg = load_config()
            path_str = cfg.ashby_json_path
            if path_str:
                try:
                    resolved = find_latest_ashby_export(path_str)
                    p = Path(resolved)
                    stat = p.stat()
                    payload = {
                        "configured": True,
                        "exists": True,
                        "path": path_str,
                        "resolved_file": resolved,
                        "modified_at": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                        "size_bytes": stat.st_size,
                    }
                except FileNotFoundError:
                    payload = {"configured": True, "exists": False, "path": path_str}
            else:
                payload = {"configured": False, "exists": False}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_api_ashby_import(self):
        """Import Ashby candidates from a JSON export and merge into submissions."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}

        # Path can come from the request body or fall back to config
        ashby_path = (payload.get("path") or "").strip()
        if not ashby_path:
            try:
                cfg = load_config()
                ashby_path = cfg.ashby_json_path or ""
            except Exception:
                ashby_path = ""

        if not ashby_path:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": (
                    "No Ashby JSON path provided. "
                    "Set ASHBY_JSON_PATH in .env or pass 'path' in the request body."
                )
            }).encode())
            return

        try:
            ashby_path = find_latest_ashby_export(ashby_path)
            ashby_candidates = load_ashby_export(ashby_path)

            json_path = DIRECTORY / "weekly_slack_reconciliation.json"
            if json_path.exists():
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {
                    "submissions": [],
                    "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                }

            existing = data.get("submissions", [])
            merged = merge_ashby_into_submissions(existing, ashby_candidates)

            data["submissions"] = merged
            data["ashby_imported_at"] = datetime.now(tz=timezone.utc).isoformat()
            data["ashby_candidate_count"] = len(ashby_candidates)

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            print(
                f"[ASHBY] Imported {len(ashby_candidates)} candidates from {ashby_path}"
            )

            file_modified_at = datetime.fromtimestamp(
                Path(ashby_path).stat().st_mtime, tz=timezone.utc
            ).isoformat()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ok": True,
                "imported": len(ashby_candidates),
                "total": len(merged),
                "file_modified_at": file_modified_at,
                "resolved_file": ashby_path,
            }).encode())

        except FileNotFoundError as e:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
        except Exception as e:
            traceback.print_exc()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_api_ashby_login(self):
        """Launch Playwright browser for Ashby SSO login."""
        try:
            auth_cmd = [
                "node", "--loader", "ts-node/esm",
                "src/cli.ts", "auth",
            ]
            # Launch in background — opens a Chrome window for the user
            proc = subprocess.Popen(
                auth_cmd,
                cwd=str(ASHBY_AUTOMATION_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            print(f"[ASHBY] Launched Playwright auth (pid={proc.pid}). User should log in and close the browser.")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ok": True,
                "message": "Browser window opened. Log in with Google, then close the browser window.",
                "pid": proc.pid,
            }).encode())
        except Exception as e:
            traceback.print_exc()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_api_ashby_set_cookie(self):
        """Save a fresh Ashby session cookie, then re-run extraction + import."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}

        cookie = (payload.get("cookie") or "").strip()
        if not cookie:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "cookie is required"}).encode())
            return

        # Auto-detect format:
        # • Full cookie header  → "ashby_session_token=xxx; other=yyy"  (pass as-is)
        # • Bare token value    → "eyJ..." or "s%3A..."                  (wrap it)
        # Heuristic: if the input already contains "ashby_session_token" or has
        # multiple "name=value" pairs (semicolons), treat as full header.
        if "ashby_session_token" not in cookie and ";" not in cookie:
            # Assume the user pasted just the raw token value; wrap it so
            # createSessionFromCookieHeader() can find it.
            cookie = f"ashby_session_token={cookie}"
            print(f"[ASHBY] Detected bare token value; wrapped as ashby_session_token=...")

        try:
            # 1. Save the cookie using the auth-cookie CLI command
            auth_cmd = [
                "node", "--loader", "ts-node/esm",
                "src/cli.ts", "auth-cookie",
                "--cookie", cookie,
            ]
            auth_result = subprocess.run(
                auth_cmd,
                cwd=str(ASHBY_AUTOMATION_DIR),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if auth_result.returncode != 0:
                raise RuntimeError(
                    f"auth-cookie failed: {auth_result.stderr[-300:]}"
                )

            # 2. Re-run extraction with the fresh session
            cfg = load_config()
            ashby_path = cfg.ashby_json_path or ""
            if not ashby_path:
                raise RuntimeError("ASHBY_JSON_PATH not configured")

            extracted = _run_ashby_extraction(ashby_path)
            if not extracted:
                raise RuntimeError("Extraction failed even after saving new cookie")

            # 3. Import the fresh data
            ashby_file = find_latest_ashby_export(ashby_path)
            ashby_candidates = load_ashby_export(ashby_file)
            json_path = DIRECTORY / "weekly_slack_reconciliation.json"
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["submissions"] = merge_ashby_into_submissions(
                data.get("submissions", []), ashby_candidates
            )
            data["ashby_imported_at"] = datetime.now(tz=timezone.utc).isoformat()
            data["ashby_candidate_count"] = len(ashby_candidates)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            generation_status["ashby_auth_required"] = False
            print(f"[ASHBY] Cookie saved + imported {len(ashby_candidates)} candidates")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ok": True,
                "imported": len(ashby_candidates),
            }).encode())

        except Exception as e:
            traceback.print_exc()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())


    # ── Pipeline Status Check endpoints ──────────────────────────────────────

    def handle_api_status_check_generate(self):
        """Start the status-check pipeline in a background thread."""
        global status_check_status, _status_check_drafts

        if status_check_status["running"]:
            self.send_response(409)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Status check already in progress"}).encode())
            return

        thread = threading.Thread(target=run_status_check_background, daemon=True)
        thread.start()

        self.send_response(202)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "started"}).encode())

    def handle_api_status_check_status(self):
        """Return current status-check pipeline progress."""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(status_check_status).encode())

    def handle_api_status_check_drafts(self):
        """Return all drafted messages."""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"drafts": _status_check_drafts}).encode())

    def handle_api_status_check_draft_update(self):
        """Update a draft's message_text or status (e.g. mark skipped)."""
        global _status_check_drafts

        parsed = urlparse(self.path)
        draft_id = parsed.path.split('/')[-1]

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        for draft in _status_check_drafts:
            if draft.get("draft_id") == draft_id:
                if "message_text" in payload:
                    draft["message_text"] = payload["message_text"]
                if "status" in payload and payload["status"] in ("pending", "skipped"):
                    draft["status"] = payload["status"]
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "draft": draft}).encode())
                return

        self.send_response(404)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"error": "Draft not found"}).encode())

    def handle_api_status_check_approve(self):
        """Approve and post one or more draft messages to their Slack channels."""
        global _slack_instance, _status_check_drafts

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        approve_all = payload.get("all", False)
        draft_ids = set(payload.get("draft_ids", []))

        if _slack_instance is None:
            cfg = load_config()
            _slack_instance = SlackAPI(token=cfg.slack_bot_token)

        sent = []
        errors = []

        for draft in _status_check_drafts:
            if draft.get("status") == "skipped":
                continue
            if not approve_all and draft.get("draft_id") not in draft_ids:
                continue

            try:
                ts = _slack_instance.post_channel_message(
                    draft["channel_id"], draft["message_text"]
                )
                draft["status"] = "approved"
                draft["sent_ts"] = ts
                sent.append({"draft_id": draft["draft_id"], "client_name": draft["client_name"], "ts": ts})

                # Audit log
                _append_status_check_log(draft)
            except Exception as e:
                errors.append({"draft_id": draft.get("draft_id"), "error": str(e)})

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "sent": sent, "errors": errors}).encode())


    # ── Candidate Outreach endpoints ──────────────────────────────────────────

    def handle_api_candidate_outreach_search(self):
        """Return candidate names matching a search query."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        query = params.get("q", [""])[0].strip()

        json_path = str(DIRECTORY / "weekly_slack_reconciliation.json")
        try:
            results = search_candidates(query, json_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"candidates": results}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_api_candidate_outreach_compose(self):
        """Compose a candidate check-in email from pipeline data."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        candidate_name = (payload.get("candidate_name") or "").strip()
        if not candidate_name:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "candidate_name is required"}).encode())
            return

        json_path = str(DIRECTORY / "weekly_slack_reconciliation.json")
        try:
            opportunities = get_candidate_opportunities(candidate_name, json_path)
            first_name = candidate_name.strip().split()[0]
            message = compose_candidate_message(first_name, opportunities)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "first_name": first_name,
                "message": message,
                "opportunities": opportunities,
            }).encode())
        except Exception as e:
            traceback.print_exc()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_api_candidate_outreach_lookup_email(self):
        """Try to find a candidate's email address from Gmail history."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        candidate_name = (payload.get("candidate_name") or "").strip()
        if not candidate_name:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "candidate_name is required"}).encode())
            return

        try:
            cfg = load_config()
            email = lookup_candidate_email(
                candidate_name,
                cfg.gmail_credentials_path,
                cfg.gmail_token_path,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"email": email}).encode())
        except Exception as e:
            traceback.print_exc()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_api_candidate_outreach_send_email(self):
        """Send an email to a candidate directly via the Gmail API."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        to      = (payload.get("to") or "").strip()
        subject = (payload.get("subject") or "").strip()
        message = (payload.get("message") or "").strip()

        if not to or not message:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "to and message are required"}).encode())
            return

        try:
            cfg = load_config()
            result = send_email_via_gmail(
                to=to,
                subject=subject or "Checking in on your interviews",
                body=message,
                credentials_path=cfg.gmail_credentials_path,
                token_path=cfg.gmail_token_path,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        except Exception as e:
            traceback.print_exc()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())


    def handle_api_ashby_sync(self):
        """Start Ashby extraction + import in a background thread."""
        global ashby_sync_status

        if ashby_sync_status["running"]:
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Sync already in progress"}).encode())
            return

        thread = threading.Thread(target=run_ashby_sync, daemon=True)
        thread.start()

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "started"}).encode())

    def handle_api_ashby_sync_status(self):
        """Return current Ashby sync progress."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(ashby_sync_status).encode())


def run_ashby_sync():
    """Background thread: run Ashby extraction then import fresh data."""
    global ashby_sync_status, generation_status

    ashby_sync_status = {
        "running": True,
        "detail": "Starting Ashby sync...",
        "error": None,
        "completed": False,
        "auth_required": False,
        "imported": 0,
    }

    try:
        cfg = load_config()
        if not cfg.ashby_json_path:
            ashby_sync_status["error"] = "ASHBY_JSON_PATH not set in .env"
            return

        # Step 1: Run extraction
        ashby_sync_status["detail"] = "Running Ashby extraction (this takes ~1-2 min)..."
        success = _run_ashby_extraction(cfg.ashby_json_path)

        if not success:
            ashby_sync_status["auth_required"] = True
            ashby_sync_status["detail"] = "Session expired — paste a fresh Ashby cookie below."
            generation_status["ashby_auth_required"] = True
            return

        # Step 2: Import fresh data
        ashby_sync_status["detail"] = "Importing fresh candidates..."
        ashby_file = find_latest_ashby_export(cfg.ashby_json_path)
        ashby_candidates = load_ashby_export(ashby_file)

        json_path = DIRECTORY / "weekly_slack_reconciliation.json"
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"submissions": [], "generated_at": datetime.now(tz=timezone.utc).isoformat()}

        data["submissions"] = merge_ashby_into_submissions(data.get("submissions", []), ashby_candidates)
        data["ashby_imported_at"] = datetime.now(tz=timezone.utc).isoformat()
        data["ashby_candidate_count"] = len(ashby_candidates)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        ashby_sync_status["imported"] = len(ashby_candidates)
        ashby_sync_status["detail"] = f"Done! Imported {len(ashby_candidates)} candidates."
        ashby_sync_status["completed"] = True
        generation_status["ashby_auth_required"] = False
        print(f"[ASHBY SYNC] Imported {len(ashby_candidates)} candidates from {ashby_file}")

    except Exception as e:
        ashby_sync_status["error"] = str(e)
        ashby_sync_status["detail"] = f"Error: {e}"
        traceback.print_exc()
    finally:
        ashby_sync_status["running"] = False


def _append_status_check_log(draft: dict):
    """Append a sent message record to the audit log file."""
    log_path = DIRECTORY / "status_check_log.json"
    try:
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                log = json.load(f)
        else:
            log = {"entries": []}

        log["entries"].append({
            "sent_at": datetime.now(tz=timezone.utc).isoformat(),
            "client_name": draft.get("client_name"),
            "channel_id": draft.get("channel_id"),
            "channel_name": draft.get("channel_name"),
            "draft_id": draft.get("draft_id"),
            "ts": draft.get("sent_ts"),
        })

        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[STATUS-CHECK] Audit log error: {e}")


def run_status_check_background():
    """Background thread: run the full status-check pipeline."""
    global status_check_status, _status_check_drafts, _slack_instance

    status_check_status = {
        "running": True,
        "phase": "starting",
        "current": 0,
        "total": 0,
        "detail": "Initializing...",
        "error": None,
        "completed": False,
    }

    try:
        cfg = load_config()
        if _slack_instance is None:
            _slack_instance = SlackAPI(token=cfg.slack_bot_token)

        def progress(phase, current, total, detail):
            status_check_status["phase"] = phase
            status_check_status["current"] = current
            status_check_status["total"] = total
            status_check_status["detail"] = detail
            print(f"[STATUS-CHECK] {phase} {current}/{total}: {detail}")

        drafts = run_status_check(cfg, _slack_instance, progress_callback=progress)

        # Convert DraftMessage dataclasses to plain dicts for JSON serialization
        _status_check_drafts = [
            {
                "draft_id": d.draft_id,
                "client_name": d.client_name,
                "channel_id": d.channel_id,
                "channel_name": d.channel_name,
                "message_text": d.message_text,
                "candidates": d.candidates,
                "status": d.status,
            }
            for d in drafts
        ]

        status_check_status["phase"] = "complete"
        status_check_status["completed"] = True
        status_check_status["detail"] = f"Done! {len(drafts)} draft messages ready."

    except Exception as e:
        error_msg = str(e)
        status_check_status["error"] = error_msg
        status_check_status["detail"] = f"Error: {error_msg}"
        print(f"[STATUS-CHECK ERROR] {error_msg}")
        traceback.print_exc()

    finally:
        status_check_status["running"] = False


ASHBY_AUTOMATION_DIR = Path.home() / "Desktop" / "Ashby automation"
def _enrich_with_calendar_events(cfg, json_path: str) -> None:
    """Look up Google Calendar for events matching each candidate + client."""
    import re as _re

    cal = CalendarClient(cfg.gmail_credentials_path, cfg.gcal_token_path)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    submissions = data.get("submissions", [])

    # Build unique (candidate_first_name, client_name) pairs to minimize API calls
    search_cache: dict[tuple[str, str], list] = {}

    for s in submissions:
        candidate_name = s.get("candidate_name", "")
        first_name = candidate_name.split()[0] if candidate_name else ""
        if not first_name:
            continue

        # Derive client name from channel: "candidatelabs-sequence-holdings" -> "sequence holdings"
        channel = s.get("channel_name", "")
        client_name = _re.sub(r"^#?candidatelabs-", "", channel).replace("-", " ")
        if not client_name:
            continue

        cache_key = (first_name.lower(), client_name.lower())
        if cache_key not in search_cache:
            events = cal.search_events(
                candidate_first_name=first_name,
                client_name=client_name,
                lookback_days=cfg.lookback_days,
                lookahead_days=cfg.gcal_lookahead_days,
            )
            search_cache[cache_key] = events

        events = search_cache[cache_key]
        if events:
            best = events[0]  # Already sorted: upcoming first, then most recent
            s["calendar_event_summary"] = best.summary
            s["calendar_event_date"] = best.start_time.isoformat()
            s["calendar_event_is_upcoming"] = best.is_upcoming

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    matched = sum(1 for s in submissions if s.get("calendar_event_date"))
    print(f"[CALENDAR] Matched {matched}/{len(submissions)} submissions to calendar events")


ASHBY_EXTRACT_TIMEOUT = 300  # seconds (5 min) — extraction across all orgs can be slow


def _run_ashby_extraction(ashby_json_path: str) -> bool:
    """
    Run the Ashby Automation Node.js extraction tool to generate a fresh export.
    Uses the saved session in .ashby-session.json (no browser required).

    Returns True if extraction succeeded, False if it failed (e.g. session expired).
    On failure the caller falls back to the last-good JSON file in the output directory.
    """
    if not ASHBY_AUTOMATION_DIR.exists():
        print(f"[ASHBY] Automation directory not found: {ASHBY_AUTOMATION_DIR}")
        return False

    output_dir = Path(ashby_json_path) if Path(ashby_json_path).is_dir() else Path(ashby_json_path).parent
    today = datetime.now().strftime("%Y-%m-%d")
    out_json = output_dir / f"ashby_pipeline_{today}.json"
    out_csv  = output_dir / f"ashby_pipeline_{today}.csv"

    cmd = [
        "node", "--loader", "ts-node/esm",
        "src/cli.ts", "extract",
        "--json", str(out_json),
        "--csv",  str(out_csv),
    ]

    update_progress("Refreshing Ashby data...")
    print(f"[ASHBY] Running extraction: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(ASHBY_AUTOMATION_DIR),
            capture_output=True,
            text=True,
            timeout=ASHBY_EXTRACT_TIMEOUT,
        )
        if result.returncode == 0:
            print(f"[ASHBY] Extraction succeeded → {out_json}")
            generation_status["ashby_auth_required"] = False
            return True
        else:
            print(f"[ASHBY] Extraction failed (exit {result.returncode}) — session likely expired")
            if result.stderr:
                print(f"[ASHBY] stderr: {result.stderr[-500:]}")
            generation_status["ashby_auth_required"] = True
            return False
    except subprocess.TimeoutExpired:
        print(f"[ASHBY] Extraction timed out after {ASHBY_EXTRACT_TIMEOUT}s")
        generation_status["ashby_auth_required"] = True
        return False
    except Exception as e:
        print(f"[ASHBY] Extraction error: {e}")
        generation_status["ashby_auth_required"] = True
        return False


def update_progress(message: str):
    """Update generation progress"""
    generation_status["progress"] = message
    print(f"[PROGRESS] {message}")


def run_generation(settings: dict = None):
    """Run the reconciliation tool and generate JSON"""
    global generation_status
    
    generation_status["running"] = True
    generation_status["progress"] = "Starting..."
    generation_status["error"] = None
    generation_status["completed"] = False
    
    try:
        update_progress("Loading configuration...")
        cfg = load_config()
        
        update_progress("Connecting to Slack...")
        slack = SlackAPI(token=cfg.slack_bot_token)
        
        update_progress("Resolving DK Slack user by email...")
        dk_user_id = slack.get_user_id_by_email(cfg.dk_email)
        
        update_progress("Discovering relevant channels...")
        channels = slack.list_candidate_channels_for_user(dk_user_id)
        
        if not channels:
            generation_status["error"] = "No matching channels found"
            generation_status["running"] = False
            return
        
        update_progress(f"Found {len(channels)} channels. Scanning messages (this may take a few minutes due to rate limits)...")
        
        now = datetime.now(tz=timezone.utc)
        try:
            submissions, stats = build_candidate_submissions(cfg, slack, dk_user_id, channels, now=now)
        except Exception as e:
            # Even if there's an error, try to save partial data if possible
            error_msg = f"Error during scanning: {str(e)}"
            print(f"[ERROR] {error_msg}")
            # Check if we can still get partial results
            generation_status["error"] = error_msg + ". Check terminal for details. Partial data may have been saved."
            generation_status["running"] = False
            return
        
        update_progress(f"Found {len(submissions)} candidate submissions. Generating report...")

        # Write markdown and JSON
        if cfg.output_markdown_path:
            write_markdown(submissions, cfg.output_markdown_path, generated_at=now)
            json_path = cfg.output_markdown_path.replace('.md', '.json')
            write_json(submissions, json_path, generated_at=now)

        # Look up calendar events for each submission
        try:
            update_progress("Checking Google Calendar for upcoming events...")
            json_path = cfg.output_markdown_path.replace('.md', '.json')
            _enrich_with_calendar_events(cfg, json_path)
        except FileNotFoundError:
            print("[CALENDAR] Credentials not found — skipping calendar lookup")
        except Exception as e:
            print(f"[CALENDAR] Calendar lookup failed: {e}")

        # Auto-refresh Ashby data then import
        if cfg.ashby_json_path:
            # Step 1: Re-run the Node.js extraction to get fresh data
            _run_ashby_extraction(cfg.ashby_json_path)
            # Step 2: Import whatever the latest file is (fresh or last-good fallback)
            try:
                update_progress("Importing Ashby candidates...")
                ashby_file = find_latest_ashby_export(cfg.ashby_json_path)
                ashby_candidates = load_ashby_export(ashby_file)
                resolved_json = Path(cfg.output_markdown_path.replace('.md', '.json'))
                with open(resolved_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["submissions"] = merge_ashby_into_submissions(
                    data.get("submissions", []), ashby_candidates
                )
                data["ashby_imported_at"] = datetime.now(tz=timezone.utc).isoformat()
                data["ashby_candidate_count"] = len(ashby_candidates)
                with open(resolved_json, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                print(f"[ASHBY] Imported {len(ashby_candidates)} candidates from {ashby_file}")
            except FileNotFoundError:
                print("[ASHBY] No export file found — skipping import")
            except Exception as e:
                print(f"[ASHBY] Import failed: {e}")

        update_progress("Complete!")
        generation_status["completed"] = True
        generation_status["running"] = False
        
    except Exception as e:
        error_msg = str(e)
        generation_status["error"] = error_msg
        generation_status["running"] = False
        update_progress(f"Error: {error_msg}")
        print(f"[ERROR] {error_msg}")


def run_enrichment(payload: dict = None):
    """Run LLM enrichment on current submissions data."""
    global enrichment_status

    enrichment_status = {
        "running": True,
        "phase": "starting",
        "current": 0,
        "total": 0,
        "detail": "Loading data...",
        "error": None,
        "completed": False,
        "results": None,
    }

    try:
        # Load existing JSON data
        json_path = DIRECTORY / "weekly_slack_reconciliation.json"
        if not json_path.exists():
            enrichment_status["error"] = "No submission data found. Run 'Generate Data' first."
            return

        with open(json_path, "r") as f:
            data = json.load(f)

        submissions_data = data.get("submissions", [])
        if not submissions_data:
            enrichment_status["error"] = "No submissions in data file."
            return

        # Filter candidates if payload specifies which ones
        filter_statuses = (payload or {}).get("statuses")  # e.g. ["IN PROCESS — unclear"]
        filter_channels = (payload or {}).get("channels")  # e.g. ["candidatelabs-argus"]

        # Convert JSON dicts back to CandidateSubmission objects
        # Skip CLOSED candidates — only enrich active ones to save tokens
        filtered_submissions = []
        for s in submissions_data:
            if s["status"] == "CLOSED":
                continue
            if filter_statuses and s["status"] not in filter_statuses:
                continue
            if filter_channels and s["channel_name"] not in filter_channels:
                continue
            submitted_at = datetime.fromisoformat(s["submitted_at"])
            filtered_submissions.append(CandidateSubmission(
                candidate_name=s["candidate_name"],
                linkedin_url=s["linkedin_url"],
                channel_name=s["channel_name"],
                channel_id=s["channel_id"],
                submitted_at=submitted_at,
                status=s["status"],
                status_reason=s.get("status_reason"),
                days_since_submission=s["days_since_submission"],
                needs_followup=s["needs_followup"],
                slack_url=s.get("slack_url"),
            ))

        if not filtered_submissions:
            enrichment_status["error"] = "No submissions match the filter criteria."
            return

        enrichment_status["total"] = len(filtered_submissions)
        enrichment_status["detail"] = f"Enriching {len(filtered_submissions)} candidates..."

        cfg = load_config()
        slack = SlackAPI(token=cfg.slack_bot_token)

        all_results = []

        def progress_callback(phase, current, total, detail):
            enrichment_status["phase"] = phase
            enrichment_status["current"] = current
            enrichment_status["total"] = total
            enrichment_status["detail"] = detail
            print(f"[ENRICH] {phase}: {current}/{total} - {detail}")

        def result_callback(result, index, total):
            """Called after each candidate is enriched — write to JSON immediately."""
            all_results.append(result)
            enrichment_status["current"] = index + 1
            enrichment_status["detail"] = f"{result.candidate_name} done ({index + 1}/{total})"
            # Write this single result to JSON so the dashboard can pick it up
            _merge_enrichment_into_json(json_path, [result])

        results = enrich_submissions(
            cfg, slack, filtered_submissions,
            progress_callback=progress_callback,
            result_callback=result_callback,
        )

        # Store all results
        enrichment_status["results"] = [r.to_dict() for r in results]

        enrichment_status["phase"] = "complete"
        enrichment_status["completed"] = True
        enrichment_status["detail"] = f"Done! Enriched {len(results)} candidates."

    except Exception as e:
        error_msg = str(e)
        enrichment_status["error"] = error_msg
        enrichment_status["detail"] = f"Error: {error_msg}"
        print(f"[ENRICH ERROR] {error_msg}")
        traceback.print_exc()

    finally:
        enrichment_status["running"] = False


def _merge_enrichment_into_json(json_path: Path, results: list):
    """Merge enrichment results back into the submissions JSON file."""
    with open(json_path, "r") as f:
        data = json.load(f)

    # Build lookup: (candidate_name, channel_name) -> result
    result_lookup = {}
    for r in results:
        key = (r.candidate_name, r.channel_name)
        result_lookup[key] = r

    for sub in data.get("submissions", []):
        key = (sub["candidate_name"], sub["channel_name"])
        result = result_lookup.get(key)
        if result:
            sub["ai_summary"] = result.ai_summary
            sub["ai_enriched_at"] = result.enriched_at

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[ENRICH] Merged {len(result_lookup)} enrichment results into {json_path}")


def main():
    os.chdir(DIRECTORY)
    
    # Try to find an available port
    port = PORT
    max_attempts = 10
    httpd = None
    
    for attempt in range(max_attempts):
        try:
            httpd = socketserver.TCPServer(("", port), DashboardRequestHandler)
            break
        except OSError:
            if attempt < max_attempts - 1:
                port += 1
            else:
                print(f"Error: Could not find an available port (tried {PORT}-{port})")
                print("Another instance may already be running. Please close it first.")
                return
    
    url = f"http://localhost:{port}/dashboard.html"
    print(f"Dashboard server starting on port {port}...")
    print(f"Opening dashboard at: {url}")
    print(f"Press Ctrl+C to stop the server")
    print()
    
    # Open browser
    webbrowser.open(url)
    
    # Start server
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\nServer stopped.")
    finally:
        if httpd:
            httpd.shutdown()


if __name__ == "__main__":
    main()
