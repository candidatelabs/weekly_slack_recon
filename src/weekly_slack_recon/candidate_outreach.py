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
#
# Candidate email resolution.
#
# The old lookup ran `from:"First Last"` and then fell back to `from:"First"`,
# returning the sender of the FIRST hit. The first-name fallback routinely
# matched a Candidate Labs colleague or a client contact (e.g. "DJ (Krishnamurthy)
# Dvijotham" → `from:"DJ"` → djdeanda@candidatelabs.com), and the address then
# flowed into the Add-to-Ashby modal — i.e. into a client's ATS. The resolver
# below is surname-anchored, reads DK's *sent* mail and Calendly notices as
# evidence, never runs a first-name-only query, excludes internal/system
# senders, and returns a confidence so callers can gate on it.

_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

_DEFAULT_INTERNAL_DOMAINS = ("candidatelabs.com",)
_DEFAULT_OWN_ADDRESSES = ("dkimball@candidatelabs.com",)

# Senders that are never a candidate's personal address.
_SYSTEM_DOMAIN_SUFFIXES = (
    "superhuman.com", "zoom.us", "metaview.ai", "calendly.com", "google.com",
    "linkedin.com", "ashbyhq.com", "greenhouse.io", "greenhouse-mail.io", "lever.co",
    "slack.com", "granola.ai", "fathom.video", "cal.com", "goodtime.io",
    "docusign.net", "docusign.com", "sendgrid.net", "mailchimp.com", "mailgun.org",
    "amazonses.com", "intercom-mail.com", "zoominfo.com", "apollo.io", "gem.com",
    "wellfound.com", "hired.com", "indeed.com", "glassdoor.com", "workable.com",
    "bamboohr.com", "hirevue.com", "codesignal.com", "hackerrank.com",
)
_SYSTEM_LOCAL_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply",
    "notifications", "notification", "reminder", "reminders", "calendar-notification",
    "mailer-daemon", "postmaster", "bounce", "bounces", "alerts", "alert", "digest",
    "newsletter", "news", "marketing", "updates", "invitations", "invites", "scheduling",
    "calendar", "meetings", "system", "robot", "bot", "auto", "automated",
)
_SYSTEM_LOCAL_EXACT = frozenset({
    "support", "hello", "info", "team", "sales", "careers", "jobs", "recruiting",
    "talent", "hr", "hiring", "people", "admin", "billing", "contact", "help",
    "security", "privacy", "legal", "press", "media", "events", "feedback",
})
# Senders whose notices mean "a conversation with this person was scheduled"
# (supporting evidence only — they never yield an address by themselves).
_SCHEDULING_NOTICE_DOMAINS = (
    "zoom.us", "metaview.ai", "calendly.com", "fathom.video", "granola.ai",
    "cal.com", "goodtime.io",
)
_CALENDLY_DOMAINS = ("calendly.com",)

_PERSONAL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "rocketmail.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "protonmail.ch", "pm.me",
    "fastmail.com", "fastmail.fm", "hey.com", "aol.com", "zoho.com", "gmx.com",
    "gmx.net", "duck.com", "tutanota.com", "tuta.io", "mail.com", "posteo.de",
    "qq.com", "163.com", "126.com", "naver.com", "yandex.com",
})
_PERSONAL_DOMAIN_PREFIXES = ("proton", "yahoo.", "outlook.", "hotmail.", "live.", "gmx.")

# Surnames that are also ordinary English words — a bare `"Jolly"` body query
# would be mostly noise, so those names only get the tighter "First Last" phrase
# queries. Not exhaustive; it's a guard, not a dictionary.
_COMMON_WORD_SURNAMES = frozenset("""
white black brown green gray grey king young long hill wood stone bell rose cook
baker hall ward price love best good may day week miller summer winter north south
east west little small bush lane park field moon star sun rice fox wolf bird fish
bank book case hope grace joy frost snow rain storm reed read chase gold silver steel
ford strong sharp wise noble power bright free dear fair lord marks mark page post ray
ross jolly happy merry swift quick church castle bridge river lake forest glass
bishop knight hunter fisher carpenter mason taylor smith cooper parker turner walker
butler brewer farmer shepherd singer archer dean judge marshall page sergeant
hart heart dove crane hawk swan drake bull lamb fowler mills wells brooks banks
woods fields burns ball bond cross march mayo money morning night noon oak olive
pepper pitt pool rush sands short stern still sweet thorn town tree wall waters wild
well west winter world rich poor old new grand great high low light dark early late
true just right left last first second gates gate gay done real royal royale
""".split())

_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "phd", "md", "mba", "esq"})


def _ascii_fold(text: str) -> str:
    """Lowercase + strip diacritics ("José" → "jose")."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).lower()


def _name_tokens(text: str) -> list[str]:
    """Alphanumeric-only lowercase tokens of a display name or free text."""
    return re.findall(r"[a-z0-9]+", _ascii_fold(text))


def parse_candidate_name(raw: str) -> dict:
    """
    Split a candidate display name into first-name variants + surname.

    "DJ (Krishnamurthy) Dvijotham" → firsts ["dj", "krishnamurthy"], surname "dvijotham"
    "Zoe (Zhaoyuan) Xi"            → firsts ["zoe", "zhaoyuan"],     surname "xi"
    "Aayush Gupta –"                → firsts ["aayush"],              surname "gupta"
    "Madonna"                       → firsts [],  surname "madonna", single_token True

    Returns dict(firsts, surname, single_token, display_firsts, display_surname,
    searchable_surname) — `searchable_surname` is False when the surname is too
    short (< 4 chars) or a common English word, i.e. a bare surname query would
    be noise.
    """
    s = (raw or "").strip()
    # Parenthesised nickname / formal name: "(Krishnamurthy)", "(Robert)".
    parens = re.findall(r"\(([^)]*)\)", s)
    s = re.sub(r"\([^)]*\)", " ", s)
    # Quoted nickname: Robert "Bob" Smith
    quoted = re.findall(r'["“”]([^"“”]+)["“”]', s)
    s = re.sub(r'["“”][^"“”]+["“”]', " ", s)

    tokens: list[str] = []
    display_tokens: list[str] = []
    for t in re.split(r"\s+", s):
        t = t.strip(" \t–—-,|;:./\\")
        if not t or not re.search(r"[A-Za-z]", t):
            continue
        folded = "".join(_name_tokens(t))
        if not folded or folded in _NAME_SUFFIXES:
            continue
        tokens.append(folded)
        display_tokens.append(t)

    firsts: list[str] = []
    display_firsts: list[str] = []
    surname = ""
    display_surname = ""
    single_token = False
    if len(tokens) >= 2:
        firsts.append(tokens[0])
        display_firsts.append(display_tokens[0])
        surname = tokens[-1]
        display_surname = display_tokens[-1]
    elif len(tokens) == 1:
        surname = tokens[0]
        display_surname = display_tokens[0]
        single_token = True

    for extra in list(parens) + list(quoted):
        extra_tokens = [t for t in re.split(r"\s+", extra.strip()) if re.search(r"[A-Za-z]", t)]
        # Only single-word, purely alphabetic parentheticals are treated as a name
        # variant — "(he/him)", "(Staff Eng, ex-Stripe)", "(2x)" etc. are ignored.
        if len(extra_tokens) != 1 or not re.fullmatch(r"[A-Za-z\u00C0-\u024F][A-Za-z\u00C0-\u024F'\-]*\.?", extra_tokens[0]):
            continue
        folded = "".join(_name_tokens(extra_tokens[0]))
        if not folded or folded in firsts or folded == surname or len(folded) < 2:
            continue
        firsts.append(folded)
        display_firsts.append(extra_tokens[0].strip(" ,.-–—"))

    searchable_surname = bool(surname) and len(surname) >= 4 and surname not in _COMMON_WORD_SURNAMES
    return {
        "firsts": firsts,
        "surname": surname,
        "single_token": single_token,
        "display_firsts": display_firsts,
        "display_surname": display_surname,
        "searchable_surname": searchable_surname,
    }


def _split_address(value: str) -> tuple[str, str]:
    """'Name <a@b.c>' → ('Name', 'a@b.c'); bare address → ('', 'a@b.c')."""
    from email.utils import parseaddr
    name, addr = parseaddr(value or "")
    addr = (addr or "").strip().strip("<>").lower()
    if "@" not in addr:
        m = re.search(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+", value or "")
        addr = m.group(0).lower() if m else ""
    return (name or "").strip().strip('"'), addr


def _addresses_in(header_value: str) -> list[tuple[str, str]]:
    from email.utils import getaddresses
    out = []
    for name, addr in getaddresses([header_value or ""]):
        addr = (addr or "").strip().lower()
        if "@" in addr:
            out.append(((name or "").strip().strip('"'), addr))
    return out


def _domain_of(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def _is_internal_address(addr: str, internal_domains, own_addresses) -> bool:
    if not addr:
        return True
    if addr in own_addresses:
        return True
    dom = _domain_of(addr)
    return any(dom == d or dom.endswith("." + d) for d in internal_domains)


def _is_system_address(addr: str) -> bool:
    if not addr or "@" not in addr:
        return True
    local, dom = addr.rsplit("@", 1)
    local = local.lower()
    dom = dom.lower()
    if any(dom == d or dom.endswith("." + d) for d in _SYSTEM_DOMAIN_SUFFIXES):
        return True
    if local in _SYSTEM_LOCAL_EXACT:
        return True
    if any(local.startswith(p) and not local[len(p):len(p) + 1].isalpha() for p in _SYSTEM_LOCAL_PREFIXES):
        return True
    if "noreply" in local or "no-reply" in local or "donotreply" in local:
        return True
    return False


def _is_scheduling_notice_sender(addr: str) -> bool:
    dom = _domain_of(addr)
    return any(dom == d or dom.endswith("." + d) for d in _SCHEDULING_NOTICE_DOMAINS)


def _is_calendly_sender(addr: str) -> bool:
    dom = _domain_of(addr)
    return any(dom == d or dom.endswith("." + d) for d in _CALENDLY_DOMAINS)


def _is_personal_domain(addr: str) -> bool:
    dom = _domain_of(addr)
    if not dom:
        return False
    if dom in _PERSONAL_DOMAINS or dom.endswith(".edu") or ".edu." in dom:
        return True
    return any(dom.startswith(p) for p in _PERSONAL_DOMAIN_PREFIXES)


def _token_matches(token: str, needle: str) -> bool:
    """Exact token match, or ≥3-char prefix either way ("dan" ~ "daniel")."""
    if not token or not needle:
        return False
    if token == needle:
        return True
    if len(needle) >= 3 and len(token) >= 3:
        return token.startswith(needle) or needle.startswith(token)
    return False


def _local_contains(local: str, needle: str) -> bool:
    """Needle inside the address local-part; short needles must sit on a boundary."""
    if not needle:
        return False
    local_alnum = re.sub(r"[^a-z0-9]", "", local.lower())
    if len(needle) >= 3:
        return needle in local_alnum
    # 2-char surnames ("xi", "hu", "li"): only at a separator boundary or an end.
    return bool(re.search(r"(^|[._\-])" + re.escape(needle) + r"($|[._\-])", local.lower())
                or local_alnum.startswith(needle) or local_alnum.endswith(needle))


def _match_name(display: str, addr: str, parsed: dict) -> dict:
    """
    How well does (display name, address) match the candidate's name?
    Returns {surname: bool, first: bool, conflict: bool}.
    """
    surname = parsed["surname"]
    firsts = parsed["firsts"]
    display_tokens = _name_tokens(display)
    local = addr.split("@", 1)[0] if "@" in addr else addr

    surname_hit = False
    surname_display = False
    if surname:
        if any(t == surname for t in display_tokens):
            surname_hit = surname_display = True
        elif len(surname) >= 6 and surname in "".join(display_tokens):
            surname_hit = surname_display = True   # hyphenated / run-together surnames ("Smith-Jones")
        elif _local_contains(local, surname):
            surname_hit = True

    first_hit = False
    first_weak = False   # initial only ("A Gupta") — neither a hit nor a conflict
    for f in firsts:
        if any(_token_matches(t, f) for t in display_tokens):
            first_hit = True
            break
        if len(f) >= 3 and _local_contains(local, f):
            first_hit = True
            break
        if len(f) == 2 and any(t == f for t in display_tokens):
            first_hit = True
            break
        if any(len(t) == 1 and t == f[0] for t in display_tokens):
            first_weak = True

    # Conflict: display name clearly names someone else with the same surname
    # ("Rahul Gupta" when we're looking for Aayush Gupta).
    conflict = False
    if surname_hit and firsts and not first_hit and not first_weak and len(display_tokens) >= 2:
        others = [t for t in display_tokens if t != surname]
        if others and all(len(t) >= 2 for t in others):
            conflict = True
    return {"surname": surname_hit, "surname_display": surname_display, "first": first_hit, "conflict": conflict}


def _surname_in_text(text: str, parsed: dict) -> bool:
    surname = parsed["surname"]
    if not surname or len(surname) < 3:
        # Very short surnames ("Xi") need the full "First Last" phrase in the text.
        return bool(surname) and any(
            re.search(r"\b" + re.escape(f) + r"\s+" + re.escape(surname) + r"\b", _ascii_fold(text))
            for f in parsed["firsts"]
        )
    return bool(re.search(r"\b" + re.escape(surname) + r"\b", _ascii_fold(text)))


def _build_queries(parsed: dict, *, internal_domains, lookback: str = "18m") -> list[dict]:
    """
    Ordered Gmail queries. Each is {"q": str, "anchor": bool} — `anchor=True`
    means the query itself required the surname, so any message it returns
    co-occurs with the surname even if the address lacks it.

    Invariant: no first-name-only query, ever.
    """
    firsts = parsed["display_firsts"]
    surname = parsed["display_surname"]
    queries: list[dict] = []
    exclude = " ".join(f"-from:{d}" for d in internal_domains)

    if parsed["single_token"]:
        if len(parsed["surname"]) >= 4:
            queries.append({"q": f'from:"{surname}" OR to:"{surname}" OR cc:"{surname}"', "anchor": True})
            if parsed["searchable_surname"]:
                queries.append({"q": f'from:me "{surname}" newer_than:{lookback}', "anchor": True})
                queries.append({"q": f'"{surname}" {exclude} newer_than:{lookback}'.strip(), "anchor": True})
        return queries

    phrases = [f"{f} {surname}" for f in firsts][:3]
    # 1. Header matches on the full name (display names in From/To/Cc).
    for p in phrases:
        queries.append({"q": f'from:"{p}" OR to:"{p}" OR cc:"{p}"', "anchor": True})
    # 2. Surname-anchored: DK's sent mail + external mentions (replies, Calendly, notices).
    if parsed["searchable_surname"]:
        queries.append({"q": f'from:me "{surname}" newer_than:{lookback}', "anchor": True})
        queries.append({"q": f'"{surname}" {exclude} newer_than:{lookback}'.strip(), "anchor": True})
    else:
        # Short or common-word surname: require the full phrase anywhere instead.
        for p in phrases:
            queries.append({"q": f'"{p}" newer_than:{lookback}', "anchor": True})
    return queries


def _parse_msg_date(msg: dict) -> str:
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone
    headers = {h.get("name", "").lower(): h.get("value", "") for h in msg.get("payload", {}).get("headers", [])}
    raw = headers.get("date")
    if raw:
        try:
            return parsedate_to_datetime(raw).date().isoformat()
        except Exception:
            pass
    try:
        ms = int(msg.get("internalDate", 0))
        if ms:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
    except Exception:
        pass
    return ""


def _body_text(msg: dict) -> str:
    """Best-effort plain text from a format=full message payload."""
    import html as _html
    out: list[str] = []

    def walk(part):
        if not isinstance(part, dict):
            return
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime.startswith("text/"):
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            except Exception:
                decoded = ""
            if mime == "text/html":
                decoded = re.sub(r"<[^>]+>", " ", decoded)
                decoded = _html.unescape(decoded)
            out.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(msg.get("payload") or {})
    return re.sub(r"\s+", " ", " ".join(out))


_CALENDLY_INVITEE_RE = re.compile(
    r"Invitee:\s*(?P<name>.*?)\s*Invitee\s*Email:\s*(?P<email>[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+)",
    re.I | re.S,
)
_ANY_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


def score_candidate_messages(
    messages: list[dict],
    parsed: dict,
    *,
    internal_domains=_DEFAULT_INTERNAL_DOMAINS,
    own_addresses=_DEFAULT_OWN_ADDRESSES,
) -> dict:
    """
    Pure scoring step (no network) so it can be unit-tested with fake messages.

    `messages` items: {"id", "threadId", "headers": {From, To, Cc, Subject, Date},
    "snippet", "date": "YYYY-MM-DD", "anchor": bool, "body": optional str}.

    Returns {"candidates": [...sorted desc...], "supporting": [...]} where each
    candidate is {email, score, confidence, surname_matched, first_matched,
    bidirectional, threads, evidence}.
    """
    import html as _html

    internal_domains = tuple(d.lower() for d in internal_domains)
    own_addresses = tuple(a.lower() for a in own_addresses)

    by_addr: dict[str, dict] = {}
    supporting: list[dict] = []

    def rec(addr: str) -> dict:
        return by_addr.setdefault(addr, {
            "email": addr, "display_names": set(), "surname": False, "surname_display": False,
            "first": False, "conflict": False, "cooccur": False, "you_emailed": False, "they_emailed": False,
            "calendly": False, "mention": False, "threads": set(), "evidence": [],
        })

    def note(addr: str, display: str, kind: str, msg: dict, detail: str, *, msg_has_surname: bool):
        if not addr or "@" not in addr:
            return
        if _is_internal_address(addr, internal_domains, own_addresses) or _is_system_address(addr):
            return
        r = rec(addr)
        m = _match_name(display, addr, parsed)
        if display:
            r["display_names"].add(display)
        if m["surname"]:
            r["surname"] = True
        if m["surname_display"]:
            r["surname_display"] = True
        if m["first"]:
            r["first"] = True
            if msg_has_surname:
                r["cooccur"] = True
        if m["conflict"]:
            r["conflict"] = True
        r[{"you_emailed": "you_emailed", "they_emailed": "they_emailed",
           "calendly_invitee": "calendly", "mention": "mention"}[kind]] = True
        if msg.get("threadId"):
            r["threads"].add(msg["threadId"])
        r["evidence"].append({
            "kind": kind,
            "subject": (msg.get("headers") or {}).get("Subject", "") or "",
            "date": msg.get("date", "") or "",
            "detail": detail,
        })

    for msg in messages:
        headers = msg.get("headers") or {}
        from_display, from_addr = _split_address(headers.get("From", ""))
        subject = headers.get("Subject", "") or ""
        snippet = _html.unescape(msg.get("snippet", "") or "")
        body = msg.get("body", "") or ""
        text_blob = " ".join([subject, snippet, body, headers.get("From", ""), headers.get("To", ""), headers.get("Cc", "")])
        msg_has_surname = bool(msg.get("anchor")) or _surname_in_text(text_blob, parsed)
        recipients = _addresses_in(headers.get("To", "")) + _addresses_in(headers.get("Cc", ""))

        if _is_internal_address(from_addr, internal_domains, own_addresses):
            # DK sent it — the candidate is a recipient. A colleague's mail that DK
            # was copied on only counts as a mention.
            if from_addr in own_addresses:
                kind, detail = "you_emailed", "You emailed them"
            else:
                kind, detail = "mention", f"Emailed by {from_addr}"
            for disp, addr in recipients:
                note(addr, disp, kind, msg, detail, msg_has_surname=msg_has_surname)
            continue

        if _is_calendly_sender(from_addr):
            text = snippet + " " + body
            m = _CALENDLY_INVITEE_RE.search(text)
            if m:
                invitee_name = re.sub(r"\s+", " ", m.group("name")).strip()
                invitee_email = m.group("email").lower()
                note(invitee_email, invitee_name, "calendly_invitee", msg,
                     f"Calendly invitee: {invitee_name}", msg_has_surname=msg_has_surname)
            else:
                found = [e.lower() for e in _ANY_EMAIL_RE.findall(text)]
                found = [e for e in found if not _is_system_address(e)
                         and not _is_internal_address(e, internal_domains, own_addresses)]
                if len(found) == 1:
                    # Name context lives in the snippet — let the snippet act as the display name.
                    note(found[0], snippet[:200], "calendly_invitee", msg, "Calendly invitee",
                         msg_has_surname=msg_has_surname)
                else:
                    supporting.append({"kind": "scheduling_notice", "subject": subject,
                                       "date": msg.get("date", ""), "detail": "Calendly notice"})
            continue

        if _is_scheduling_notice_sender(from_addr) or _is_system_address(from_addr):
            if _is_scheduling_notice_sender(from_addr) and (
                _surname_in_text(text_blob, parsed) or msg.get("anchor")
            ):
                supporting.append({
                    "kind": "scheduling_notice", "subject": subject,
                    "date": msg.get("date", ""), "detail": f"{_domain_of(from_addr)} notice",
                })
            continue

        # External human sender.
        note(from_addr, from_display, "they_emailed", msg, "They emailed you", msg_has_surname=msg_has_surname)
        for disp, addr in recipients:
            if addr != from_addr:
                note(addr, disp, "mention", msg, "Copied on a thread", msg_has_surname=msg_has_surname)

    # ── Score ──
    out: list[dict] = []
    for addr, r in by_addr.items():
        surname_matched = r["surname"] or (r["first"] and r["cooccur"])
        if not r["surname"] and not r["first"]:
            continue   # no name evidence at all → drop
        score = 0.0
        if r["surname"]:
            score += 3
        elif surname_matched:
            score += 2   # surname co-occurred in the message, address carries the first name
        if r["first"]:
            score += 2
        if r["you_emailed"]:
            score += 2
        if r["they_emailed"]:
            score += 2
        if r["calendly"]:
            score += 2
        threads = len(r["threads"])
        if threads > 1:
            score += min(3, threads - 1)
        if _is_personal_domain(addr):
            score += 1
        if r["conflict"]:
            score -= 3
        if score <= 0:
            continue
        bidirectional = r["you_emailed"] and r["they_emailed"]
        # Identity gate: a surname that only appears inside the local-part
        # ("arul18.gupta@") with no first-name signal is "some Gupta", not this
        # one — such addresses never rise above low.
        identity_ok = (r["first"] or r["surname_display"] or r["calendly"]) and not r["conflict"]

        if surname_matched and identity_ok and (bidirectional or threads >= 2 or (r["calendly"] and r["you_emailed"])) and score >= 6:
            confidence = "high"
        elif surname_matched and identity_ok and score >= 4:
            confidence = "medium"
        else:
            confidence = "low"
        if parsed.get("single_token") and confidence == "high":
            confidence = "medium"

        # Evidence: one line per (kind, subject), newest first within a kind,
        # round-robin across kinds so the list shows variety ("they emailed",
        # "you emailed", "Calendly invitee") instead of four replies in a row.
        kind_rank = {"they_emailed": 0, "you_emailed": 1, "calendly_invitee": 2, "mention": 3, "scheduling_notice": 4}
        buckets: dict[str, list] = {}
        seen = set()
        for e in sorted(r["evidence"], key=lambda e: e["date"] or "", reverse=True):
            key = (e["kind"], e["subject"])
            if key in seen:
                continue
            seen.add(key)
            buckets.setdefault(e["kind"], []).append(e)
        evidence = []
        ordered_kinds = sorted(buckets, key=lambda k: kind_rank.get(k, 9))
        while any(buckets.values()):
            for k in ordered_kinds:
                if buckets[k]:
                    evidence.append(buckets[k].pop(0))
        out.append({
            "email": addr,
            "score": score,
            "confidence": confidence,
            "surname_matched": bool(surname_matched),
            "first_matched": bool(r["first"]),
            "bidirectional": bool(bidirectional),
            "threads": threads,
            "display_names": sorted(r["display_names"])[:3],
            "evidence": evidence,
        })

    conf_rank = {"high": 3, "medium": 2, "low": 1}
    out.sort(key=lambda c: (conf_rank[c["confidence"]], c["score"], c["threads"]), reverse=True)
    supporting.sort(key=lambda e: e.get("date", ""), reverse=True)
    return {"candidates": out, "supporting": supporting}


def resolve_candidate_email(
    candidate_name: str,
    credentials_path: str,
    token_path: str,
    *,
    context: Optional[dict] = None,
    internal_domains=_DEFAULT_INTERNAL_DOMAINS,
    own_addresses=_DEFAULT_OWN_ADDRESSES,
    max_results_per_query: int = 15,
    max_message_gets: int = 60,
) -> dict:
    """
    Resolve a candidate's personal email from DK's Gmail with a confidence.

    Surname-anchored, evidence-based: reads DK's sent mail (To/Cc), the
    candidate's own messages (From), Calendly invitee notices, and Zoom/Metaview
    scheduling notices. Never runs a first-name-only query; never returns an
    @candidatelabs.com / DK / system address.

    Returns:
        {
          "email": str|None,          # top candidate ONLY when confidence is high|medium
          "confidence": "high"|"medium"|"low"|"none",
          "evidence": [...],          # for the top candidate (≤4)
          "candidates": [...],        # top 3, all confidences, sorted desc
          "queries": [...],           # Gmail queries run
          "supporting": [...],        # scheduling notices (no address), ≤3
          "name": {...},              # parsed name (firsts / surname)
          "error": str               # only present when Gmail access failed
        }
    """
    import html as _html
    context = context or {}
    parsed = parse_candidate_name(candidate_name)
    internal_domains = tuple(d.lower() for d in internal_domains)
    own_addresses = tuple(a.lower() for a in own_addresses)
    base = {
        "email": None, "confidence": "none", "evidence": [], "candidates": [],
        "queries": [], "supporting": [],
        "name": {"firsts": parsed["display_firsts"], "surname": parsed["display_surname"]},
    }
    if not parsed["surname"]:
        return base

    query_plan = _build_queries(parsed, internal_domains=internal_domains)
    base["queries"] = [q["q"] for q in query_plan]
    if not query_plan:
        return base

    try:
        from googleapiclient.discovery import build
        from .google_auth_helper import get_credentials

        creds = get_credentials(credentials_path, token_path, [_GMAIL_READ_SCOPE])
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:  # auth / discovery failure
        print(f"[OUTREACH] Gmail resolver init failed for '{candidate_name}': {e}")
        base["error"] = str(e)
        return base

    # ── 1. List message ids per query (ordered; precise queries first) ──
    wanted: dict[str, dict] = {}     # msg id → {"anchor": bool}
    errors: list[str] = []
    for q in query_plan:
        if len(wanted) >= max_message_gets:
            break
        try:
            res = service.users().messages().list(
                userId="me", q=q["q"], maxResults=max_results_per_query,
            ).execute()
        except Exception as e:
            errors.append(f"{q['q']}: {e}")
            continue
        for ref in res.get("messages", []) or []:
            mid = ref.get("id")
            if not mid:
                continue
            if mid in wanted:
                wanted[mid]["anchor"] = wanted[mid]["anchor"] or q["anchor"]
            elif len(wanted) < max_message_gets:
                wanted[mid] = {"anchor": q["anchor"]}

    # ── 2. Fetch metadata in batches ──
    fetched: dict[str, dict] = {}

    def _cb(request_id, response, exception):
        if exception is not None or not response:
            errors.append(f"get {request_id}: {exception}")
            return
        fetched[request_id] = response

    ids = list(wanted.keys())
    CHUNK = 20
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        try:
            batch = service.new_batch_http_request(callback=_cb)
            for mid in chunk:
                batch.add(
                    service.users().messages().get(
                        userId="me", id=mid, format="metadata",
                        metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
                    ),
                    request_id=mid,
                )
            batch.execute()
        except Exception as e:
            # Batch endpoint failed wholesale — fall back to sequential gets.
            errors.append(f"batch: {e}")
            for mid in chunk:
                if mid in fetched:
                    continue
                try:
                    fetched[mid] = service.users().messages().get(
                        userId="me", id=mid, format="metadata",
                        metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
                    ).execute()
                except Exception as e2:
                    errors.append(f"get {mid}: {e2}")

    # ── 3. Normalise; pull full body for Calendly notices whose snippet lacks the invitee email ──
    messages: list[dict] = []
    calendly_full_budget = 5
    for mid in ids:
        msg = fetched.get(mid)
        if not msg:
            continue
        headers = {h.get("name", ""): h.get("value", "") for h in (msg.get("payload") or {}).get("headers", [])}
        # Header names are case-insensitive; normalise the ones we use.
        norm = {}
        for k, v in headers.items():
            lk = k.lower()
            if lk == "from": norm["From"] = v
            elif lk == "to": norm["To"] = v
            elif lk == "cc": norm["Cc"] = v
            elif lk == "subject": norm["Subject"] = v
            elif lk == "date": norm["Date"] = v
        item = {
            "id": mid,
            "threadId": msg.get("threadId", ""),
            "headers": norm,
            "snippet": _html.unescape(msg.get("snippet", "") or ""),
            "date": _parse_msg_date(msg),
            "anchor": wanted[mid]["anchor"],
            "body": "",
        }
        _, from_addr = _split_address(norm.get("From", ""))
        if _is_calendly_sender(from_addr) and "invitee email" not in item["snippet"].lower() and calendly_full_budget > 0:
            calendly_full_budget -= 1
            try:
                full = service.users().messages().get(userId="me", id=mid, format="full").execute()
                item["body"] = _body_text(full)[:4000]
            except Exception as e:
                errors.append(f"full {mid}: {e}")
        messages.append(item)

    scored = score_candidate_messages(
        messages, parsed, internal_domains=internal_domains, own_addresses=own_addresses,
    )
    candidates = scored["candidates"]
    top = candidates[0] if candidates else None

    result = dict(base)
    result["supporting"] = scored["supporting"][:3]
    result["candidates"] = [
        {"email": c["email"], "confidence": c["confidence"], "score": c["score"],
         "display_names": c["display_names"], "evidence": c["evidence"][:4]}
        for c in candidates[:3]
    ]
    if top:
        result["confidence"] = top["confidence"]
        evidence = list(top["evidence"][:4])
        if top["confidence"] in ("high", "medium"):
            result["email"] = top["email"]
            if scored["supporting"] and len(evidence) < 4:
                evidence.append(scored["supporting"][0])
        result["evidence"] = evidence
    if errors and not candidates:
        result["error"] = "; ".join(errors[:3])
    elif errors:
        result["warnings"] = errors[:3]
    if context.get("client_name"):
        result["client_name"] = context["client_name"]
    return result


def lookup_candidate_email(
    candidate_name: str,
    credentials_path: str,
    token_path: str,
) -> Optional[str]:
    """
    Backward-compatible wrapper around `resolve_candidate_email`: returns the
    resolved address only when confidence is high or medium, else None.
    """
    try:
        return resolve_candidate_email(candidate_name, credentials_path, token_path)["email"]
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
