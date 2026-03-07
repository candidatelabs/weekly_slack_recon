#!/usr/bin/env python3
"""
Simple GUI for Weekly Slack Pipeline Reconciliation Tool
"""
import os
import sys
import threading
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone
from tkinter import (
    Tk, ttk, StringVar, IntVar, BooleanVar, Text, scrolledtext,
    messagebox, filedialog
)
import tkinter as tk

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv
from weekly_slack_recon.config import Config
from weekly_slack_recon.slack_client import SlackAPI
from weekly_slack_recon.logic import build_candidate_submissions
from weekly_slack_recon.reporting import write_markdown, write_json, generate_followup_snippets

# Load .env for defaults
load_dotenv()


class ReconciliationGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Weekly Slack Pipeline Reconciliation")
        self.root.geometry("700x650")
        self.root.resizable(True, True)
        
        # Configuration variables
        self.lookback_days = IntVar(value=int(os.getenv("LOOKBACK_DAYS", "60")))
        self.unclear_followup_days = IntVar(value=int(os.getenv("UNCLEAR_FOLLOWUP_DAYS", "7")))
        self.inactivity_days = IntVar(value=int(os.getenv("INACTIVITY_DAYS", "5")))
        self.include_confused_close = BooleanVar(value=os.getenv("INCLUDE_CONFUSED_CLOSE", "false").lower() in {"1", "true", "yes", "y"})
        self.dk_email = StringVar(value=os.getenv("DK_EMAIL", "dkimball@candidatelabs.com"))
        self.slack_token = StringVar(value=os.getenv("SLACK_BOT_TOKEN", ""))
        
        self.is_running = False
        self.setup_ui()
        
    def setup_ui(self):
        # Main frame with padding
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        
        # Title
        title_label = ttk.Label(main_frame, text="Weekly Slack Pipeline Reconciliation", 
                               font=("Helvetica", 16, "bold"))
        title_label.grid(row=0, column=0, columnspan=2, pady=(0, 20))
        
        # Configuration section
        config_frame = ttk.LabelFrame(main_frame, text="Configuration", padding="10")
        config_frame.grid(row=1, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10))
        config_frame.columnconfigure(1, weight=1)
        
        # Lookback days
        ttk.Label(config_frame, text="Lookback Days:").grid(row=0, column=0, sticky=tk.W, pady=5)
        lookback_spin = ttk.Spinbox(config_frame, from_=1, to=365, width=10, 
                                    textvariable=self.lookback_days)
        lookback_spin.grid(row=0, column=1, sticky=tk.W, padx=(10, 0), pady=5)
        ttk.Label(config_frame, text="How far back to scan for submissions (days)", 
                 foreground="gray").grid(row=0, column=2, sticky=tk.W, padx=(10, 0))
        
        # Unclear followup days
        ttk.Label(config_frame, text="Unclear Followup Days:").grid(row=1, column=0, sticky=tk.W, pady=5)
        unclear_spin = ttk.Spinbox(config_frame, from_=1, to=365, width=10,
                                   textvariable=self.unclear_followup_days)
        unclear_spin.grid(row=1, column=1, sticky=tk.W, padx=(10, 0), pady=5)
        ttk.Label(config_frame, text="Min days for follow-up flagging", 
                 foreground="gray").grid(row=1, column=2, sticky=tk.W, padx=(10, 0))
        
        # Inactivity days
        ttk.Label(config_frame, text="Inactivity Days:").grid(row=2, column=0, sticky=tk.W, pady=5)
        inactivity_spin = ttk.Spinbox(config_frame, from_=1, to=365, width=10,
                                      textvariable=self.inactivity_days)
        inactivity_spin.grid(row=2, column=1, sticky=tk.W, padx=(10, 0), pady=5)
        ttk.Label(config_frame, text="No activity threshold (days)", 
                 foreground="gray").grid(row=2, column=2, sticky=tk.W, padx=(10, 0))
        
        # Include confused close
        confused_check = ttk.Checkbutton(config_frame, text="Include Confused Close",
                                        variable=self.include_confused_close)
        confused_check.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=5)
        
        # DK Email
        ttk.Label(config_frame, text="DK Email:").grid(row=4, column=0, sticky=tk.W, pady=5)
        email_entry = ttk.Entry(config_frame, textvariable=self.dk_email, width=40)
        email_entry.grid(row=4, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=5)
        
        # Slack Token (optional override)
        ttk.Label(config_frame, text="Slack Token:").grid(row=5, column=0, sticky=tk.W, pady=5)
        token_entry = ttk.Entry(config_frame, textvariable=self.slack_token, width=40, show="*")
        token_entry.grid(row=5, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=5)
        ttk.Label(config_frame, text="(Optional - uses .env if empty)", 
                 foreground="gray").grid(row=5, column=2, sticky=tk.W, padx=(10, 0))
        
        # Run button
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=2, column=0, columnspan=2, pady=10)
        
        self.run_button = ttk.Button(button_frame, text="Run Reconciliation", 
                                     command=self.run_reconciliation, width=20)
        self.run_button.pack(side=tk.LEFT, padx=5)
        
        self.open_output_button = ttk.Button(button_frame, text="Open Output File", 
                                            command=self.open_output_file, width=20)
        self.open_output_button.pack(side=tk.LEFT, padx=5)
        
        # Status/Output text area
        output_frame = ttk.LabelFrame(main_frame, text="Output", padding="10")
        output_frame.grid(row=3, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(10, 0))
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(3, weight=1)
        
        self.output_text = scrolledtext.ScrolledText(output_frame, height=15, wrap=tk.WORD,
                                                     font=("Monaco", 10))
        self.output_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Progress bar
        self.progress = ttk.Progressbar(main_frame, mode='indeterminate')
        self.progress.grid(row=4, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(10, 0))
        
    def log(self, message):
        """Append message to output text area"""
        self.output_text.insert(tk.END, message + "\n")
        self.output_text.see(tk.END)
        self.root.update_idletasks()
        
    def run_reconciliation(self):
        """Run the reconciliation in a separate thread"""
        if self.is_running:
            messagebox.showwarning("Already Running", "Reconciliation is already running. Please wait.")
            return
            
        # Get token - use entry if provided, otherwise try .env
        token = self.slack_token.get().strip()
        if not token:
            load_dotenv()
            token = os.getenv("SLACK_BOT_TOKEN", "").strip()
            
        if not token:
            messagebox.showerror("Missing Token", 
                               "Slack token is required. Please enter a token or set SLACK_BOT_TOKEN in .env file.")
            return
        
        # Clear output
        self.output_text.delete(1.0, tk.END)
        self.is_running = True
        self.run_button.config(state="disabled")
        self.progress.start()
        
        # Run in separate thread to keep UI responsive
        thread = threading.Thread(target=self._run_reconciliation_thread, args=(token,), daemon=True)
        thread.start()
        
    def _run_reconciliation_thread(self, token):
        """Run reconciliation in background thread"""
        try:
            self.log(f"Starting reconciliation at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            self.log(f"Configuration:")
            self.log(f"  Lookback Days: {self.lookback_days.get()}")
            self.log(f"  Unclear Followup Days: {self.unclear_followup_days.get()}")
            self.log(f"  Inactivity Days: {self.inactivity_days.get()}")
            self.log(f"  Include Confused Close: {self.include_confused_close.get()}")
            self.log(f"  DK Email: {self.dk_email.get()}")
            self.log("")
            
            # Create config
            cfg = Config(
                slack_bot_token=token,
                dk_email=self.dk_email.get(),
                lookback_days=self.lookback_days.get(),
                unclear_followup_days=self.unclear_followup_days.get(),
                inactivity_days=self.inactivity_days.get(),
                include_confused_close=self.include_confused_close.get(),
                output_markdown_path="weekly_slack_reconciliation.md",
            )
            
            slack = SlackAPI(token=cfg.slack_bot_token)
            
            self.log("Resolving DK Slack user by email...")
            dk_user_id = slack.get_user_id_by_email(cfg.dk_email)
            self.log(f"Found DK user ID: {dk_user_id}")
            
            self.log("Discovering relevant channels (prefix 'candidatelabs-' and DK is member)...")
            channels = slack.list_candidate_channels_for_user(dk_user_id)
            
            if not channels:
                self.log("ERROR: No matching channels found.")
                messagebox.showwarning("No Channels", "No matching channels found.")
                return
            
            self.log(f"Found {len(channels)} matching channel(s)")
            for ch in channels[:10]:
                self.log(f"  - {ch.get('name', ch.get('id', 'unknown'))}")
            if len(channels) > 10:
                self.log(f"  ... and {len(channels) - 10} more")
            self.log("")
            
            now = datetime.now(tz=timezone.utc)
            self.log(f"Scanning messages from the last {cfg.lookback_days} days...")
            
            submissions, stats = build_candidate_submissions(cfg, slack, dk_user_id, channels, now=now)
            
            self.log(f"\nFound {len(submissions)} candidate submission(s)")
            self.log(f"Stats: {stats['total_messages']} total messages, "
                    f"{stats['top_level_messages']} top-level, "
                    f"{stats['dk_messages']} from DK, "
                    f"{stats['messages_with_linkedin']} with LinkedIn URLs")
            self.log("")
            
            # Group by status
            by_status = defaultdict(list)
            for s in submissions:
                by_status[s.status].append(s)
            
            # Print summary
            for status in ["CLOSED", "IN PROCESS — explicit", "IN PROCESS — unclear"]:
                if status in by_status:
                    self.log(f"{status}: {len(by_status[status])} candidates")
                    if status == "IN PROCESS — unclear":
                        needs_followup = sum(1 for s in by_status[status] if s.needs_followup)
                        if needs_followup > 0:
                            self.log(f"  ({needs_followup} need follow-up)")
            
            # Write markdown and JSON files
            if cfg.output_markdown_path:
                write_markdown(submissions, cfg.output_markdown_path, generated_at=now)
                output_path = Path(cfg.output_markdown_path).absolute()
                self.log(f"\n✓ Markdown report written to: {output_path}")
                
                # Also write JSON for dashboard
                json_path = cfg.output_markdown_path.replace('.md', '.json')
                write_json(submissions, json_path, generated_at=now)
                json_output_path = Path(json_path).absolute()
                self.log(f"✓ JSON data written to: {json_output_path} (for dashboard)")
            
            # Generate follow-up snippets
            snippets = generate_followup_snippets(submissions)
            if snippets:
                self.log(f"\nSuggested follow-up messages for {len(snippets)} channel(s)")
            
            self.log("\n✓ Reconciliation completed successfully!")
            messagebox.showinfo("Success", 
                              f"Reconciliation completed!\n\n"
                              f"Found {len(submissions)} candidate submissions.\n"
                              f"Report saved to: {cfg.output_markdown_path}")
            
        except Exception as e:
            error_msg = str(e)
            self.log(f"\n✗ ERROR: {error_msg}")
            messagebox.showerror("Error", f"An error occurred:\n\n{error_msg}")
        finally:
            self.is_running = False
            self.progress.stop()
            self.run_button.config(state="normal")
            
    def open_output_file(self):
        """Open the output markdown file"""
        output_path = Path("weekly_slack_reconciliation.md")
        if output_path.exists():
            import subprocess
            import platform
            if platform.system() == "Darwin":  # macOS
                subprocess.run(["open", str(output_path)])
            elif platform.system() == "Windows":
                os.startfile(str(output_path))
            else:  # Linux
                subprocess.run(["xdg-open", str(output_path)])
        else:
            messagebox.showinfo("File Not Found", 
                              "Output file not found. Please run reconciliation first.")


def main():
    root = Tk()
    app = ReconciliationGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

