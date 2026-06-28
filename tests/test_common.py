import imaplib
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from commonFunctions import (
    connect_to_imap,
    decode_header_value,
    extract_email_address,
    get_all_labels,
    imap_call,
    imap_date,
    parse_headers,
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
