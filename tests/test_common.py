import imaplib
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from commonFunctions import (
    build_rule_groups,
    connect_to_imap,
    convert_domain_rule,
    decode_header_value,
    delete_rule,
    extract_email_address,
    full_label_name,
    get_all_labels,
    imap_call,
    imap_date,
    move_imap_messages,
    parse_headers,
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


def test_imap_call_raises_non_rate_limit_immediately():
    calls = {"n": 0}

    def non_rate_limit():
        calls["n"] += 1
        raise imaplib.IMAP4.error("LOGIN failed: invalid credentials")

    with pytest.raises(imaplib.IMAP4.error, match="invalid credentials"):
        imap_call(non_rate_limit)

    assert calls["n"] == 1  # no retry


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


# ── move_imap_messages ─────────────────────────────────────────────────────────

def test_move_imap_messages_moves_matching_messages(mock_imap):
    mock_imap.search.return_value = ("OK", [b"1 2"])
    with patch("commonFunctions.connect_to_imap", return_value=mock_imap):
        result = move_imap_messages(
            "sender@example.com", "MailMatrixCategories/Old", "MailMatrixCategories/New",
            imap_server="imap.example.com", imap_port=993, username="user", password="pw",
        )
    assert result == {"ok": True, "moved": 2}
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
    assert result == {"ok": True, "moved": 0}
    mock_imap.expunge.assert_not_called()
