import json
import time
from unittest.mock import MagicMock, patch

import pytest

from emailRulesInit import build_email_rules, crawl_emails_in_label, write_to_json


# ── write_to_json ─────────────────────────────────────────────────────────────

def test_write_to_json_creates_valid_file(tmp_path):
    data = {
        "labels": [{
            "labelName": "MailMatrixCategories/Work",
            "emailAddresses": ["boss@work.com"],
            "emailDomains": [],
        }]
    }
    out = tmp_path / "rules.json"
    write_to_json(data, str(out))

    assert out.exists()
    written = json.loads(out.read_text())
    assert written == data


def test_write_to_json_pretty_prints(tmp_path):
    out = tmp_path / "rules.json"
    write_to_json({"labels": []}, str(out))
    content = out.read_text()
    assert "\n" in content  # has newlines (indented)


# ── crawl_emails_in_label ─────────────────────────────────────────────────────

def _make_progress():
    return {
        "start": time.monotonic(),
        "last_summary": time.monotonic(),
        "messages": 0,
        "labels_done": 0,
        "labels_total": 1,
        "unique_addresses": 0,
    }


def test_crawl_emails_in_label_extracts_from_header(mock_imap):
    raw_email = b"From: Alice <alice@work.com>\r\nSubject: Test\r\n\r\nHello"
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {42})", raw_email), b")"])

    result = crawl_emails_in_label(mock_imap, "MailMatrixCategories/Work", _make_progress())

    assert "alice@work.com" in result


def test_crawl_emails_in_label_ignores_missing_email(mock_imap):
    raw_email = b"From: Not An Email Address\r\nSubject: Test\r\n\r\nHello"
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {42})", raw_email), b")"])

    result = crawl_emails_in_label(mock_imap, "MailMatrixCategories/Work", _make_progress())

    assert len(result) == 0  # no valid email address found


def test_crawl_emails_in_label_no_messages(mock_imap):
    mock_imap.search.return_value = ("OK", [b""])

    result = crawl_emails_in_label(mock_imap, "MailMatrixCategories/Work", _make_progress())

    assert result == set()
    mock_imap.fetch.assert_not_called()


def test_crawl_emails_in_label_select_failure(mock_imap):
    mock_imap.select.return_value = ("NO", [b"Mailbox not found"])

    result = crawl_emails_in_label(mock_imap, "MailMatrixCategories/Work", _make_progress())

    assert result == set()
    mock_imap.search.assert_not_called()


def test_crawl_emails_in_label_deduplicates(mock_imap):
    raw_email = b"From: Alice <alice@work.com>\r\nSubject: Msg1\r\n\r\nHello"
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {42})", raw_email), b")"])

    # Same address in every message — should appear once in the set
    result = crawl_emails_in_label(mock_imap, "MailMatrixCategories/Work", _make_progress())

    assert result == {"alice@work.com"}


# ── build_email_rules ─────────────────────────────────────────────────────────

FOLDER_BYTES = [
    b'(\\HasNoChildren) "/" "MailMatrixCategories/Work"',
    b'(\\HasNoChildren) "/" "MailMatrixCategories/Shopping"',
]


def test_build_email_rules_structure(mock_imap):
    mock_imap.list.return_value = ("OK", FOLDER_BYTES)

    # Labels are sorted alphabetically: Shopping before Work
    with patch(
        "emailRulesInit.crawl_emails_in_label",
        side_effect=[{"orders@amazon.com"}, {"alice@work.com", "boss@work.com"}],
    ):
        result = build_email_rules(mock_imap)

    assert "labels" in result
    assert len(result["labels"]) == 2

    work_entry = next(e for e in result["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    assert "alice@work.com" in work_entry["emailAddresses"]
    assert "boss@work.com" in work_entry["emailAddresses"]
    assert work_entry["emailDomains"] == []

    shopping_entry = next(e for e in result["labels"] if e["labelName"] == "MailMatrixCategories/Shopping")
    assert "orders@amazon.com" in shopping_entry["emailAddresses"]


def test_build_email_rules_addresses_sorted(mock_imap):
    mock_imap.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "MailMatrixCategories/Work"'])

    with patch(
        "emailRulesInit.crawl_emails_in_label",
        return_value={"zach@work.com", "alice@work.com", "bob@work.com"},
    ):
        result = build_email_rules(mock_imap)

    addresses = result["labels"][0]["emailAddresses"]
    assert addresses == sorted(addresses)


def test_build_email_rules_no_labels(mock_imap):
    mock_imap.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])

    result = build_email_rules(mock_imap)

    assert result["labels"] == []  # INBOX doesn't match MailMatrixCategories/ prefix
