import json
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

from emailSummary import (
    ANALYSIS_MODELS,
    DEFAULT_ANALYSIS_MODEL,
    AnalysisCache,
    SummaryError,
    accept_filing,
    analysis_model,
    analyze_senders,
    analyze_with_claude,
    collect_day,
    count_day,
    deduplicate_inbox_emails,
    format_summary_text,
    open_imap,
    split_by_sender,
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


def test_analyze_with_claude_writes_token_usage_record(_token_log):
    response_json = json.dumps({"action_required": [], "filing_suggestions": []})

    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.return_value = _mock_anthropic_stream(response_json)
        analyze_with_claude([_make_email("a@b.com"), _make_email("c@d.com")], [],
                            caller="inbox-analyze")

    lines = _token_log.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["caller"] == "inbox-analyze"
    assert rec["model"] == DEFAULT_ANALYSIS_MODEL
    assert rec["email_count"] == 2
    assert rec["input_tokens"] == 123
    assert rec["output_tokens"] == 45
    # MagicMock cache fields aren't ints — they must degrade to 0, not crash
    assert rec["cache_read_tokens"] == 0
    assert rec["stop_reason"] == "end_turn"


# ── analysis model selection ──────────────────────────────────────────────────

def _stream_kwargs(configured=None):
    """Run analyze_with_claude with ANALYSIS_MODEL set to `configured` and
    return the kwargs passed to messages.stream."""
    import commonFunctions
    if configured is not None:
        commonFunctions.set_credential("ANALYSIS_MODEL", configured)
    response_json = json.dumps({"action_required": [], "filing_suggestions": []})
    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.return_value = _mock_anthropic_stream(response_json)
        analyze_with_claude([_make_email("a@b.com")], [])
    return mock_client.messages.stream.call_args.kwargs


def test_default_analysis_model_is_haiku():
    assert DEFAULT_ANALYSIS_MODEL == "claude-haiku-4-5"
    assert analysis_model() == "claude-haiku-4-5"


def test_analyze_with_claude_uses_default_model_without_thinking():
    kwargs = _stream_kwargs()
    assert kwargs["model"] == "claude-haiku-4-5"
    # Haiku 4.5 doesn't take adaptive thinking — nothing thinking-related is sent
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


def test_analyze_with_claude_uses_configured_sonnet_with_low_effort():
    kwargs = _stream_kwargs("claude-sonnet-5")
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "low"}


def test_analyze_with_claude_opus_keeps_adaptive_thinking():
    kwargs = _stream_kwargs("claude-opus-4-8")
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["thinking"] == {"type": "adaptive"}


def test_unknown_configured_model_falls_back_to_default():
    kwargs = _stream_kwargs("gpt-4o")
    assert kwargs["model"] == DEFAULT_ANALYSIS_MODEL


def test_token_usage_records_the_model_actually_used(_token_log):
    _stream_kwargs("claude-sonnet-5")
    assert json.loads(_token_log.read_text())["model"] == "claude-sonnet-5"


def test_every_analysis_model_has_label_and_params():
    for info in ANALYSIS_MODELS.values():
        assert info["label"]
        assert isinstance(info["params"], dict)


def test_analyze_with_claude_defaults_caller_to_summary(_token_log):
    response_json = json.dumps({"action_required": [], "filing_suggestions": []})

    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.return_value = _mock_anthropic_stream(response_json)
        analyze_with_claude([_make_email("a@b.com")], [])

    assert json.loads(_token_log.read_text())["caller"] == "summary"


def test_analyze_with_claude_no_usage_record_on_api_error(_token_log):
    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.stream.side_effect = anthropic.APIConnectionError(
            request=_anthropic_request())
        analyze_with_claude([_make_email("a@b.com")], [])

    assert not _token_log.exists()


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


def test_accept_filing_creates_missing_mailbox(mock_imap, tmp_path):
    # Issue #14: accepting a brand-new category must CREATE the mailbox first,
    # since IMAP COPY will not auto-create it (Fastmail/Cyrus answers NO).
    rules_file = tmp_path / "emailRules.json"
    rules_file.write_text(json.dumps({"labels": []}))
    mock_imap.search.return_value = ("OK", [b"1"])

    with patch("emailSummary.connect_to_imap", return_value=mock_imap):
        result = accept_filing(
            body={"from_addr": "doc@clinic.com", "label": "MailMatrixCategories/Medical"},
            imap_server="imap.example.com", imap_port=993,
            username="u", password="p",
            rules_path=str(rules_file),
        )

    assert result["ok"] is True
    assert result["moved"] == 1
    mock_imap.create.assert_called_once_with('"MailMatrixCategories/Medical"')
    mock_imap.copy.assert_called()


def test_accept_filing_proceeds_when_mailbox_already_exists(mock_imap, tmp_path):
    # CREATE on an existing mailbox returns NO — that must be tolerated, not fatal.
    rules_file = tmp_path / "emailRules.json"
    rules_file.write_text(json.dumps({"labels": []}))
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.create.return_value = ("NO", [b"[ALREADYEXISTS] Mailbox already exists"])

    with patch("emailSummary.connect_to_imap", return_value=mock_imap):
        result = accept_filing(
            body={"from_addr": "doc@clinic.com", "label": "MailMatrixCategories/Medical"},
            imap_server="imap.example.com", imap_port=993,
            username="u", password="p",
            rules_path=str(rules_file),
        )

    assert result["ok"] is True
    assert result["moved"] == 1


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


# ── open_imap / collect_day / count_day ───────────────────────────────────────

def test_open_imap_raises_summary_error_without_credentials(monkeypatch):
    for key in ("IMAP_SERVER", "IMAP_USERNAME", "IMAP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SummaryError):
        open_imap()


def test_collect_day_returns_live_day_without_msg_ids():
    inbox = [
        {**_make_email("a@x.com", "First"), "msg_id": b"1"},
        {**_make_email("a@x.com", "Second"), "msg_id": b"2"},
        {**_make_email("b@y.com", "Other"), "msg_id": b"3"},
    ]
    filed = [{"msg_id": b"9", "from_display": "c@z.com", "from_addr": "c@z.com",
              "subject": "Receipt", "date": "d"}]
    with (
        patch("emailSummary.get_all_labels",
              return_value=["MailMatrixCategories/Work", "MailMatrixCategories/Shopping"]),
        patch("emailSummary.fetch_folder_emails",
              side_effect=lambda imap, label, df: filed if label.endswith("Shopping") else []) as ffe,
        patch("emailSummary.fetch_inbox_with_body", return_value=inbox),
    ):
        day = collect_day(MagicMock(), date(2026, 9, 25))

    assert day["date"] == "2026-09-25"
    assert ffe.call_args_list[0].args[2] == "25-Sep-2026"
    assert list(day["filed"]) == ["MailMatrixCategories/Shopping"]  # empty labels omitted
    assert day["counts"] == {"filed": 1, "unfiled": 3, "senders": 2}
    assert [e["from_addr"] for e in day["inbox"]] == ["a@x.com", "b@y.com"]
    assert day["inbox"][0]["count"] == 2
    # msg_ids are per-connection bytes — never leak into the JSON
    assert all("msg_id" not in e for e in day["inbox"])
    assert all("msg_id" not in e for e in day["filed"]["MailMatrixCategories/Shopping"])
    json.dumps(day)


def test_count_day_uses_search_only(mock_imap):
    selected = []
    mock_imap.select.side_effect = lambda folder, readonly=False: (selected.append((folder, readonly)) or ("OK", [b"1"]))
    hits = {'"MailMatrixCategories/Work"': b"1 2", '"INBOX"': b"4 5 6"}
    mock_imap.search.side_effect = lambda charset, crit: ("OK", [hits.get(selected[-1][0], b"")])

    counts = count_day(mock_imap, ["MailMatrixCategories/Work", "MailMatrixCategories/Empty"], date(2026, 9, 25))

    assert counts == {"filed": 2, "unfiled": 3}
    assert all(readonly for _, readonly in selected)
    assert mock_imap.search.call_args.args[1] == "ON 25-Sep-2026"
    mock_imap.fetch.assert_not_called()


def test_count_day_skips_unselectable_folder(mock_imap):
    mock_imap.select.return_value = ("NO", [b"nope"])
    assert count_day(mock_imap, ["MailMatrixCategories/Gone"], date(2026, 9, 25)) == {"filed": 0, "unfiled": 0}
    mock_imap.search.assert_not_called()


# ── AnalysisCache / split_by_sender ───────────────────────────────────────────

def test_cache_key_changes_with_everything_the_result_depends_on():
    em = _make_email("a@x.com")
    base = AnalysisCache.key("m", ["L1", "L2"], em)
    assert AnalysisCache.key("m", ["L2", "L1"], em) == base  # label order doesn't matter
    assert AnalysisCache.key("other", ["L1", "L2"], em) != base
    assert AnalysisCache.key("m", ["L1"], em) != base
    assert AnalysisCache.key("m", ["L1", "L2"], {**em, "subject": "New"}) != base
    assert AnalysisCache.key("m", ["L1", "L2"], {**em, "count": 2}) != base


def test_cache_get_put_and_ttl():
    cache = AnalysisCache(ttl=60)
    cache.put("k", {"action": None, "suggestion": None})
    stored_at, result = cache.get("k")
    assert result == {"action": None, "suggestion": None}
    with patch("emailSummary.time.time", return_value=stored_at + 61):
        assert cache.get("k") is None
    assert len(cache) == 0


def test_cache_evicts_oldest_past_max_entries():
    cache = AnalysisCache(ttl=float("inf"), max_entries=2)  # fake timestamps are "old"
    with patch("emailSummary.time.time", side_effect=[1.0, 2.0, 3.0]):
        cache.put("a", {})
        cache.put("b", {})
        cache.put("c", {})
    assert cache.get("a") is None
    assert cache.get("b") is not None and cache.get("c") is not None


def test_split_by_sender_maps_items_and_drops_bad_indices():
    analysis = {
        "action_required": [{"index": 2, "reason": "reply"}, {"index": 0}, {"index": 9},
                            {"index": "1"}, {"index": True}, {"reason": "no index"}, "junk"],
        "filing_suggestions": [{"index": 1, "suggested_label": "MailMatrixCategories/Work"}],
    }
    per = split_by_sender(analysis, 2)
    assert per[0] == {"action": None, "suggestion": {"suggested_label": "MailMatrixCategories/Work"}}
    assert per[1] == {"action": {"reason": "reply"}, "suggestion": None}


# ── analyze_senders ───────────────────────────────────────────────────────────

class _FakeAnalyzer:
    """Stands in for analyze_with_claude: suggests Work for everyone and flags
    subjects starting with "URGENT"; records every batch it's given."""

    def __init__(self, fail=False):
        self.batches = []
        self.fail = fail

    def __call__(self, batch, labels, caller="summary"):
        self.batches.append(([e["from_addr"] for e in batch], caller))
        if self.fail:
            return {"action_required": [], "filing_suggestions": [], "_error": "boom"}
        return {
            "action_required": [{"index": i, "from": e["from_addr"], "reason": "urgent"}
                                for i, e in enumerate(batch, 1) if e["subject"].startswith("URGENT")],
            "filing_suggestions": [{"index": i, "suggested_label": "MailMatrixCategories/Work",
                                    "is_new_label": False, "reason": "work"}
                                   for i, _ in enumerate(batch, 1)],
        }


def _senders(*addrs):
    return [_make_email(a, subject="URGENT reply" if a.startswith("u") else "Hello") for a in addrs]


def test_analyze_senders_batches_and_indexes_into_full_list():
    fake, cache = _FakeAnalyzer(), AnalysisCache()
    emails = _senders("a@x.com", "u@x.com", "c@x.com")
    progress = []
    out = analyze_senders(emails, ["MailMatrixCategories/Work"], cache=cache, batch_size=2,
                          analyze_fn=fake, caller="summary",
                          progress_cb=lambda *a: progress.append(a))

    assert [b[0] for b in fake.batches] == [["a@x.com", "u@x.com"], ["c@x.com"]]
    assert all(caller == "summary" for _, caller in fake.batches)
    assert [s["index"] for s in out["filing_suggestions"]] == [1, 2, 3]
    assert [(a["index"], a["from"]) for a in out["action_required"]] == [(2, "u@x.com")]
    assert out["analyzed"] == 3 and out["cached"] == 0 and out["error"] is None
    assert out["model"] == DEFAULT_ANALYSIS_MODEL and out["analyzed_at"] is not None
    assert progress[0] == ("analyzing", 0, 3) and progress[-1] == ("analyzing", 3, 3)


def test_analyze_senders_second_view_is_served_from_cache():
    fake, cache = _FakeAnalyzer(), AnalysisCache()
    emails = _senders("a@x.com", "u@x.com")
    first = analyze_senders(emails, [], cache=cache, analyze_fn=fake)
    fake.batches.clear()

    second = analyze_senders(emails, [], cache=cache, analyze_fn=fake)

    assert fake.batches == []  # no Claude call at all
    assert second["cached"] == 2 and second["analyzed"] == 0
    assert second["action_required"] == first["action_required"]
    assert second["filing_suggestions"] == first["filing_suggestions"]


def test_analyze_senders_only_analyzes_new_senders():
    fake, cache = _FakeAnalyzer(), AnalysisCache()
    analyze_senders(_senders("a@x.com"), [], cache=cache, analyze_fn=fake)
    fake.batches.clear()

    out = analyze_senders(_senders("a@x.com", "b@x.com"), [], cache=cache, analyze_fn=fake)

    assert [b[0] for b in fake.batches] == [["b@x.com"]]
    assert out["cached"] == 1 and out["analyzed"] == 1
    assert [s["index"] for s in out["filing_suggestions"]] == [1, 2]


def test_analyze_senders_force_bypasses_cache():
    fake, cache = _FakeAnalyzer(), AnalysisCache()
    emails = _senders("a@x.com", "b@x.com")
    analyze_senders(emails, [], cache=cache, analyze_fn=fake)
    fake.batches.clear()

    out = analyze_senders(emails, [], cache=cache, analyze_fn=fake, force=True)

    assert [b[0] for b in fake.batches] == [["a@x.com", "b@x.com"]]
    assert out["cached"] == 0


def test_analyze_senders_model_switch_reanalyzes():
    import commonFunctions
    fake, cache = _FakeAnalyzer(), AnalysisCache()
    emails = _senders("a@x.com")
    analyze_senders(emails, [], cache=cache, analyze_fn=fake)
    commonFunctions.set_credential("ANALYSIS_MODEL", "claude-sonnet-5")

    out = analyze_senders(emails, [], cache=cache, analyze_fn=fake)

    assert len(fake.batches) == 2
    assert out["model"] == "claude-sonnet-5"


def test_analyze_senders_failed_batch_is_reported_not_cached():
    cache = AnalysisCache()
    out = analyze_senders(_senders("a@x.com"), [], cache=cache, analyze_fn=_FakeAnalyzer(fail=True))
    assert out["error"] == "boom"
    assert out["filing_suggestions"] == [] and out["analyzed_at"] is None
    assert len(cache) == 0

    retry = _FakeAnalyzer()
    out = analyze_senders(_senders("a@x.com"), [], cache=cache, analyze_fn=retry)
    assert len(retry.batches) == 1 and out["error"] is None


def test_analyze_senders_cancel_between_batches():
    import threading
    cancel = threading.Event()
    fake = _FakeAnalyzer()

    def analyze_then_cancel(batch, labels, caller="summary"):
        cancel.set()
        return fake(batch, labels, caller)

    out = analyze_senders(_senders("a@x.com", "b@x.com"), [], cache=AnalysisCache(), batch_size=1,
                          analyze_fn=analyze_then_cancel, cancel_event=cancel)
    assert out == {"cancelled": True}
    assert len(fake.batches) == 1


def test_analyze_senders_defaults_to_real_analyzer_and_shared_cache():
    """With no analyze_fn/cache, analyze_with_claude + ANALYSIS_CACHE are used."""
    import emailSummary
    response_json = json.dumps({"action_required": [], "filing_suggestions": [
        {"index": 1, "suggested_label": "MailMatrixCategories/Work", "is_new_label": False, "reason": "r"}]})
    with patch("emailSummary.anthropic.Anthropic") as MockAnthropic:
        mock_client = MockAnthropic.return_value
        mock_client.messages.stream.return_value = _mock_anthropic_stream(response_json)
        out = analyze_senders(_senders("a@x.com"), ["MailMatrixCategories/Work"])
        again = analyze_senders(_senders("a@x.com"), ["MailMatrixCategories/Work"])
    assert out["filing_suggestions"][0]["suggested_label"] == "MailMatrixCategories/Work"
    assert mock_client.messages.stream.call_count == 1
    assert again["cached"] == 1 and len(emailSummary.ANALYSIS_CACHE) == 1


def test_analyze_senders_empty_inbox_makes_no_call():
    fake = _FakeAnalyzer()
    out = analyze_senders([], [], cache=AnalysisCache(), analyze_fn=fake)
    assert fake.batches == [] and out["action_required"] == [] and out["analyzed_at"] is None


# ── CLI ───────────────────────────────────────────────────────────────────────

def _day(inbox=None, filed=None):
    inbox = inbox if inbox is not None else [_make_email("a@x.com", "Invoice", count=2)]
    filed = filed if filed is not None else {"MailMatrixCategories/Work": [_make_email("w@x.com")]}
    for em in inbox:
        em.pop("msg_id", None)
    return {
        "date": "2026-09-25", "labels": ["MailMatrixCategories/Work"], "filed": filed, "inbox": inbox,
        "counts": {"filed": sum(len(v) for v in filed.values()),
                   "unfiled": sum(e.get("count", 1) for e in inbox), "senders": len(inbox)},
    }


def test_format_summary_text_lists_every_section():
    analysis = {
        "action_required": [{"index": 1, "subject": "Invoice", "from": "a@x.com", "reason": "Pay it"}],
        "filing_suggestions": [{"index": 1, "suggested_label": "MailMatrixCategories/Bills",
                                "is_new_label": True, "reason": "billing"}],
        "error": None,
    }
    text = format_summary_text(_day(), analysis)
    assert "# Email summary — 2026-09-25" in text
    assert "3 processed · 1 need attention · 2 unfiled (1 senders) · 1 filed" in text
    assert "- Invoice — a@x.com: Pay it" in text
    assert "- Invoice (2×) — a@x.com" in text
    assert "→ MailMatrixCategories/Bills (new label): billing" in text
    assert "- Work: 1" in text


def test_format_summary_text_empty_day_and_error():
    text = format_summary_text(_day(inbox=[], filed={}),
                               {"action_required": [], "filing_suggestions": [], "error": "No credits"})
    assert "AI analysis unavailable: No credits" in text
    assert text.count("- (none)") == 3


def test_main_prints_summary_and_writes_no_files(tmp_path, monkeypatch, capsys):
    import emailSummary
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["emailSummary.py", "2026-09-25"])
    imap = MagicMock()
    analysis = {"action_required": [], "filing_suggestions": [], "error": None}
    with (
        patch("emailSummary.setup_logging"),
        patch("emailSummary.open_imap", return_value=imap),
        patch("emailSummary.collect_day", return_value=_day()) as collect,
        patch("emailSummary.analyze_senders", return_value=analysis),
    ):
        emailSummary.main()

    assert collect.call_args.args[1] == date(2026, 9, 25)
    imap.logout.assert_called_once()
    assert "# Email summary — 2026-09-25" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_main_rejects_bad_date(monkeypatch):
    import emailSummary
    monkeypatch.setattr(sys, "argv", ["emailSummary.py", "yesterday"])
    with patch("emailSummary.setup_logging"), pytest.raises(SystemExit) as exc:
        emailSummary.main()
    assert exc.value.code == 1


def test_main_exits_when_credentials_missing(monkeypatch):
    import emailSummary
    monkeypatch.setattr(sys, "argv", ["emailSummary.py"])
    with (
        patch("emailSummary.setup_logging"),
        patch("emailSummary.open_imap", side_effect=SummaryError("no creds")),
        pytest.raises(SystemExit) as exc,
    ):
        emailSummary.main()
    assert exc.value.code == 1
