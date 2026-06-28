import json

import pytest

from sortEmail import find_matching_labels, load_rules, sort_inbox


# ── load_rules ────────────────────────────────────────────────────────────────

def test_load_rules_email_lookup(rules_file):
    email_to_labels, _ = load_rules(str(rules_file))
    assert email_to_labels["boss@work.com"] == ["MailMatrixCategories/Work"]
    assert email_to_labels["orders@amazon.com"] == ["MailMatrixCategories/Shopping"]


def test_load_rules_domain_lookup(rules_file):
    _, domain_to_labels = load_rules(str(rules_file))
    assert domain_to_labels["shop.example.com"] == ["MailMatrixCategories/Shopping"]


def test_load_rules_lowercases_addresses(tmp_path):
    data = {
        "labels": [{
            "labelName": "MailMatrixCategories/Work",
            "emailAddresses": ["BOSS@WORK.COM"],
            "emailDomains": ["WORK.COM"],
        }]
    }
    f = tmp_path / "rules.json"
    f.write_text(json.dumps(data))
    email_to_labels, domain_to_labels = load_rules(str(f))
    assert "boss@work.com" in email_to_labels
    assert "work.com" in domain_to_labels


def test_load_rules_empty_file(tmp_path):
    f = tmp_path / "rules.json"
    f.write_text(json.dumps({"labels": []}))
    email_to_labels, domain_to_labels = load_rules(str(f))
    assert email_to_labels == {}
    assert domain_to_labels == {}


def test_load_rules_multiple_labels_same_address(tmp_path):
    data = {
        "labels": [
            {"labelName": "MailMatrixCategories/A", "emailAddresses": ["shared@test.com"], "emailDomains": []},
            {"labelName": "MailMatrixCategories/B", "emailAddresses": ["shared@test.com"], "emailDomains": []},
        ]
    }
    f = tmp_path / "rules.json"
    f.write_text(json.dumps(data))
    email_to_labels, _ = load_rules(str(f))
    assert set(email_to_labels["shared@test.com"]) == {
        "MailMatrixCategories/A",
        "MailMatrixCategories/B",
    }


# ── find_matching_labels ──────────────────────────────────────────────────────

def test_find_matching_labels_by_exact_email():
    email_to_labels = {"boss@work.com": ["MailMatrixCategories/Work"]}
    result = find_matching_labels("boss@work.com", email_to_labels, {})
    assert result == ["MailMatrixCategories/Work"]


def test_find_matching_labels_by_domain():
    domain_to_labels = {"work.com": ["MailMatrixCategories/Work"]}
    result = find_matching_labels("anyone@work.com", {}, domain_to_labels)
    assert result == ["MailMatrixCategories/Work"]


def test_find_matching_labels_no_match():
    result = find_matching_labels("unknown@nobody.com", {}, {})
    assert result == []


def test_find_matching_labels_deduplicates_overlapping_rules():
    email_to_labels = {"alice@work.com": ["MailMatrixCategories/Work"]}
    domain_to_labels = {"work.com": ["MailMatrixCategories/Work"]}
    result = find_matching_labels("alice@work.com", email_to_labels, domain_to_labels)
    assert result == ["MailMatrixCategories/Work"]  # deduplicated


def test_find_matching_labels_returns_sorted():
    email_to_labels = {"a@b.com": ["MailMatrixCategories/Z", "MailMatrixCategories/A"]}
    result = find_matching_labels("a@b.com", email_to_labels, {})
    assert result == sorted(result)


def test_find_matching_labels_address_without_at_sign():
    result = find_matching_labels("notanemail", {}, {"notanemail": ["MailMatrixCategories/X"]})
    assert result == []  # no @ means no domain match


# ── sort_inbox ────────────────────────────────────────────────────────────────

def _header_fetch(from_addr: str) -> tuple:
    return ("OK", [(b"1 (BODY[HEADER.FIELDS (FROM)] {30})", f"From: {from_addr}\r\n".encode()), b")"])


def test_sort_inbox_moves_matching_message(mock_imap):
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.fetch.return_value = _header_fetch("boss@work.com")

    email_to_labels = {"boss@work.com": ["MailMatrixCategories/Work"]}
    sort_inbox(mock_imap, email_to_labels, {})

    mock_imap.copy.assert_called_once()
    mock_imap.store.assert_called_once()
    mock_imap.expunge.assert_called_once()


def test_sort_inbox_skips_unmatched_message(mock_imap):
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.fetch.return_value = _header_fetch("unknown@nobody.com")

    sort_inbox(mock_imap, {}, {})

    mock_imap.copy.assert_not_called()
    mock_imap.store.assert_not_called()
    mock_imap.expunge.assert_called_once()  # expunge still runs at end


def test_sort_inbox_empty_inbox(mock_imap):
    mock_imap.search.return_value = ("OK", [b""])

    sort_inbox(mock_imap, {"boss@work.com": ["MailMatrixCategories/Work"]}, {})

    mock_imap.fetch.assert_not_called()
    mock_imap.copy.assert_not_called()
    mock_imap.expunge.assert_called_once()


def test_sort_inbox_copies_to_multiple_labels(mock_imap):
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.fetch.return_value = _header_fetch("alice@work.com")

    email_to_labels = {"alice@work.com": ["MailMatrixCategories/Work", "MailMatrixCategories/VIP"]}
    sort_inbox(mock_imap, email_to_labels, {})

    assert mock_imap.copy.call_count == 2
    mock_imap.store.assert_called_once()


def test_sort_inbox_matches_by_domain_rule(mock_imap):
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.fetch.return_value = _header_fetch("anyone@work.com")

    domain_to_labels = {"work.com": ["MailMatrixCategories/Work"]}
    sort_inbox(mock_imap, {}, domain_to_labels)

    mock_imap.copy.assert_called_once()
    mock_imap.store.assert_called_once()


def test_sort_inbox_aborts_on_select_failure(mock_imap):
    mock_imap.select.return_value = ("NO", [b"Mailbox not found"])

    sort_inbox(mock_imap, {"boss@work.com": ["MailMatrixCategories/Work"]}, {})

    mock_imap.search.assert_not_called()
    mock_imap.fetch.assert_not_called()
