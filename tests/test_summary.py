import json
from datetime import date
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

from emailSummary import (
    accept_filing,
    analyze_with_claude,
    build_html_report,
    deduplicate_inbox_emails,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_email(from_addr, subject="Test Subject", from_display=None, count=1):
    return {
        "msg_id": b"1",
        "from_display": from_display or from_addr,
        "from_addr": from_addr,
        "subject": subject,
        "date": "Mon, 28 Jun 2026 10:00:00 +0000",
        "body_snippet": "Email body preview",
        "count": count,
    }


def _mock_anthropic_stream(response_text: str):
    """Return a mock that behaves like client.messages.stream(...) context manager."""
    mock_stream = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text=response_text)]
    mock_stream.get_final_message.return_value = mock_response

    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_stream
    mock_cm.__exit__.return_value = False
    return mock_cm


def _anthropic_request():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


# ── deduplicate_inbox_emails ──────────────────────────────────────────────────

def test_deduplicate_keeps_distinct_senders():
    emails = [
        _make_email("alice@example.com"),
        _make_email("bob@example.com"),
    ]
    result = deduplicate_inbox_emails(emails)
    assert len(result) == 2
    addrs = {e["from_addr"] for e in result}
    assert addrs == {"alice@example.com", "bob@example.com"}


def test_deduplicate_collapses_same_sender():
    emails = [
        _make_email("alice@example.com", subject="Msg 1"),
        _make_email("alice@example.com", subject="Msg 2"),
        _make_email("alice@example.com", subject="Msg 3"),
    ]
    result = deduplicate_inbox_emails(emails)
    assert len(result) == 1
    assert result[0]["count"] == 3


def test_deduplicate_preserves_first_occurrence_data():
    emails = [
        _make_email("alice@example.com", subject="First"),
        _make_email("alice@example.com", subject="Second"),
    ]
    result = deduplicate_inbox_emails(emails)
    assert result[0]["subject"] == "First"


def test_deduplicate_empty_list():
    assert deduplicate_inbox_emails([]) == []


def test_deduplicate_single_email():
    emails = [_make_email("solo@example.com")]
    result = deduplicate_inbox_emails(emails)
    assert len(result) == 1
    assert result[0]["count"] == 1


# ── build_html_report ─────────────────────────────────────────────────────────

def test_build_html_report_contains_date_heading():
    html = build_html_report(
        target_date=date(2026, 6, 28),
        inbox_emails=[],
        filed_emails={},
        analysis={"action_required": [], "filing_suggestions": []},
    )
    assert "June 28, 2026" in html


def test_build_html_report_has_three_sections():
    html = build_html_report(
        target_date=date(2026, 6, 28),
        inbox_emails=[],
        filed_emails={},
        analysis={"action_required": [], "filing_suggestions": []},
    )
    assert "Action Required" in html
    assert "Unmatched Emails" in html
    assert "Filed Emails" in html


def test_build_html_report_shows_action_item():
    analysis = {
        "action_required": [{
            "index": 1,
            "from": "boss@work.com",
            "subject": "Urgent: Meeting",
            "reason": "Needs a reply by EOD",
        }],
        "filing_suggestions": [],
    }
    html = build_html_report(date(2026, 6, 28), [], {}, analysis)
    assert "Urgent: Meeting" in html
    assert "Needs a reply by EOD" in html


def test_build_html_report_empty_action_items_shows_placeholder():
    html = build_html_report(date(2026, 6, 28), [], {}, {"action_required": [], "filing_suggestions": []})
    assert "No emails require immediate action" in html


def test_build_html_report_unmatched_email_card():
    email = _make_email("alice@nowhere.com", subject="Hello there")
    html = build_html_report(date(2026, 6, 28), [email], {}, {"action_required": [], "filing_suggestions": []})
    assert "alice@nowhere.com" in html
    assert "Hello there" in html


def test_build_html_report_accept_button_for_suggestion():
    email = _make_email("alice@nowhere.com", subject="Hello")
    analysis = {
        "action_required": [],
        "filing_suggestions": [{
            "index": 1,
            "from": "alice@nowhere.com",
            "subject": "Hello",
            "suggested_label": "MailMatrixCategories/Personal",
            "is_new_label": False,
            "reason": "Looks personal",
        }],
    }
    html = build_html_report(date(2026, 6, 28), [email], {}, analysis)
    assert "Accept" in html
    assert "MailMatrixCategories/Personal" in html


def test_build_html_report_filed_emails():
    filed = {
        "MailMatrixCategories/Work": [
            _make_email("boss@work.com", subject="Status Update"),
        ]
    }
    html = build_html_report(date(2026, 6, 28), [], filed, {"action_required": [], "filing_suggestions": []})
    assert "Status Update" in html
    assert "Work" in html


def test_build_html_report_escapes_html_in_subjects():
    xss = '<script>alert("xss")</script>'
    email = _make_email("a@b.com", subject=xss)
    html = build_html_report(date(2026, 6, 28), [email], {}, {"action_required": [], "filing_suggestions": []})
    # The raw XSS payload must not appear literally in the output
    assert xss not in html
    # The escaped form must be present
    assert "&lt;script&gt;" in html


def test_build_html_report_error_note():
    analysis = {
        "action_required": [],
        "filing_suggestions": [],
        "_error": "API key invalid",
    }
    html = build_html_report(date(2026, 6, 28), [], {}, analysis)
    assert "AI analysis unavailable" in html
    assert "API key invalid" in html


def test_build_html_report_count_badge_for_duplicated_sender():
    email = _make_email("newsletter@promo.com", count=5)
    html = build_html_report(date(2026, 6, 28), [email], {}, {"action_required": [], "filing_suggestions": []})
    assert "5×" in html


# ── analyze_with_claude ───────────────────────────────────────────────────────

def test_analyze_with_claude_returns_empty_for_empty_inbox():
    result = analyze_with_claude([], ["MailMatrixCategories/Work"])
    assert result == {"action_required": [], "filing_suggestions": []}


def test_analyze_with_claude_parses_json_response():
    response_json = json.dumps({
        "action_required": [{"index": 1, "from": "boss@work.com", "subject": "Urgent", "reason": "Needs reply"}],
        "filing_suggestions": [{"index": 1, "from": "boss@work.com", "subject": "Urgent",
                                  "suggested_label": "MailMatrixCategories/Work", "is_new_label": False, "reason": "Work email"}],
    })

    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.return_value = _mock_anthropic_stream(response_json)

        emails = [_make_email("boss@work.com", subject="Urgent")]
        result = analyze_with_claude(emails, ["MailMatrixCategories/Work"])

    assert len(result["action_required"]) == 1
    assert result["action_required"][0]["from"] == "boss@work.com"
    assert len(result["filing_suggestions"]) == 1


def test_analyze_with_claude_handles_auth_error():
    req = _anthropic_request()
    resp = httpx.Response(401, request=req)
    exc = anthropic.AuthenticationError(message="Invalid key", response=resp, body={})

    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.side_effect = exc

        emails = [_make_email("a@b.com")]
        result = analyze_with_claude(emails, [])

    assert result["action_required"] == []
    assert result["filing_suggestions"] == []
    assert "API key" in result["_error"]


def test_analyze_with_claude_handles_no_credits():
    req = _anthropic_request()
    resp = httpx.Response(402, request=req)
    exc = anthropic.APIStatusError(message="Payment required", response=resp, body={})

    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.side_effect = exc

        emails = [_make_email("a@b.com")]
        result = analyze_with_claude(emails, [])

    assert result["action_required"] == []
    assert "credits" in result["_error"]


def test_analyze_with_claude_handles_connection_error():
    req = _anthropic_request()
    exc = anthropic.APIConnectionError(message="Cannot connect", request=req)

    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.side_effect = exc

        emails = [_make_email("a@b.com")]
        result = analyze_with_claude(emails, [])

    assert result["action_required"] == []
    assert "connect" in result["_error"].lower()


def test_analyze_with_claude_falls_back_on_invalid_json():
    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.return_value = _mock_anthropic_stream("This is not JSON at all.")

        emails = [_make_email("a@b.com")]
        result = analyze_with_claude(emails, [])

    assert result["action_required"] == []
    assert result["filing_suggestions"] == []


# ── accept_filing ─────────────────────────────────────────────────────────────

def test_accept_filing_missing_from_addr():
    result = accept_filing(
        body={"label": "MailMatrixCategories/Work"},
        imap_server="imap.gmail.com", imap_port=993,
        username="u", password="p", rules_path="/tmp/rules.json",
    )
    assert result["ok"] is False
    assert "Missing" in result["error"]


def test_accept_filing_missing_label():
    result = accept_filing(
        body={"from_addr": "alice@example.com"},
        imap_server="imap.gmail.com", imap_port=993,
        username="u", password="p", rules_path="/tmp/rules.json",
    )
    assert result["ok"] is False
    assert "Missing" in result["error"]


def test_accept_filing_moves_messages_and_updates_rules(mock_imap, tmp_path):
    rules_file = tmp_path / "emailRules.json"
    rules_file.write_text(json.dumps({
        "labels": [{
            "labelName": "MailMatrixCategories/Work",
            "emailAddresses": ["boss@work.com"],
            "emailDomains": [],
        }]
    }))

    mock_imap.search.return_value = ("OK", [b"1 2"])

    with patch("emailSummary.connect_to_imap", return_value=mock_imap):
        result = accept_filing(
            body={"from_addr": "new@work.com", "label": "MailMatrixCategories/Work"},
            imap_server="imap.gmail.com", imap_port=993,
            username="user@gmail.com", password="pass",
            rules_path=str(rules_file),
        )

    assert result["ok"] is True
    assert result["moved"] == 2
    mock_imap.copy.assert_called()
    mock_imap.store.assert_called()
    mock_imap.expunge.assert_called_once()

    updated = json.loads(rules_file.read_text())
    work_entry = next(l for l in updated["labels"] if l["labelName"] == "MailMatrixCategories/Work")
    assert "new@work.com" in work_entry["emailAddresses"]


def test_accept_filing_creates_new_label_entry(mock_imap, tmp_path):
    rules_file = tmp_path / "emailRules.json"
    rules_file.write_text(json.dumps({"labels": []}))

    mock_imap.search.return_value = ("OK", [b"1"])

    with patch("emailSummary.connect_to_imap", return_value=mock_imap):
        result = accept_filing(
            body={"from_addr": "promo@newsletter.com", "label": "MailMatrixCategories/Newsletters"},
            imap_server="imap.gmail.com", imap_port=993,
            username="user@gmail.com", password="pass",
            rules_path=str(rules_file),
        )

    assert result["ok"] is True
    updated = json.loads(rules_file.read_text())
    assert len(updated["labels"]) == 1
    assert updated["labels"][0]["labelName"] == "MailMatrixCategories/Newsletters"
    assert "promo@newsletter.com" in updated["labels"][0]["emailAddresses"]


def test_accept_filing_no_messages_to_move(mock_imap, tmp_path):
    rules_file = tmp_path / "emailRules.json"
    rules_file.write_text(json.dumps({"labels": []}))

    mock_imap.search.return_value = ("OK", [b""])  # empty search result

    with patch("emailSummary.connect_to_imap", return_value=mock_imap):
        result = accept_filing(
            body={"from_addr": "nobody@example.com", "label": "MailMatrixCategories/Work"},
            imap_server="imap.gmail.com", imap_port=993,
            username="user@gmail.com", password="pass",
            rules_path=str(rules_file),
        )

    assert result["ok"] is True
    assert result["moved"] == 0
    mock_imap.copy.assert_not_called()
    mock_imap.expunge.assert_not_called()
