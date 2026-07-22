import imaplib
import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import commonFunctions
from commonFunctions import (
    build_rule_groups,
    collapse_domain_rule,
    connect_to_imap,
    convert_domain_rule,
    dashboard_stats,
    decode_header_value,
    delete_rule,
    extract_body_snippet,
    extract_email_address,
    full_label_name,
    get_all_labels,
    imap_call,
    imap_date,
    move_imap_messages,
    parse_headers,
    resolve_duplicate_address,
    summary_files,
    update_sender_rule,
)


# ── extract_email_address ─────────────────────────────────────────────────────

def test_extract_bare_address():
    assert extract_email_address("test@example.com") == "test@example.com"


def test_extract_display_name_form():
    assert extract_email_address("Alice Smith <alice@example.com>") == "alice@example.com"


def test_extract_lowercases_result():
    assert extract_email_address("ALICE@EXAMPLE.COM") == "alice@example.com"


def test_extract_display_name_lowercases():
    assert extract_email_address("Bob <BOB@WORK.COM>") == "bob@work.com"


def test_extract_strips_whitespace():
    assert extract_email_address("  user@example.com  ") == "user@example.com"


# ── imap_date ─────────────────────────────────────────────────────────────────

def test_imap_date_format():
    assert imap_date(date(2026, 6, 28)) == "28-Jun-2026"


def test_imap_date_single_digit_day():
    assert imap_date(date(2026, 1, 5)) == "5-Jan-2026"


def test_imap_date_december():
    assert imap_date(date(2025, 12, 31)) == "31-Dec-2025"


# ── parse_headers ─────────────────────────────────────────────────────────────

def test_parse_headers_basic():
    raw = "From: Alice <alice@example.com>\r\nSubject: Hello\r\nDate: Mon, 28 Jun 2026 10:00:00 +0000\r\n"
    result = parse_headers(raw)
    assert result["from"] == "Alice <alice@example.com>"
    assert result["subject"] == "Hello"
    assert "2026" in result["date"]


def test_parse_headers_missing_fields_default_empty():
    raw = "From: sender@example.com\r\n"
    result = parse_headers(raw)
    assert result["from"] == "sender@example.com"
    assert result["subject"] == ""
    assert result["date"] == ""


def test_parse_headers_ignores_unknown_fields():
    raw = "From: a@b.com\r\nX-Custom-Header: some value\r\nSubject: Test\r\n"
    result = parse_headers(raw)
    assert "x-custom-header" not in result
    assert result["from"] == "a@b.com"
    assert result["subject"] == "Test"


def test_parse_headers_folded_value():
    raw = "Subject: Long\r\n subject continued\r\nFrom: a@b.com\r\n"
    result = parse_headers(raw)
    assert "Long" in result["subject"]
    assert "continued" in result["subject"]


# ── decode_header_value ───────────────────────────────────────────────────────

def test_decode_header_value_plain():
    assert decode_header_value("Hello World") == "Hello World"


def test_decode_header_value_strips_whitespace():
    assert decode_header_value("  Hello  ") == "Hello"


def test_decode_header_value_utf8_encoded():
    # =?utf-8?b? is base64-encoded UTF-8
    encoded = "=?utf-8?b?SGVsbG8gV29ybGQ=?="  # "Hello World"
    assert decode_header_value(encoded) == "Hello World"


# ── get_all_labels ────────────────────────────────────────────────────────────

FOLDER_BYTES = [
    b'(\\HasNoChildren) "/" "MailMatrixCategories/Work"',
    b'(\\HasNoChildren) "/" "MailMatrixCategories/Shopping"',
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren) "/" "Sent"',
]


def test_get_all_labels_no_filter(mock_imap):
    mock_imap.list.return_value = ("OK", FOLDER_BYTES)
    labels = get_all_labels(mock_imap)
    assert "INBOX" in labels
    assert "Sent" in labels
    assert "MailMatrixCategories/Work" in labels
    assert sorted(labels) == labels  # returned sorted


def test_get_all_labels_with_parent_filter(mock_imap):
    mock_imap.list.return_value = ("OK", FOLDER_BYTES)
    labels = get_all_labels(mock_imap, parent_label="MailMatrixCategories")
    assert labels == ["MailMatrixCategories/Shopping", "MailMatrixCategories/Work"]
    assert "INBOX" not in labels
    assert "Sent" not in labels


def test_get_all_labels_empty_mailbox(mock_imap):
    mock_imap.list.return_value = ("OK", [])
    assert get_all_labels(mock_imap) == []


def test_get_all_labels_returns_empty_on_error(mock_imap):
    mock_imap.list.return_value = ("NO", [b"Error"])
    assert get_all_labels(mock_imap) == []


# ── imap_call ─────────────────────────────────────────────────────────────────

def test_imap_call_returns_on_success():
    fn = MagicMock(return_value=("OK", [b"data"]))
    status, data = imap_call(fn)
    assert status == "OK"
    assert data == [b"data"]
    fn.assert_called_once()


def test_imap_call_retries_on_rate_limit_error():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise imaplib.IMAP4.error("rate limit exceeded")
        return ("OK", [b"success"])

    with patch("commonFunctions.time.sleep"):
        status, data = imap_call(flaky)

    assert status == "OK"
    assert calls["n"] == 3


def test_imap_call_raises_after_max_retries():
    def always_rate_limited():
        raise imaplib.IMAP4.error("throttled by server")

    with patch("commonFunctions.time.sleep"):
        with pytest.raises(imaplib.IMAP4.error, match="throttled"):
            imap_call(always_rate_limited)


def test_imap_call_logs_error_when_giving_up_after_max_retries(caplog):
    def always_rate_limited():
        raise imaplib.IMAP4.error("throttled by server")

    with patch("commonFunctions.time.sleep"), caplog.at_level("ERROR"):
        with pytest.raises(imaplib.IMAP4.error):
            imap_call(always_rate_limited)

    assert any(
        r.levelname == "ERROR" and "giving up" in r.message.lower() and "rate-limited" in r.message.lower()
        for r in caplog.records
    )


def test_imap_call_raises_non_rate_limit_immediately():
    calls = {"n": 0}

    def non_rate_limit():
        calls["n"] += 1
        raise imaplib.IMAP4.error("LOGIN failed: invalid credentials")

    with pytest.raises(imaplib.IMAP4.error, match="invalid credentials"):
        imap_call(non_rate_limit)

    assert calls["n"] == 1  # no retry


def test_imap_call_logs_error_for_non_rate_limit_failure(caplog):
    def non_rate_limit():
        raise imaplib.IMAP4.error("LOGIN failed: invalid credentials")

    with caplog.at_level("ERROR"):
        with pytest.raises(imaplib.IMAP4.error):
            imap_call(non_rate_limit)

    assert any(r.levelname == "ERROR" and "IMAP call failed" in r.message for r in caplog.records)


def test_imap_call_retries_on_no_status_with_rate_limit_phrase():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            return ("NO", [b"rate limit exceeded"])
        return ("OK", [b"data"])

    with patch("commonFunctions.time.sleep"):
        status, data = imap_call(fn)

    assert status == "OK"
    assert calls["n"] == 2


# ── connect_to_imap ───────────────────────────────────────────────────────────

def test_connect_to_imap_calls_ssl_and_login():
    mock_imap_instance = MagicMock()
    mock_imap_instance.login.return_value = ("OK", [b"Logged in"])

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap_instance) as MockSSL:
        result = connect_to_imap("imap.gmail.com", "user@gmail.com", "password", 993)

    MockSSL.assert_called_once_with("imap.gmail.com", 993)
    mock_imap_instance.login.assert_called_once_with("user@gmail.com", "password")
    assert result is mock_imap_instance


def test_connect_to_imap_logs_success_at_info(caplog):
    mock_imap_instance = MagicMock()
    mock_imap_instance.login.return_value = ("OK", [b"Logged in"])

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap_instance), caplog.at_level("INFO"):
        connect_to_imap("imap.gmail.com", "user@gmail.com", "password", 993)

    assert any("Connecting to IMAP" in r.message for r in caplog.records)
    assert any("login succeeded" in r.message for r in caplog.records)


def test_connect_to_imap_logs_rate_limit_error_distinctly(caplog):
    mock_imap_instance = MagicMock()
    mock_imap_instance.login.side_effect = imaplib.IMAP4.error("Too many login attempts, slow down")

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap_instance), caplog.at_level("ERROR"):
        with pytest.raises(imaplib.IMAP4.error):
            connect_to_imap("imap.gmail.com", "user@gmail.com", "password", 993)

    assert any(
        r.levelname == "ERROR" and "rate-limited" in r.message.lower()
        for r in caplog.records
    )


def test_connect_to_imap_logs_non_rate_limit_error_distinctly(caplog):
    mock_imap_instance = MagicMock()
    mock_imap_instance.login.side_effect = imaplib.IMAP4.error("Invalid credentials")

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap_instance), caplog.at_level("ERROR"):
        with pytest.raises(imaplib.IMAP4.error):
            connect_to_imap("imap.gmail.com", "user@gmail.com", "password", 993)

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("login failed" in r.message.lower() for r in errors)
    assert not any("rate-limited" in r.message.lower() for r in errors)


# ── full_label_name ───────────────────────────────────────────────────────────

def test_full_label_name_prefixes_bare_name():
    assert full_label_name("Work") == "MailMatrixCategories/Work"


def test_full_label_name_leaves_qualified_name_alone():
    assert full_label_name("MailMatrixCategories/Work") == "MailMatrixCategories/Work"


# ── build_rule_groups ─────────────────────────────────────────────────────────

def test_build_rule_groups_groups_by_domain(rules_data):
    result = build_rule_groups(rules_data)
    domains = {g["domain"] for g in result["groups"]}
    assert domains == {"work.com", "amazon.com", "shop.example.com"}
    assert result["label_names"] == ["Shopping", "Work"]
    assert result["total_senders"] == 3
    assert result["total_domain_rules"] == 1


def test_build_rule_groups_can_convert_true_when_no_domain_rule_exists(rules_data):
    result = build_rule_groups(rules_data)
    work_group = next(g for g in result["groups"] if g["domain"] == "work.com")
    assert work_group["can_convert"] is True


def test_build_rule_groups_can_convert_false_when_domain_rule_already_exists(rules_data):
    result = build_rule_groups(rules_data)
    shopping_group = next(g for g in result["groups"] if g["domain"] == "shop.example.com")
    assert shopping_group["can_convert"] is False


def test_build_rule_groups_no_domain_bucket_cannot_convert():
    data = {"labels": [{"labelName": "MailMatrixCategories/Misc", "emailAddresses": ["nodothere"], "emailDomains": []}]}
    result = build_rule_groups(data)
    group = next(g for g in result["groups"] if g["domain"] == "__no_domain__")
    assert group["can_convert"] is False


def test_build_rule_groups_empty_labels():
    result = build_rule_groups({"labels": []})
    assert result == {"groups": [], "label_names": [], "total_senders": 0, "total_domain_rules": 0}


def test_build_rule_groups_sorted_by_sender_count_descending(rules_data):
    result = build_rule_groups(rules_data)
    domains_in_order = [g["domain"] for g in result["groups"]]
    # work.com: 2 senders, amazon.com: 1 sender, shop.example.com: 0 senders (domain rule only)
    assert domains_in_order == ["work.com", "amazon.com", "shop.example.com"]


# ── delete_rule ────────────────────────────────────────────────────────────────

def test_delete_rule_removes_sender(rules_data):
    changed = delete_rule(rules_data, "sender", "MailMatrixCategories/Work", address="boss@work.com")
    assert changed is True
    assert "boss@work.com" not in rules_data["labels"][0]["emailAddresses"]


def test_delete_rule_removes_domain(rules_data):
    changed = delete_rule(rules_data, "domain", "MailMatrixCategories/Shopping", domain="shop.example.com")
    assert changed is True
    assert "shop.example.com" not in rules_data["labels"][1]["emailDomains"]


def test_delete_rule_returns_false_when_not_found(rules_data):
    changed = delete_rule(rules_data, "sender", "MailMatrixCategories/Work", address="nobody@nowhere.com")
    assert changed is False


def test_delete_rule_returns_false_for_unknown_label(rules_data):
    changed = delete_rule(rules_data, "sender", "MailMatrixCategories/Nope", address="boss@work.com")
    assert changed is False


# ── update_sender_rule ─────────────────────────────────────────────────────────

def test_update_sender_rule_moves_to_existing_label(rules_data):
    update_sender_rule(rules_data, "boss@work.com", "MailMatrixCategories/Work", "MailMatrixCategories/Shopping")
    assert "boss@work.com" not in rules_data["labels"][0]["emailAddresses"]
    assert "boss@work.com" in rules_data["labels"][1]["emailAddresses"]


def test_update_sender_rule_creates_new_label(rules_data):
    update_sender_rule(rules_data, "boss@work.com", "MailMatrixCategories/Work", "MailMatrixCategories/NewLabel")
    new_entry = next(e for e in rules_data["labels"] if e["labelName"] == "MailMatrixCategories/NewLabel")
    assert new_entry["emailAddresses"] == ["boss@work.com"]
    assert new_entry["emailDomains"] == []


def test_update_sender_rule_no_duplicate_if_already_present(rules_data):
    rules_data["labels"][1]["emailAddresses"].append("boss@work.com")
    update_sender_rule(rules_data, "boss@work.com", "MailMatrixCategories/Work", "MailMatrixCategories/Shopping")
    assert rules_data["labels"][1]["emailAddresses"].count("boss@work.com") == 1


# ── convert_domain_rule ────────────────────────────────────────────────────────

def test_convert_domain_rule_adds_domain_and_removes_subsumed_senders(rules_data):
    convert_domain_rule(rules_data, "work.com", "MailMatrixCategories/Work")
    entry = rules_data["labels"][0]
    assert "work.com" in entry["emailDomains"]
    assert entry["emailAddresses"] == []


def test_convert_domain_rule_creates_new_label_entry():
    data = {"labels": []}
    convert_domain_rule(data, "example.com", "MailMatrixCategories/New")
    entry = data["labels"][0]
    assert entry["labelName"] == "MailMatrixCategories/New"
    assert entry["emailDomains"] == ["example.com"]
    assert entry["emailAddresses"] == []


def test_convert_domain_rule_no_duplicate_domain(rules_data):
    convert_domain_rule(rules_data, "shop.example.com", "MailMatrixCategories/Shopping")
    assert rules_data["labels"][1]["emailDomains"].count("shop.example.com") == 1


def test_convert_domain_rule_leaves_other_labels_by_default():
    data = {
        "labels": [
            {"labelName": "MailMatrixCategories/Work", "emailAddresses": ["a@work.com"], "emailDomains": []},
            {"labelName": "MailMatrixCategories/Personal", "emailAddresses": ["b@work.com"], "emailDomains": []},
        ]
    }
    convert_domain_rule(data, "work.com", "MailMatrixCategories/Work")
    personal = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Personal")
    assert personal["emailAddresses"] == ["b@work.com"]


def test_convert_domain_rule_purges_other_labels_when_requested():
    data = {
        "labels": [
            {"labelName": "MailMatrixCategories/Work", "emailAddresses": ["a@work.com"], "emailDomains": []},
            {"labelName": "MailMatrixCategories/Personal", "emailAddresses": ["b@work.com", "c@elsewhere.com"], "emailDomains": []},
        ]
    }
    convert_domain_rule(data, "work.com", "MailMatrixCategories/Work", purge_other_labels=True)
    work = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    personal = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Personal")
    assert work["emailDomains"] == ["work.com"]
    assert work["emailAddresses"] == []
    # b@work.com purged (matches the new domain rule); c@elsewhere.com is unrelated and stays
    assert personal["emailAddresses"] == ["c@elsewhere.com"]


# ── move_imap_messages ─────────────────────────────────────────────────────────

def test_move_imap_messages_moves_matching_messages(mock_imap):
    mock_imap.search.return_value = ("OK", [b"1 2"])
    with patch("commonFunctions.connect_to_imap", return_value=mock_imap):
        result = move_imap_messages(
            "sender@example.com", "MailMatrixCategories/Old", "MailMatrixCategories/New",
            imap_server="imap.example.com", imap_port=993, username="user", password="pw",
        )
    assert result == {"ok": True, "moved": 2, "copy_failed": 0}
    mock_imap.expunge.assert_called_once()
    mock_imap.logout.assert_called_once()


def test_move_imap_messages_rejects_invalid_address(mock_imap):
    with patch("commonFunctions.connect_to_imap", return_value=mock_imap):
        result = move_imap_messages(
            "not-an-email", "MailMatrixCategories/Old", "MailMatrixCategories/New",
            imap_server="imap.example.com", imap_port=993, username="user", password="pw",
        )
    assert result["ok"] is False
    mock_imap.select.assert_not_called()


def test_move_imap_messages_skips_when_credentials_missing(mock_imap):
    with patch("commonFunctions.connect_to_imap", return_value=mock_imap) as mock_connect:
        result = move_imap_messages(
            "sender@example.com", "MailMatrixCategories/Old", "MailMatrixCategories/New",
            imap_server="", imap_port=993, username="", password="",
        )
    assert result == {"ok": False, "error": "IMAP not configured", "moved": 0}
    mock_connect.assert_not_called()


def test_move_imap_messages_no_messages_found(mock_imap):
    mock_imap.search.return_value = ("OK", [b""])
    with patch("commonFunctions.connect_to_imap", return_value=mock_imap):
        result = move_imap_messages(
            "sender@example.com", "MailMatrixCategories/Old", "MailMatrixCategories/New",
            imap_server="imap.example.com", imap_port=993, username="user", password="pw",
        )
    assert result == {"ok": True, "moved": 0, "copy_failed": 0}
    mock_imap.expunge.assert_not_called()


# ── summary_files ──────────────────────────────────────────────────────────────

def test_summary_files_empty_dir_missing(tmp_path):
    assert summary_files(tmp_path / "does-not-exist") == []


def test_summary_files_parses_date_and_label(tmp_path):
    (tmp_path / "email_summary_2026-07-16.html").write_text("<html></html>")
    (tmp_path / "email_summary_2026-07-01.html").write_text("<html></html>")
    (tmp_path / "not_a_summary.html").write_text("<html></html>")

    files = summary_files(tmp_path)

    assert [f["date"] for f in files] == ["2026-07-16", "2026-07-01"]  # newest first
    assert files[0]["filename"] == "email_summary_2026-07-16.html"
    assert files[0]["label"] == "July 16, 2026"


def test_summary_files_falls_back_to_raw_date_part_on_parse_failure(tmp_path):
    (tmp_path / "email_summary_not-a-date.html").write_text("<html></html>")
    files = summary_files(tmp_path)
    assert files[0]["label"] == "not-a-date"


def test_summary_files_reads_stats_from_json_sidecar(tmp_path):
    (tmp_path / "email_summary_2026-07-16.html").write_text("<html></html>")
    (tmp_path / "email_summary_2026-07-16.json").write_text(json.dumps({
        "processed": 42, "need_attention": 3, "unfiled": 5, "filed": 37,
        "generated_at": "2026-07-16T09:05:00",
    }))
    files = summary_files(tmp_path)
    f = files[0]
    assert f["processed"] == 42
    assert f["need_attention"] == 3
    assert f["unfiled"] == 5
    assert f["filed"] == 37
    assert f["generated_at"] == "Jul 16, 2026 at 09:05 AM"


def test_summary_files_no_stats_without_sidecar(tmp_path):
    (tmp_path / "email_summary_2026-07-16.html").write_text("<html></html>")
    files = summary_files(tmp_path)
    assert "processed" not in files[0]
    assert "generated_at" not in files[0]


def test_summary_files_tolerates_malformed_sidecar(tmp_path):
    (tmp_path / "email_summary_2026-07-16.html").write_text("<html></html>")
    (tmp_path / "email_summary_2026-07-16.json").write_text("{not valid json")
    files = summary_files(tmp_path)
    # Doesn't blow up; just no stats surfaced for this entry
    assert files[0]["filename"] == "email_summary_2026-07-16.html"
    assert "processed" not in files[0]


# ── dashboard_stats ────────────────────────────────────────────────────────────

def test_dashboard_stats_counts_labels_and_rules(rules_data, tmp_path):
    stats = dashboard_stats(rules_data, tmp_path, date(2026, 7, 16))
    assert stats["label_count"] == 2
    assert stats["rules_count"] == 4  # 2 Work senders + 1 Shopping sender + 1 Shopping domain
    assert stats["summary_count"] == 0


def test_dashboard_stats_recent_days_labels_today_and_yesterday(tmp_path):
    stats = dashboard_stats({"labels": []}, tmp_path, date(2026, 7, 16))
    days = stats["recent_days"]
    assert len(days) == 7
    assert days[0] == {
        "date": "2026-07-16", "label": "Today", "has_summary": False, "filename": None,
        "processed": None, "need_attention": None, "unfiled": None, "generated_at": None,
    }
    assert days[1]["label"] == "Yesterday"
    assert days[1]["date"] == "2026-07-15"


def test_dashboard_stats_marks_has_summary_and_filename(tmp_path):
    (tmp_path / "email_summary_2026-07-16.html").write_text("<html></html>")
    stats = dashboard_stats({"labels": []}, tmp_path, date(2026, 7, 16))
    assert stats["summary_count"] == 1
    assert stats["recent_days"][0]["has_summary"] is True
    assert stats["recent_days"][0]["filename"] == "email_summary_2026-07-16.html"


def test_dashboard_stats_recent_days_carries_stats_from_sidecar(tmp_path):
    (tmp_path / "email_summary_2026-07-16.html").write_text("<html></html>")
    (tmp_path / "email_summary_2026-07-16.json").write_text(json.dumps({
        "processed": 42, "need_attention": 3, "unfiled": 5, "filed": 37,
        "generated_at": "2026-07-16T09:05:00",
    }))
    stats = dashboard_stats({"labels": []}, tmp_path, date(2026, 7, 16))
    today = stats["recent_days"][0]
    assert today["processed"] == 42
    assert today["need_attention"] == 3
    assert today["unfiled"] == 5
    assert today["generated_at"] == "Jul 16, 2026 at 09:05 AM"


def test_dashboard_stats_recent_days_without_summary_have_none_stats(tmp_path):
    stats = dashboard_stats({"labels": []}, tmp_path, date(2026, 7, 16))
    today = stats["recent_days"][0]
    assert today["processed"] is None
    assert today["need_attention"] is None
    assert today["unfiled"] is None
    assert today["generated_at"] is None


def test_dashboard_stats_custom_default_is_one_day_before_oldest_recent_day(tmp_path):
    stats = dashboard_stats({"labels": []}, tmp_path, date(2026, 7, 16))
    oldest = stats["recent_days"][-1]["date"]
    assert oldest == "2026-07-10"
    assert stats["custom_default"] == "2026-07-09"


# ── collapse_domain_rule ───────────────────────────────────────────────────────

def test_collapse_domain_rule_removes_matching_addresses():
    data = {
        "labels": [
            {
                "labelName": "MailMatrixCategories/Work",
                "emailAddresses": ["alice@work.com", "bob@work.com"],
                "emailDomains": ["work.com"],
            }
        ]
    }
    removed = collapse_domain_rule(data, "work.com")
    assert removed == 2
    assert data["labels"][0]["emailAddresses"] == []


def test_collapse_domain_rule_removes_cross_label_addresses():
    data = {
        "labels": [
            {"labelName": "MailMatrixCategories/Work", "emailAddresses": [], "emailDomains": ["work.com"]},
            {"labelName": "MailMatrixCategories/VIP", "emailAddresses": ["vip@work.com"], "emailDomains": []},
        ]
    }
    removed = collapse_domain_rule(data, "work.com")
    assert removed == 1
    vip = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/VIP")
    assert "vip@work.com" not in vip["emailAddresses"]


def test_collapse_domain_rule_returns_none_when_no_domain_rule_exists(rules_data):
    assert collapse_domain_rule(rules_data, "no-such-domain.com") is None


# ── resolve_duplicate_address ──────────────────────────────────────────────────

def test_resolve_duplicate_address_removes_from_other_labels():
    data = {
        "labels": [
            {"labelName": "MailMatrixCategories/Work", "emailAddresses": ["shared@example.com"], "emailDomains": []},
            {"labelName": "MailMatrixCategories/VIP", "emailAddresses": ["shared@example.com"], "emailDomains": []},
        ]
    }
    removed = resolve_duplicate_address(data, "shared@example.com", "MailMatrixCategories/Work")
    assert removed == 1
    work = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    vip = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/VIP")
    assert "shared@example.com" in work["emailAddresses"]
    assert "shared@example.com" not in vip["emailAddresses"]


def test_resolve_duplicate_address_no_op_when_not_duplicated(rules_data):
    removed = resolve_duplicate_address(rules_data, "boss@work.com", "MailMatrixCategories/Work")
    assert removed == 0


# ── extract_body_snippet ───────────────────────────────────────────────────────

def test_extract_body_snippet_plain_message_no_content_type():
    header_bytes = b"From: a@b.com\r\nSubject: Hi\r\n\r\n"
    body_bytes = b"Just plain text here."
    assert extract_body_snippet(header_bytes, body_bytes) == "Just plain text here."


def test_extract_body_snippet_multipart_prefers_decoded_plain_text_over_mime_markup():
    header_bytes = b'Content-Type: multipart/alternative; boundary="BOUNDARY123"\r\n\r\n'
    body_bytes = (
        b'--BOUNDARY123\r\n'
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b'Content-Transfer-Encoding: quoted-printable\r\n'
        b'\r\n'
        b'Hi there,=0D=0AThis has an em dash =E2=80=94 in it.\r\n'
        b'--BOUNDARY123\r\n'
        b'Content-Type: text/html; charset="utf-8"\r\n'
        b'\r\n'
        b'<html><body><p>Hi there</p></body></html>\r\n'
        b'--BOUNDARY123--\r\n'
    )
    snippet = extract_body_snippet(header_bytes, body_bytes)
    assert "Content-Type" not in snippet
    assert "BOUNDARY123" not in snippet
    assert "Hi there, This has an em dash — in it." in snippet


def test_extract_body_snippet_decodes_base64_part():
    import base64
    plain = b"This is a base64 encoded message body for testing purposes."
    encoded = base64.b64encode(plain)
    header_bytes = b'Content-Type: multipart/mixed; boundary="B2"\r\n\r\n'
    body_bytes = (
        b'--B2\r\n'
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b'Content-Transfer-Encoding: base64\r\n'
        b'\r\n'
        + encoded + b'\r\n'
        + b'--B2--\r\n'
    )
    assert extract_body_snippet(header_bytes, body_bytes) == plain.decode()


def test_extract_body_snippet_strips_html_when_no_plain_part():
    header_bytes = b'Content-Type: text/html; charset="utf-8"\r\n\r\n'
    body_bytes = b"<html><body><p>Hello <b>World</b></p></body></html>"
    assert extract_body_snippet(header_bytes, body_bytes) == "Hello World"


def test_extract_body_snippet_truncates_to_max_len():
    header_bytes = b"From: a@b.com\r\n\r\n"
    body_bytes = b"x" * 1000
    snippet = extract_body_snippet(header_bytes, body_bytes, max_len=50)
    assert snippet == "x" * 50


def test_extract_body_snippet_strips_complete_style_block():
    header_bytes = b'Content-Type: text/html; charset="utf-8"\r\n\r\n'
    body_bytes = b"<html><head><style>body { color: red; }</style></head><body><p>Hello</p></body></html>"
    assert extract_body_snippet(header_bytes, body_bytes) == "Hello"


def test_extract_body_snippet_drops_unclosed_style_block_from_byte_range_truncation():
    # BODY[TEXT]<0.2000> fetches are byte-range-truncated -- a marketing-template
    # email's inline <style> block can be cut off before its closing tag ever
    # appears, which previously leaked raw CSS into the preview.
    header_bytes = (
        b'Content-Type: text/html; charset="utf-8"\r\n'
        b'Content-Transfer-Encoding: quoted-printable\r\n\r\n'
    )
    body_bytes = (
        b'<html><head><style type=3D"text/css">\r\n'
        b'body { margin: 0; padding: 0; min-width: 100%; b=\r\n'
        b'ackground: #fff; width: 100% !important; font-family: Arial, Helvetica, san=\r\n'
        b's-serif; }\r\n'
        b'tbody { display:table !important; width:100% !important;} .but'
    )
    assert extract_body_snippet(header_bytes, body_bytes) == ""


def test_extract_body_snippet_keeps_text_between_style_blocks():
    header_bytes = b'Content-Type: text/html; charset="utf-8"\r\n\r\n'
    body_bytes = (
        b"<style>.a{color:red}</style>Real content here"
        b"<script>unclosed script tail that never closes"
    )
    assert extract_body_snippet(header_bytes, body_bytes) == "Real content here"


# ── fetch_many ─────────────────────────────────────────────────────────────────

def test_fetch_many_empty_id_list(mock_imap):
    assert commonFunctions.fetch_many(mock_imap, [], "(BODY[HEADER.FIELDS (FROM)])") == {}
    mock_imap.fetch.assert_not_called()


def test_fetch_many_parses_multiple_messages(mock_imap):
    mock_imap.fetch.return_value = ("OK", [
        (b"1 (BODY[HEADER.FIELDS (FROM)] {20}", b"From: a@x.com\r\n"),
        b")",
        (b"2 (BODY[HEADER.FIELDS (FROM)] {20}", b"From: b@y.com\r\n"),
        b")",
    ])
    result = commonFunctions.fetch_many(mock_imap, [b"1", b"2"], "(BODY[HEADER.FIELDS (FROM)])")
    assert result[b"1"]["header"] == b"From: a@x.com\r\n"
    assert result[b"2"]["header"] == b"From: b@y.com\r\n"
    # One batched FETCH with a comma-joined ID set, not one call per message
    mock_imap.fetch.assert_called_once()
    assert mock_imap.fetch.call_args[0][0] == b"1,2"


def test_fetch_many_combined_header_and_text_sections(mock_imap):
    # Servers echo the partial-fetch request <0.2000> back as just <0>; the
    # text continuation tuple carries no sequence number.
    mock_imap.fetch.return_value = ("OK", [
        (b"7 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {15}", b"From: a@x.com\r\n"),
        (b" BODY[TEXT]<0> {5}", b"hello"),
        b")",
    ])
    result = commonFunctions.fetch_many(
        mock_imap, [b"7"], "(BODY[HEADER.FIELDS (FROM SUBJECT DATE)] BODY[TEXT]<0.2000>)"
    )
    assert result[b"7"] == {"header": b"From: a@x.com\r\n", "text": b"hello"}


def test_fetch_many_skips_noise_and_missing_messages(mock_imap):
    mock_imap.fetch.return_value = ("OK", [
        b"* 3 FLAGS (\\Seen)",  # unsolicited untagged noise
        (b"1 (BODY[HEADER.FIELDS (FROM)] {15}", b"From: a@x.com\r\n"),
        b")",
    ])
    result = commonFunctions.fetch_many(mock_imap, [b"1", b"2"], "(BODY[HEADER.FIELDS (FROM)])")
    assert b"1" in result
    assert b"2" not in result  # server returned nothing for message 2


def test_fetch_many_chunks_large_id_lists(mock_imap):
    mock_imap.fetch.return_value = ("OK", [])
    ids = [str(i).encode() for i in range(1, 8)]
    commonFunctions.fetch_many(mock_imap, ids, "(BODY[HEADER.FIELDS (FROM)])", chunk_size=3)
    id_sets = [c[0][0] for c in mock_imap.fetch.call_args_list]
    assert id_sets == [b"1,2,3", b"4,5,6", b"7"]


def test_fetch_many_skips_failed_chunk(mock_imap):
    mock_imap.fetch.side_effect = [
        ("NO", [b"err"]),
        ("OK", [(b"3 (BODY[HEADER.FIELDS (FROM)] {15}", b"From: c@z.com\r\n"), b")"]),
    ]
    result = commonFunctions.fetch_many(
        mock_imap, [b"1", b"2", b"3"], "(BODY[HEADER.FIELDS (FROM)])", chunk_size=2
    )
    assert result == {b"3": {"header": b"From: c@z.com\r\n", "text": None}}


# ── move_imap_messages copy-failure safety ─────────────────────────────────────

def test_move_imap_messages_failed_copy_never_deletes(mock_imap):
    mock_imap.search.return_value = ("OK", [b"1 2"])
    mock_imap.copy.return_value = ("NO", [b"[TRYCREATE] no such mailbox"])
    with patch("commonFunctions.connect_to_imap", return_value=mock_imap):
        result = move_imap_messages(
            "sender@example.com", "MailMatrixCategories/Old", "MailMatrixCategories/New",
            imap_server="imap.example.com", imap_port=993, username="user", password="pw",
        )
    assert result["ok"] is False
    assert result["moved"] == 0
    assert result["copy_failed"] == 2
    mock_imap.store.assert_not_called()
    mock_imap.expunge.assert_not_called()


def test_move_imap_messages_partial_copy_failure_deletes_only_copied(mock_imap):
    mock_imap.search.return_value = ("OK", [b"1 2"])
    mock_imap.copy.side_effect = [("OK", None), ("NO", [b"quota exceeded"])]
    with patch("commonFunctions.connect_to_imap", return_value=mock_imap):
        result = move_imap_messages(
            "sender@example.com", "MailMatrixCategories/Old", "MailMatrixCategories/New",
            imap_server="imap.example.com", imap_port=993, username="user", password="pw",
        )
    assert result == {"ok": True, "moved": 1, "copy_failed": 1}
    mock_imap.store.assert_called_once()
    mock_imap.expunge.assert_called_once()


# ── load_rules_file corrupt-file handling ──────────────────────────────────────

def test_load_rules_file_missing_returns_empty(tmp_path):
    assert commonFunctions.load_rules_file(tmp_path / "nope.json") == {"labels": []}


def test_load_rules_file_corrupt_backs_up_and_returns_empty(tmp_path):
    rules = tmp_path / "emailRules.json"
    rules.write_text("{not valid json", encoding="utf-8")
    assert commonFunctions.load_rules_file(rules) == {"labels": []}
    backups = list(tmp_path.glob("emailRules.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"


def test_load_rules_file_non_object_root_treated_as_corrupt(tmp_path):
    rules = tmp_path / "emailRules.json"
    rules.write_text("[1, 2, 3]", encoding="utf-8")
    assert commonFunctions.load_rules_file(rules) == {"labels": []}
    assert list(tmp_path.glob("emailRules.json.corrupt-*"))


# ── credential blob caching ────────────────────────────────────────────────────

def test_get_credential_caches_keychain_blob(monkeypatch):
    import keyring
    commonFunctions.set_credential("IMAP_SERVER", "imap.cached.com")
    calls = {"n": 0}
    real_get = keyring.get_password

    def _counting_get(service, username):
        calls["n"] += 1
        return real_get(service, username)

    monkeypatch.setattr("keyring.get_password", _counting_get)
    assert commonFunctions.get_credential("IMAP_SERVER") == "imap.cached.com"
    assert commonFunctions.get_credential("IMAP_SERVER") == "imap.cached.com"
    assert calls["n"] == 0  # served from the in-process cache


def test_set_credential_updates_cache_immediately():
    commonFunctions.set_credential("IMAP_SERVER", "first.example.com")
    assert commonFunctions.get_credential("IMAP_SERVER") == "first.example.com"
    commonFunctions.set_credential("IMAP_SERVER", "second.example.com")
    assert commonFunctions.get_credential("IMAP_SERVER") == "second.example.com"


# ── Mail-client helpers ───────────────────────────────────────────────────────

from commonFunctions import (
    add_sender_to_label_rule,
    decode_modified_utf7,
    extract_message_parts,
    fetch_many,
    get_attachment,
    list_folders,
    move_message_uid,
    send_smtp,
    uid_search_all,
    validate_folder,
    validate_new_folder_name,
)


# validate_folder / validate_new_folder_name

def test_validate_folder_accepts_ordinary_names():
    assert validate_folder("INBOX")
    assert validate_folder("Sent")
    assert validate_folder("MailMatrixCategories/Work")
    assert validate_folder("Archive/2026")


def test_validate_folder_rejects_injection_and_traversal():
    assert not validate_folder("")
    assert not validate_folder('bad"name')
    assert not validate_folder("bad\\name")
    assert not validate_folder("bad\nname")
    assert not validate_folder("bad\rname")
    assert not validate_folder("../etc")
    assert not validate_folder("/leading")
    assert not validate_folder("x" * 501)


def test_validate_new_folder_name_requires_printable_ascii():
    assert validate_new_folder_name("MailMatrixCategories/Newsletters")
    assert not validate_new_folder_name("Résumés")   # non-ASCII
    assert not validate_new_folder_name("bad\tname")  # control char
    assert not validate_new_folder_name('bad"name')   # inherits validate_folder


# decode_modified_utf7

def test_decode_modified_utf7_plain_ascii():
    assert decode_modified_utf7("INBOX") == "INBOX"
    assert decode_modified_utf7("MailMatrixCategories/Work") == "MailMatrixCategories/Work"


def test_decode_modified_utf7_literal_ampersand():
    assert decode_modified_utf7("Bed &- Breakfast") == "Bed & Breakfast"


def test_decode_modified_utf7_non_ascii():
    # "&APk-" encodes U+00E9 (é); "Se&AOk-tt" style — verify a known sequence.
    assert decode_modified_utf7("R&AOk-sum&AOk-s") == "Résumés"


def test_decode_modified_utf7_bad_sequence_left_as_is():
    # An unterminated shift shouldn't raise.
    assert decode_modified_utf7("weird&noclose") == "weird&noclose"


# list_folders

def _list_imap(lines):
    imap = MagicMock()
    imap.list.return_value = ("OK", lines)
    return imap


def test_list_folders_parses_flags_delimiter_and_name():
    imap = _list_imap([
        rb'(\HasNoChildren) "/" "INBOX"',
        rb'(\HasNoChildren \Sent) "/" "Sent"',
        rb'(\HasNoChildren \Trash) "/" "Trash"',
    ])
    folders = list_folders(imap)
    by_name = {f["name"]: f for f in folders}
    assert by_name["INBOX"]["delimiter"] == "/"
    assert by_name["INBOX"]["special"] is None
    assert by_name["Sent"]["special"] == "sent"
    assert by_name["Trash"]["special"] == "trash"
    assert all(f["selectable"] for f in folders)


def test_list_folders_special_use_flag_wins_over_name():
    imap = _list_imap([rb'(\HasNoChildren \Junk) "/" "Spammy"'])
    assert list_folders(imap)[0]["special"] == "spam"


def test_list_folders_name_fallback_when_no_special_use():
    imap = _list_imap([
        rb'(\HasNoChildren) "/" "Sent"',
        rb'(\HasNoChildren) "/" "Junk Mail"',
    ])
    by_name = {f["name"]: f for f in list_folders(imap)}
    assert by_name["Sent"]["special"] == "sent"
    assert by_name["Junk Mail"]["special"] == "spam"


def test_list_folders_marks_noselect_unselectable():
    imap = _list_imap([rb'(\Noselect \HasChildren) "/" "[Parent]"'])
    f = list_folders(imap)[0]
    assert f["selectable"] is False


def test_list_folders_decodes_display_name():
    imap = _list_imap([rb'(\HasNoChildren) "/" "R&AOk-sum&AOk-s"'])
    f = list_folders(imap)[0]
    assert f["name"] == "R&AOk-sum&AOk-s"      # raw wire name preserved
    assert f["display"] == "Résumés"           # decoded for display


# uid_search_all

def test_uid_search_all_returns_uids():
    imap = MagicMock()
    imap.select.return_value = ("OK", [b"3"])
    imap.uid.return_value = ("OK", [b"11 27 42"])
    assert uid_search_all(imap, "INBOX") == [b"11", b"27", b"42"]


def test_uid_search_all_empty_folder():
    imap = MagicMock()
    imap.select.return_value = ("OK", [b"0"])
    imap.uid.return_value = ("OK", [b""])
    assert uid_search_all(imap, "INBOX") == []


def test_uid_search_all_raises_on_bad_folder():
    imap = MagicMock()
    imap.select.return_value = ("NO", [b"Mailbox does not exist"])
    with pytest.raises(imaplib.IMAP4.error):
        uid_search_all(imap, "Nope")


# fetch_many with use_uid

def _uid_fetch(headers_by_uid, flags_by_uid=None):
    flags_by_uid = flags_by_uid or {}
    def _fetch(cmd, id_set, what):
        assert cmd == "FETCH"
        data = []
        for uid in id_set.split(","):
            uid_b = uid.encode()
            headers = headers_by_uid[uid]
            flags = flags_by_uid.get(uid, b"")
            prefix = b"1 (UID %s FLAGS (%s) BODY[HEADER.FIELDS (FROM)] {%d}" % (
                uid_b, flags, len(headers))
            data.append((prefix, headers))
            data.append(b")")
        return ("OK", data)
    return _fetch


def test_fetch_many_uid_keys_by_uid_and_captures_flags():
    imap = MagicMock()
    imap.uid.side_effect = _uid_fetch(
        {"42": b"From: a@b.com\r\n", "43": b"From: c@d.com\r\n"},
        {"42": b"\\Seen", "43": b""},
    )
    result = fetch_many(imap, [b"42", b"43"], "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM)])", use_uid=True)
    assert set(result.keys()) == {b"42", b"43"}
    assert result[b"42"]["header"] == b"From: a@b.com\r\n"
    assert result[b"42"]["flags"] == b"\\Seen"
    assert result[b"43"]["flags"] == b""


def test_fetch_many_uid_handles_flags_only_response_item():
    # A UID STORE-style FLAGS-only line arrives as plain bytes, not a tuple.
    imap = MagicMock()
    imap.uid.return_value = ("OK", [b"1 (UID 7 FLAGS (\\Seen \\Flagged))"])
    result = fetch_many(imap, [b"7"], "(FLAGS)", use_uid=True)
    assert result[b"7"]["flags"] == b"\\Seen \\Flagged"


def test_fetch_many_sequence_mode_unchanged():
    # Regression: non-UID mode still keys by sequence number, no flags key.
    imap = MagicMock()
    imap.fetch.return_value = ("OK", [
        (b"1 (BODY[HEADER.FIELDS (FROM)] {16}", b"From: a@b.com\r\n"),
        b")",
    ])
    result = fetch_many(imap, [b"1"], "(BODY[HEADER.FIELDS (FROM)])")
    assert result[b"1"]["header"] == b"From: a@b.com\r\n"
    assert "flags" not in result[b"1"]


# extract_message_parts / get_attachment

def _multipart_with_attachment():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "bob@example.com"
    msg["Cc"] = "carol@example.com"
    msg["Subject"] = "Hello"
    msg["Date"] = "Fri, 27 Jun 2026 10:00:00 +0000"
    msg["Message-ID"] = "<abc@example.com>"
    msg["References"] = "<prev@example.com>"
    msg.set_content("Plain body here.")
    msg.add_alternative("<p>HTML body <script>alert(1)</script>here.</p>", subtype="html")
    msg.add_attachment(b"file-bytes", maintype="application", subtype="pdf",
                       filename="report.pdf")
    return msg.as_bytes()


def test_extract_message_parts_headers_text_html_and_attachments():
    parts = extract_message_parts(_multipart_with_attachment())
    assert parts["headers"]["from"] == "Alice <alice@example.com>"
    assert parts["headers"]["to"] == "bob@example.com"
    assert parts["headers"]["cc"] == "carol@example.com"
    assert parts["headers"]["message_id"] == "<abc@example.com>"
    assert parts["headers"]["references"] == "<prev@example.com>"
    assert "Plain body here." in parts["text"]
    assert "HTML body" in parts["html"]
    assert "<script>" not in parts["html"]  # scripts stripped
    assert len(parts["attachments"]) == 1
    assert parts["attachments"][0]["filename"] == "report.pdf"
    assert parts["attachments"][0]["content_type"] == "application/pdf"
    assert parts["attachments"][0]["size"] == len(b"file-bytes")


def test_extract_message_parts_html_only():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = "a@b.com"
    msg["Subject"] = "x"
    msg.set_content("<b>hi</b>", subtype="html")
    parts = extract_message_parts(msg.as_bytes())
    assert parts["text"] is None
    assert "hi" in parts["html"]


def test_get_attachment_returns_payload():
    raw = _multipart_with_attachment()
    part_index = extract_message_parts(raw)["attachments"][0]["part"]
    result = get_attachment(raw, part_index)
    assert result is not None
    filename, content_type, payload = result
    assert filename == "report.pdf"
    assert content_type == "application/pdf"
    assert payload == b"file-bytes"


def test_get_attachment_bad_index_returns_none():
    assert get_attachment(_multipart_with_attachment(), 999) is None


def test_get_attachment_sanitizes_traversal_filename():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = "a@b.com"
    msg.set_content("body")
    msg.add_attachment(b"x", maintype="application", subtype="octet-stream",
                       filename="../../etc/passwd")
    raw = msg.as_bytes()
    idx = extract_message_parts(raw)["attachments"][0]["part"]
    filename, _ct, _payload = get_attachment(raw, idx)
    assert "/" not in filename
    assert filename == "passwd"


# move_message_uid

def _move_imap(copy_result=("OK", None), expunge_uid_result=("OK", None)):
    imap = MagicMock()
    imap.select.return_value = ("OK", [b"3"])

    def _uid(cmd, *args):
        if cmd == "FETCH":
            return ("OK", [(b"1 (UID 5 BODY[HEADER.FIELDS (FROM)] {24}",
                            b"From: sender@x.com\r\n"), b")"])
        if cmd == "COPY":
            return copy_result
        if cmd == "STORE":
            return ("OK", [None])
        if cmd == "EXPUNGE":
            return expunge_uid_result
        return ("OK", None)

    imap.uid.side_effect = _uid
    imap.expunge.return_value = ("OK", None)
    return imap


def test_move_message_uid_success():
    imap = _move_imap()
    result = move_message_uid(imap, "INBOX", "5", "Archive")
    assert result["ok"] is True
    assert result["from_addr"] == "sender@x.com"
    # COPY then STORE \Deleted then UID EXPUNGE
    cmds = [c.args[0] for c in imap.uid.call_args_list]
    assert cmds == ["FETCH", "COPY", "STORE", "EXPUNGE"]
    imap.expunge.assert_not_called()


def test_move_message_uid_copy_failure_leaves_message():
    imap = _move_imap(copy_result=("NO", [b"over quota"]))
    result = move_message_uid(imap, "INBOX", "5", "Archive")
    assert result["ok"] is False
    cmds = [c.args[0] for c in imap.uid.call_args_list]
    assert "STORE" not in cmds      # never flagged for deletion
    assert "EXPUNGE" not in cmds
    imap.expunge.assert_not_called()


def test_move_message_uid_falls_back_to_plain_expunge():
    imap = _move_imap(expunge_uid_result=("NO", [b"UIDPLUS not supported"]))
    result = move_message_uid(imap, "INBOX", "5", "Archive")
    assert result["ok"] is True
    imap.expunge.assert_called_once()


def test_move_message_uid_missing_message():
    imap = MagicMock()
    imap.select.return_value = ("OK", [b"0"])
    imap.uid.return_value = ("OK", [None])  # no message tuple
    result = move_message_uid(imap, "INBOX", "5", "Archive")
    assert result["ok"] is False
    assert result["error"] == "Message not found"


# add_sender_to_label_rule

def test_add_sender_to_label_rule_existing_label():
    data = {"labels": [{"labelName": "MailMatrixCategories/Work",
                        "emailAddresses": ["z@work.com"], "emailDomains": []}]}
    add_sender_to_label_rule(data, "a@work.com", "MailMatrixCategories/Work")
    entry = data["labels"][0]
    assert entry["emailAddresses"] == ["a@work.com", "z@work.com"]  # sorted


def test_add_sender_to_label_rule_dedupes():
    data = {"labels": [{"labelName": "MailMatrixCategories/Work",
                        "emailAddresses": ["a@work.com"], "emailDomains": []}]}
    add_sender_to_label_rule(data, "a@work.com", "MailMatrixCategories/Work")
    assert data["labels"][0]["emailAddresses"] == ["a@work.com"]


def test_add_sender_to_label_rule_creates_new_label():
    data = {"labels": []}
    add_sender_to_label_rule(data, "a@new.com", "MailMatrixCategories/New")
    assert data["labels"] == [{
        "labelName": "MailMatrixCategories/New",
        "emailAddresses": ["a@new.com"],
        "emailDomains": [],
    }]


# send_smtp

def test_send_smtp_uses_ssl_for_465():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = "hi"
    with patch("commonFunctions.smtplib.SMTP_SSL") as mock_ssl:
        smtp = mock_ssl.return_value.__enter__.return_value
        send_smtp("smtp.fastmail.com", 465, "user@x.com", "pw", msg)
        mock_ssl.assert_called_once_with("smtp.fastmail.com", 465)
        smtp.login.assert_called_once_with("user@x.com", "pw")
        smtp.send_message.assert_called_once_with(msg)


def test_send_smtp_uses_starttls_for_587():
    from email.message import EmailMessage
    msg = EmailMessage()
    with patch("commonFunctions.smtplib.SMTP") as mock_smtp:
        smtp = mock_smtp.return_value.__enter__.return_value
        send_smtp("smtp.fastmail.com", 587, "user@x.com", "pw", msg)
        mock_smtp.assert_called_once_with("smtp.fastmail.com", 587)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("user@x.com", "pw")
        smtp.send_message.assert_called_once_with(msg)
