"""Tests for the Slack/Ashby dedupe in merge_ashby_into_submissions."""
import copy

from weekly_slack_recon.ashby_importer import (
    _company_matches,
    _normalize_name_key,
    merge_ashby_into_submissions,
)


def slack_row(**overrides):
    row = {
        "candidate_name": "Manoj Kumar Panguluru",
        "linkedin_url": "https://www.linkedin.com/in/manoj-kumar-panguluru",
        "channel_name": "candidatelabs-langchain-eng",
        "channel_id": "C0BD6QJDQKA",
        "submitted_at": "2026-07-06T20:05:30+00:00",
        "status": "IN PROCESS — explicit",
        "status_reason": ":white_check_mark:",
        "days_since_submission": 1,
        "needs_followup": False,
        "slack_url": "https://candidatelabs.slack.com/archives/C0BD6QJDQKA/p1",
        "ai_summary": None,
        "ai_enriched_at": None,
    }
    row.update(overrides)
    return row


def ashby_row(**overrides):
    row = {
        "source": "ashby",
        "candidate_name": "Manoj Panguluru",
        "linkedin_url": None,
        "email": None,
        "submitted_at": "2026-07-06T00:00:00+00:00",
        "days_since_submission": 1,
        "status": "IN PROCESS — explicit",
        "status_reason": "Recruiter Screen",
        "needs_followup": False,
        "company_name": "Langchain",
        "job_title": "Senior Backend Software Engineer",
        "pipeline_stage": "Recruiter Screen",
        "stage_progress": "2/8",
        "days_in_stage": 1,
        "needs_scheduling": True,
        "ashby_candidate_id": "cand-123",
        "interview_events": [],
    }
    row.update(overrides)
    return row


def test_middle_name_merge_collapses_to_one_row():
    merged = merge_ashby_into_submissions([slack_row()], [ashby_row()])
    assert len(merged) == 1
    row = merged[0]
    assert row["candidate_name"] == "Manoj Kumar Panguluru"
    assert row["pipeline_stage"] == "Recruiter Screen"
    assert row["stage_progress"] == "2/8"
    assert row["also_in_ashby"] is True
    assert row["slack_url"]
    assert row["linkedin_url"]
    assert row["slack_status"] == "IN PROCESS — explicit"
    assert row.get("source", "slack") != "ashby"


def test_linkedin_merge_legacy_path():
    url = "https://www.linkedin.com/in/manoj-kumar-panguluru/"
    merged = merge_ashby_into_submissions(
        [slack_row()],
        [ashby_row(candidate_name="M. K. P.", company_name="Somewhere Else", linkedin_url=url)],
    )
    assert len(merged) == 1
    assert merged[0]["also_in_ashby"] is True


def test_same_name_different_company_stays_separate():
    merged = merge_ashby_into_submissions(
        [slack_row()],
        [ashby_row(company_name="Acme")],
    )
    assert len(merged) == 2


def test_same_company_different_name_stays_separate():
    merged = merge_ashby_into_submissions(
        [slack_row()],
        [ashby_row(candidate_name="Priya Sharma")],
    )
    assert len(merged) == 2


def test_company_matching_rules():
    assert _company_matches("candidatelabs-langchain-eng", "Langchain")
    assert _company_matches("candidatelabs-charta-health-fwd", "Charta Health")
    assert not _company_matches("candidatelabs-langchain-eng", "Lang")
    assert not _company_matches(None, "Langchain")
    assert not _company_matches("candidatelabs-langchain-eng", None)


def test_company_matching_spacing_variants():
    # "Preference Model" (Ashby) vs channel "candidatelabs-preferencemodel"
    assert _company_matches("candidatelabs-preferencemodel", "Preference Model")
    assert _company_matches("candidatelabs-preference-model", "Preferencemodel")
    # Short fragments still never match via the compact fallback
    assert not _company_matches("candidatelabs-preferencemodel", "Pref")


def test_spacing_variant_merge_collapses_to_one_row():
    slack = slack_row(channel_name="candidatelabs-preferencemodel")
    ashby = ashby_row(company_name="Preference Model")
    merged = merge_ashby_into_submissions([slack], [ashby])
    assert len(merged) == 1
    assert merged[0]["also_in_ashby"] is True


def test_name_key_tolerates_middle_names():
    assert _normalize_name_key("Manoj Kumar Panguluru") == "manoj panguluru"
    assert _normalize_name_key("Manoj Panguluru") == "manoj panguluru"
    assert _normalize_name_key("  ") == ""
    assert _normalize_name_key("Cher") == "cher"


def test_remerge_is_idempotent_and_refreshes():
    m1 = merge_ashby_into_submissions([slack_row()], [ashby_row()])
    updated = ashby_row(pipeline_stage="Onsite", stage_progress="5/8")
    m2 = merge_ashby_into_submissions(copy.deepcopy(m1), [updated])
    assert len(m2) == len(m1) == 1
    assert m2[0]["pipeline_stage"] == "Onsite"
    assert m2[0]["slack_status"] == "IN PROCESS — explicit"


def test_ashby_disappearance_restores_slack_status():
    m1 = merge_ashby_into_submissions(
        [slack_row()], [ashby_row(status="CLOSED")]
    )
    assert m1[0]["status"] == "CLOSED"
    m2 = merge_ashby_into_submissions(copy.deepcopy(m1), [])
    assert len(m2) == 1
    assert m2[0]["status"] == "IN PROCESS — explicit"
    assert m2[0]["also_in_ashby"] is False
    assert "pipeline_stage" not in m2[0]
    assert "slack_status" not in m2[0]


def test_one_slack_row_absorbs_at_most_one_ashby_record():
    older = slack_row(submitted_at="2026-06-01T00:00:00+00:00", slack_url="old-thread")
    newer = slack_row(submitted_at="2026-07-06T00:00:00+00:00", slack_url="new-thread")
    merged = merge_ashby_into_submissions([older, newer], [ashby_row()])
    assert len(merged) == 2
    absorbed = [r for r in merged if r.get("also_in_ashby")]
    assert len(absorbed) == 1
    assert absorbed[0]["slack_url"] == "new-thread"
