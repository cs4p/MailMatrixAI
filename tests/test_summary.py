import json
import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock, call, mock_open, patch

import anthropic
import httpx
import pytest

from emailSummary import (
    ReportGenerationError,
    accept_filing,
    analyze_with_claude,
    build_html_report,
    deduplicate_inbox_emails,
    generate_report,
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
    mock_response.stop_reason = "end_turn"
    mock_response.usage.input_tokens = 123
    mock_response.usage.output_tokens = 45
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


def test_build_html_report_stat_bar_shows_processed_need_attention_unfiled():
    email = _make_email("alice@nowhere.com")
    analysis = {
        "action_required": [{"index": 1, "from": "alice@nowhere.com", "subject": "x", "reason": "y"}],
        "filing_suggestions": [],
    }
    filed = {"MailMatrixCategories/Work": [_make_email("boss@work.com")]}
    html = build_html_report(date(2026, 6, 28), [email], filed, analysis)
    assert '<div class="stat-num blue">2</div><div class="stat-label">Processed</div>' in html
    assert '<div class="stat-num red">1</div><div class="stat-label">Need Attention</div>' in html
    assert '<div class="stat-num orange">1</div><div class="stat-label">Unfiled</div>' in html
    assert '<div class="stat-num green">1</div><div class="stat-label">Filed</div>' in html


def test_build_html_report_shows_generated_at_timestamp():
    html = build_html_report(
        date(2026, 6, 28), [], {}, {"action_required": [], "filing_suggestions": []},
        generated_at=datetime(2026, 6, 28, 9, 5),
    )
    assert "Generated Jun 28, 2026 at 09:05 AM" in html


def test_build_html_report_defaults_generated_at_to_now():
    html = build_html_report(date(2026, 6, 28), [], {}, {"action_required": [], "filing_suggestions": []})
    assert "Generated" in html


def test_build_html_report_refresh_button_carries_target_date():
    html = build_html_report(date(2026, 6, 28), [], {}, {"action_required": [], "filing_suggestions": []})
    assert 'data-date="2026-06-28"' in html
    assert 'onclick="refreshReport(this)"' in html


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


def test_build_html_report_suggestion_renders_as_dropdown():
    email = _make_email("alice@nowhere.com", subject="Hello")
    labels = ["MailMatrixCategories/Personal", "MailMatrixCategories/Work", "MailMatrixCategories/Shopping"]
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
    html = build_html_report(date(2026, 6, 28), [email], {}, analysis, available_labels=labels)
    assert '<select class="label-select">' in html
    assert 'value="MailMatrixCategories/Personal" selected' in html
    assert 'value="MailMatrixCategories/Work"' in html
    assert 'value="MailMatrixCategories/Shopping"' in html


def test_build_html_report_new_label_appears_first_in_dropdown():
    email = _make_email("promo@new.com", subject="Hi")
    labels = ["MailMatrixCategories/Work"]
    analysis = {
        "action_required": [],
        "filing_suggestions": [{
            "index": 1,
            "from": "promo@new.com",
            "subject": "Hi",
            "suggested_label": "MailMatrixCategories/Promos",
            "is_new_label": True,
            "reason": "Promotional email",
        }],
    }
    html = build_html_report(date(2026, 6, 28), [email], {}, analysis, available_labels=labels)
    # Suggested new label should be selected
    assert 'value="MailMatrixCategories/Promos" selected' in html
    # Existing labels should also be present
    assert 'value="MailMatrixCategories/Work"' in html
    assert "new label" in html


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


def test_analyze_with_claude_logs_success_with_token_usage(caplog):
    response_json = json.dumps({"action_required": [], "filing_suggestions": []})

    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic, caplog.at_level("INFO"):
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.return_value = _mock_anthropic_stream(response_json)

        emails = [_make_email("a@b.com")]
        analyze_with_claude(emails, [])

    assert any(
        "Claude response received" in r.message and "input_tokens=123" in r.message and "output_tokens=45" in r.message
        for r in caplog.records
    )


def test_analyze_with_claude_handles_rate_limit(caplog):
    req = _anthropic_request()
    resp = httpx.Response(429, request=req, headers={"retry-after": "30"})
    exc = anthropic.RateLimitError(message="Rate limited", response=resp, body={})

    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic, caplog.at_level("ERROR"):
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.side_effect = exc

        emails = [_make_email("a@b.com")]
        result = analyze_with_claude(emails, [])

    assert result["action_required"] == []
    assert result["filing_suggestions"] == []
    assert "rate limit" in result["_error"].lower()
    assert any(
        r.levelname == "ERROR" and "rate limited" in r.message.lower() and "30" in r.message
        for r in caplog.records
    )


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


# ── generate_report ────────────────────────────────────────────────────────────

def test_generate_report_raises_when_credentials_missing(monkeypatch):
    monkeypatch.delenv("IMAP_SERVER", raising=False)
    monkeypatch.delenv("IMAP_USERNAME", raising=False)
    monkeypatch.delenv("IMAP_PASSWORD", raising=False)

    with pytest.raises(ReportGenerationError):
        generate_report(date(2026, 6, 28), run_sort=False)


def test_generate_report_returns_expected_shape(mock_imap, tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_SERVER", "imap.test.com")
    monkeypatch.setenv("IMAP_PORT", "993")
    monkeypatch.setenv("IMAP_USERNAME", "user@test.com")
    monkeypatch.setenv("IMAP_PASSWORD", "pass")

    with (
        patch("emailSummary.connect_to_imap", return_value=mock_imap),
        patch("emailSummary.get_all_labels", return_value=["MailMatrixCategories/Work"]),
        patch("emailSummary.fetch_folder_emails", return_value=[]),
        patch("emailSummary.fetch_inbox_with_body", return_value=[]),
        patch("emailSummary.analyze_with_claude",
              return_value={"action_required": [{"index": 1}], "filing_suggestions": []}),
        patch("emailSummary._SCRIPT_DIR", str(tmp_path)),
    ):
        result = generate_report(date(2026, 6, 28), run_sort=False)

    assert result["unmatched"] == 0
    assert result["action_required"] == 1
    assert result["filed"] == 0
    assert result["processed"] == 0
    assert result["output_file"] == str(tmp_path / "emailSummary" / "email_summary_2026-06-28.html")
    assert os.path.exists(result["output_file"])
    assert "Generated" in result["html"]

    # Sidecar JSON stats for the Summaries list page
    meta_file = tmp_path / "emailSummary" / "email_summary_2026-06-28.json"
    assert meta_file.exists()
    meta = json.loads(meta_file.read_text())
    assert meta["processed"] == 0
    assert meta["need_attention"] == 1
    assert meta["unfiled"] == 0
    assert meta["filed"] == 0
    assert "generated_at" in meta


def test_generate_report_skips_sort_when_run_sort_false(mock_imap, tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_SERVER", "imap.test.com")
    monkeypatch.setenv("IMAP_USERNAME", "user@test.com")
    monkeypatch.setenv("IMAP_PASSWORD", "pass")

    with (
        patch("emailSummary.subprocess.run") as mock_run,
        patch("emailSummary.connect_to_imap", return_value=mock_imap),
        patch("emailSummary.get_all_labels", return_value=[]),
        patch("emailSummary.fetch_inbox_with_body", return_value=[]),
        patch("emailSummary.analyze_with_claude", return_value={"action_required": [], "filing_suggestions": []}),
        patch("emailSummary._SCRIPT_DIR", str(tmp_path)),
    ):
        generate_report(date(2026, 6, 28), run_sort=False)

    mock_run.assert_not_called()


# ── main() — sort-before-summary behaviour ────────────────────────────────────

def _make_sort_result(returncode=0):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = ""
    r.stderr = "sort error output" if returncode != 0 else ""
    return r


def _main_patches(mock_imap, tmp_path, monkeypatch):
    """Return a dict of patches needed to run emailSummary.main() in tests."""
    monkeypatch.setenv("IMAP_SERVER", "imap.test.com")
    monkeypatch.setenv("IMAP_PORT", "993")
    monkeypatch.setenv("IMAP_USERNAME", "user@test.com")
    monkeypatch.setenv("IMAP_PASSWORD", "pass")
    monkeypatch.setenv("RULES_PATH", str(tmp_path / "emailRules.json"))

    mock_imap.list.return_value = ("OK", [])
    mock_imap.search.return_value = ("OK", [b""])

    return {
        "emailSummary.connect_to_imap": mock_imap,
        "emailSummary.get_all_labels": [],
        "emailSummary.fetch_folder_emails": [],
        "emailSummary.fetch_inbox_with_body": [],
        "emailSummary.analyze_with_claude": {"action_required": [], "filing_suggestions": []},
    }


def test_main_runs_sort_script_before_imap(mock_imap, tmp_path, monkeypatch):
    """main() must call sortEmail.py as a subprocess before opening an IMAP connection."""
    _main_patches(mock_imap, tmp_path, monkeypatch)
    call_order = []

    def track_subprocess(cmd, **kw):
        call_order.append("sort")
        return _make_sort_result(0)

    def track_imap(*args, **kw):
        call_order.append("imap")
        return mock_imap

    with (
        patch("sys.argv", ["emailSummary.py", "--no-serve"]),
        patch("emailSummary.subprocess.run", side_effect=track_subprocess),
        patch("emailSummary.connect_to_imap", side_effect=track_imap),
        patch("emailSummary.get_all_labels", return_value=[]),
        patch("emailSummary.fetch_inbox_with_body", return_value=[]),
        patch("emailSummary.analyze_with_claude", return_value={"action_required": [], "filing_suggestions": []}),
        patch("emailSummary.os.makedirs"),
        patch("builtins.open", mock_open()),
    ):
        from emailSummary import main
        main()

    assert "sort" in call_order
    assert "imap" in call_order
    assert call_order.index("sort") < call_order.index("imap")


def test_main_sort_script_called_with_correct_path(mock_imap, tmp_path, monkeypatch):
    """subprocess.run should receive the sortEmail.py path as the command."""
    _main_patches(mock_imap, tmp_path, monkeypatch)
    captured = {}

    def capture_subprocess(cmd, **kw):
        captured["cmd"] = cmd
        return _make_sort_result(0)

    with (
        patch("sys.argv", ["emailSummary.py", "--no-serve"]),
        patch("emailSummary.subprocess.run", side_effect=capture_subprocess),
        patch("emailSummary.connect_to_imap", return_value=mock_imap),
        patch("emailSummary.get_all_labels", return_value=[]),
        patch("emailSummary.fetch_inbox_with_body", return_value=[]),
        patch("emailSummary.analyze_with_claude", return_value={"action_required": [], "filing_suggestions": []}),
        patch("emailSummary.os.makedirs"),
        patch("builtins.open", mock_open()),
    ):
        from emailSummary import main
        main()

    assert captured.get("cmd") is not None
    assert captured["cmd"][0] == sys.executable
    assert captured["cmd"][1].endswith("sortEmail.py")


def test_main_continues_after_sort_failure(mock_imap, tmp_path, monkeypatch):
    """If sortEmail.py exits non-zero, main() should log a warning and still generate the report."""
    _main_patches(mock_imap, tmp_path, monkeypatch)
    summary_written = {}

    def capture_open(path, *args, **kw):
        if "email_summary_" in str(path):
            summary_written["path"] = str(path)
        return mock_open()(path, *args, **kw)

    with (
        patch("sys.argv", ["emailSummary.py", "--no-serve"]),
        patch("emailSummary.subprocess.run", return_value=_make_sort_result(1)),
        patch("emailSummary.connect_to_imap", return_value=mock_imap),
        patch("emailSummary.get_all_labels", return_value=[]),
        patch("emailSummary.fetch_inbox_with_body", return_value=[]),
        patch("emailSummary.analyze_with_claude", return_value={"action_required": [], "filing_suggestions": []}),
        patch("emailSummary.os.makedirs"),
        patch("builtins.open", side_effect=capture_open),
    ):
        from emailSummary import main
        main()  # must not raise

    assert "path" in summary_written, "Report file was not written after sort failure"


def test_accept_filing_failed_copy_never_deletes(mock_imap, tmp_path):
    rules_file = tmp_path / "emailRules.json"
    rules_file.write_text(json.dumps({"labels": []}))

    mock_imap.search.return_value = ("OK", [b"1 2"])
    mock_imap.copy.return_value = ("NO", [b"[TRYCREATE] no such mailbox"])

    with patch("emailSummary.connect_to_imap", return_value=mock_imap):
        result = accept_filing(
            body={"from_addr": "new@work.com", "label": "MailMatrixCategories/Work"},
            imap_server="imap.gmail.com", imap_port=993,
            username="user@gmail.com", password="pass",
            rules_path=str(rules_file),
        )

    assert result["ok"] is False
    assert result["moved"] == 0
    assert result["copy_failed"] == 2
    mock_imap.store.assert_not_called()
    mock_imap.expunge.assert_not_called()
    # Rules must not be updated when nothing moved
    assert json.loads(rules_file.read_text()) == {"labels": []}


def test_accept_filing_partial_copy_failure_moves_the_rest(mock_imap, tmp_path):
    rules_file = tmp_path / "emailRules.json"
    rules_file.write_text(json.dumps({"labels": []}))

    mock_imap.search.return_value = ("OK", [b"1 2"])
    mock_imap.copy.side_effect = [("OK", None), ("NO", [b"quota exceeded"])]

    with patch("emailSummary.connect_to_imap", return_value=mock_imap):
        result = accept_filing(
            body={"from_addr": "new@work.com", "label": "MailMatrixCategories/Work"},
            imap_server="imap.gmail.com", imap_port=993,
            username="user@gmail.com", password="pass",
            rules_path=str(rules_file),
        )

    assert result["ok"] is True
    assert result["moved"] == 1
    assert result["copy_failed"] == 1
    mock_imap.store.assert_called_once()
    mock_imap.expunge.assert_called_once()


def test_report_js_inserts_accept_confirmation_without_innerhtml():
    """The accepted-label confirmation must be built with textContent, not
    innerHTML — a label containing markup would otherwise execute (XSS)."""
    html = build_html_report(date(2026, 6, 28), [], {}, {"action_required": [], "filing_suggestions": []})
    assert "sug.innerHTML" not in html
    assert "textContent" in html


def test_fetch_folder_emails_uses_one_batched_fetch(mock_imap):
    from emailSummary import fetch_folder_emails
    mock_imap.search.return_value = ("OK", [b"1 2"])

    def _batched(id_set, what):
        data = []
        for mid in id_set.split(b","):
            payload = b"From: a@x.com\r\nSubject: Hi\r\nDate: Sun, 28 Jun 2026\r\n"
            data.append((mid + b" (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {%d}" % len(payload), payload))
            data.append(b")")
        return ("OK", data)

    mock_imap.fetch.side_effect = _batched

    emails = fetch_folder_emails(mock_imap, "MailMatrixCategories/Work", "28-Jun-2026")

    assert len(emails) == 2
    assert emails[0]["from_addr"] == "a@x.com"
    mock_imap.fetch.assert_called_once()
    assert mock_imap.fetch.call_args[0][0] == b"1,2"


def test_fetch_inbox_with_body_combined_fetch_builds_snippet(mock_imap):
    from emailSummary import fetch_inbox_with_body
    mock_imap.search.return_value = ("OK", [b"1"])
    headers = b"From: a@x.com\r\nSubject: Hi\r\nDate: Sun, 28 Jun 2026\r\n"
    mock_imap.fetch.return_value = ("OK", [
        (b"1 (BODY[HEADER.FIELDS (FROM SUBJECT DATE CONTENT-TYPE)] {%d}" % len(headers), headers),
        (b" BODY[TEXT]<0> {12}", b"Plain body."),
        b")",
    ])

    emails = fetch_inbox_with_body(mock_imap, "28-Jun-2026")

    assert len(emails) == 1
    assert emails[0]["body_snippet"] == "Plain body."
    # Headers and body arrive in a single FETCH round trip
    mock_imap.fetch.assert_called_once()
