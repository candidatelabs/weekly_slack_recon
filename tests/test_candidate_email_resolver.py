"""
Unit tests for the candidate email resolver in candidate_outreach.py.

No network: exercises name parsing, query planning, and the pure scoring step
with fake Gmail messages. Run with `python -m pytest tests/` or directly with
`python tests/test_candidate_email_resolver.py`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from weekly_slack_recon import candidate_outreach as co  # noqa: E402
from weekly_slack_recon.candidate_outreach import (  # noqa: E402
    _build_queries,
    _is_system_address,
    parse_candidate_name,
    score_candidate_messages,
)


# ── Name parsing ───────────────────────────────────────────────────────────────

def test_parse_parenthetical_nickname_and_surname():
    p = parse_candidate_name("DJ (Krishnamurthy) Dvijotham")
    assert p["firsts"] == ["dj", "krishnamurthy"]
    assert p["surname"] == "dvijotham"
    assert p["single_token"] is False
    assert p["searchable_surname"] is True


def test_parse_short_surname_is_not_searchable_bare():
    p = parse_candidate_name("Zoe (Zhaoyuan) Xi")
    assert p["firsts"] == ["zoe", "zhaoyuan"]
    assert p["surname"] == "xi"
    assert p["searchable_surname"] is False


def test_parse_strips_trailing_junk_and_suffixes():
    p = parse_candidate_name("Aayush Gupta –")
    assert p["firsts"] == ["aayush"] and p["surname"] == "gupta"
    p = parse_candidate_name("Jane Smith-Jones Jr.")
    assert p["surname"] == "smithjones" and p["display_surname"] == "Smith-Jones"


def test_parse_common_word_surname_is_tight():
    p = parse_candidate_name("Sahil Jolly")
    assert p["surname"] == "jolly" and p["searchable_surname"] is False


def test_parse_single_token():
    p = parse_candidate_name("Madonna")
    assert p["single_token"] is True and p["surname"] == "madonna" and p["firsts"] == []


def test_parse_ignores_multiword_parentheticals():
    p = parse_candidate_name("Alex (he/him) Chen")
    assert p["firsts"] == ["alex"] and p["surname"] == "chen"


# ── Query planning ─────────────────────────────────────────────────────────────

def test_queries_never_first_name_only():
    for name in ["DJ (Krishnamurthy) Dvijotham", "Zoe (Zhaoyuan) Xi", "Sahil Jolly", "Aayush Gupta –", "Charles Lin"]:
        parsed = parse_candidate_name(name)
        queries = _build_queries(parsed, internal_domains=("candidatelabs.com",))
        assert queries, name
        for q in queries:
            qs = q["q"]
            assert q["anchor"] is True
            # Every query must mention the surname (display form) somewhere.
            assert parsed["display_surname"].lower() in qs.lower(), (name, qs)
            # And never be a bare first-name query.
            for f in parsed["display_firsts"]:
                assert f'from:"{f}"' not in qs and qs.strip() != f'"{f}"', (name, qs)


def test_queries_surname_anchor_excludes_internal_domain_and_reads_sent_mail():
    parsed = parse_candidate_name("DJ (Krishnamurthy) Dvijotham")
    qs = [q["q"] for q in _build_queries(parsed, internal_domains=("candidatelabs.com",))]
    assert any("-from:candidatelabs.com" in q for q in qs)
    assert any(q.startswith('from:me "Dvijotham"') for q in qs)


# ── System / internal exclusion ────────────────────────────────────────────────

def test_system_address_detection():
    assert _is_system_address("notifications@calendly.com")
    assert _is_system_address("no-reply@zoom.us")
    assert _is_system_address("reminder@superhuman.com")
    assert _is_system_address("calendar-notification@google.com")
    assert _is_system_address("drive-shares-dm-noreply@google.com")
    assert _is_system_address("careers@acme.com")
    # Real people whose local-part merely starts with a system-ish prefix.
    assert not _is_system_address("autumn.smith@gmail.com")
    assert not _is_system_address("newsome.j@gmail.com")
    assert not _is_system_address("dvijcse@gmail.com")


# ── Scoring with fake messages ─────────────────────────────────────────────────

def _msg(mid, thread, frm, to="", cc="", subject="", snippet="", date="2026-07-20", anchor=True, body=""):
    return {
        "id": mid, "threadId": thread,
        "headers": {"From": frm, "To": to, "Cc": cc, "Subject": subject, "Date": ""},
        "snippet": snippet, "date": date, "anchor": anchor, "body": body,
    }


DK = "David Kimball <dkimball@candidatelabs.com>"


def test_dj_regression_scores_personal_gmail_high_and_excludes_colleague():
    parsed = parse_candidate_name("DJ (Krishnamurthy) Dvijotham")
    msgs = [
        # DK's sent mail to a bare address (no display name) — surname co-occurs via anchor query.
        _msg("1", "t1", DK, to="dvijcse@gmail.com", subject="Chat recap", date="2026-07-20"),
        _msg("2", "t2", DK, to="dvijcse@gmail.com", subject="How did the Prometheus conversation go?", date="2026-08-01"),
        # Their reply, display name carries the surname.
        _msg("3", "t2", "Dvijotham Krishnamurthy <dvijcse@gmail.com>", to=DK,
             subject="Re: How did the Prometheus conversation go?", date="2026-08-03"),
        # Calendly invitee notice.
        _msg("4", "t3", "Calendly <notifications@calendly.com>", to=DK,
             subject="New Event: Dj Dvijotham - 12:00pm Mon, Jul 20",
             snippet="A new event has been scheduled. Event Type: Intro chat Invitee: Dj Dvijotham  Invitee Email: dvijcse@gmail.com Event Date", date="2026-07-14"),
        # Zoom/Metaview scheduling notices — supporting only.
        _msg("5", "t4", "Zoom <no-reply@zoom.us>", to=DK, subject="Notetaker joined: Dj Dvijotham and David Kimball", date="2026-07-20"),
        # A colleague whose first name is also "DJ" — the old regression.
        _msg("6", "t5", "DJ DeAnda <djdeanda@candidatelabs.com>", to=DK, subject="Re: pipeline sync", date="2026-08-05", anchor=False),
    ]
    out = score_candidate_messages(msgs, parsed)
    cands = out["candidates"]
    assert cands, "expected a candidate"
    top = cands[0]
    assert top["email"] == "dvijcse@gmail.com"
    assert top["confidence"] == "high"
    assert top["bidirectional"] is True
    kinds = {e["kind"] for e in top["evidence"]}
    assert {"you_emailed", "they_emailed", "calendly_invitee"} <= kinds
    assert all(not c["email"].endswith("@candidatelabs.com") for c in cands)
    assert any(s["kind"] == "scheduling_notice" for s in out["supporting"])


def test_local_part_only_surname_match_caps_at_low():
    parsed = parse_candidate_name("Aayush Gupta")
    msgs = [
        _msg("1", "t1", "<arul18.gupta@gmail.com>", to=DK, subject="Hi", date="2026-07-01"),
        _msg("2", "t2", "<arul18.gupta@gmail.com>", to=DK, subject="Hello again", date="2026-07-05"),
    ]
    out = score_candidate_messages(msgs, parsed)
    assert out["candidates"] and out["candidates"][0]["email"] == "arul18.gupta@gmail.com"
    assert out["candidates"][0]["confidence"] == "low"


def test_conflicting_first_name_demotes_to_low():
    parsed = parse_candidate_name("Aayush Gupta")
    msgs = [
        _msg("1", "t1", "Rahul Gupta <rahul.g@gmail.com>", to=DK, subject="Re: roles", date="2026-07-01"),
        _msg("2", "t2", DK, to="Rahul Gupta <rahul.g@gmail.com>", subject="Re: roles", date="2026-07-02"),
    ]
    out = score_candidate_messages(msgs, parsed)
    assert out["candidates"][0]["confidence"] == "low"


def test_no_name_evidence_is_dropped():
    parsed = parse_candidate_name("Aayush Gupta")
    msgs = [_msg("1", "t1", DK, to="hiring.manager@client.com", subject="About Aayush Gupta", date="2026-07-01")]
    out = score_candidate_messages(msgs, parsed)
    assert out["candidates"] == []


def test_single_token_name_caps_at_medium():
    parsed = parse_candidate_name("Dvijotham")
    msgs = [
        _msg("1", "t1", "Dvijotham <dvijcse@gmail.com>", to=DK, subject="Hi", date="2026-07-01"),
        _msg("2", "t2", DK, to="Dvijotham <dvijcse@gmail.com>", subject="Re: Hi", date="2026-07-02"),
    ]
    out = score_candidate_messages(msgs, parsed)
    assert out["candidates"][0]["confidence"] == "medium"


def test_internal_and_system_addresses_never_become_candidates():
    parsed = parse_candidate_name("DJ (Krishnamurthy) Dvijotham")
    msgs = [
        _msg("1", "t1", "Dvijotham DeAnda <dj@candidatelabs.com>", to=DK, subject="x"),
        _msg("2", "t2", "Dvijotham Bot <noreply@dvijotham.com>", to=DK, subject="x"),
        _msg("3", "t3", DK, to="dkimball@candidatelabs.com", subject="Dvijotham note to self"),
    ]
    out = score_candidate_messages(msgs, parsed)
    assert out["candidates"] == []


def test_lookup_wrapper_returns_email_only_for_confident_results(monkeypatch):
    monkeypatch.setattr(co, "resolve_candidate_email",
                        lambda *a, **k: {"email": "x@gmail.com", "confidence": "high"})
    assert co.lookup_candidate_email("X Y", "c", "t") == "x@gmail.com"
    monkeypatch.setattr(co, "resolve_candidate_email",
                        lambda *a, **k: {"email": None, "confidence": "low"})
    assert co.lookup_candidate_email("X Y", "c", "t") is None


if __name__ == "__main__":
    import inspect
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            params = inspect.signature(fn).parameters
            if "monkeypatch" in params:
                class _MP:
                    def setattr(self, obj, attr, val): setattr(obj, attr, val)
                fn(_MP())
            else:
                fn()
            print("ok  ", name)
    print("all passed" if not failed else f"{failed} failed")
