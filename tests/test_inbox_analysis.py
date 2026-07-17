import json
from unittest.mock import patch

import pytest

import inboxAnalysis

# Keychain is faked repo-wide by the autouse `_fake_keychain` fixture in conftest.py.


@pytest.fixture
def rules_path(tmp_path, rules_data, monkeypatch):
    path = tmp_path / "emailRules.json"
    path.write_text(json.dumps(rules_data))
    monkeypatch.setattr(inboxAnalysis, "RULES_PATH", path)
    return path


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("IMAP_SERVER", "imap.test.com")
    monkeypatch.setenv("IMAP_PORT", "993")
    monkeypatch.setenv("IMAP_USERNAME", "user@test.com")
    monkeypatch.setenv("IMAP_PASSWORD", "pass")


# ── stats_only ─────────────────────────────────────────────────────────────────

def test_stats_only_missing_credentials(monkeypatch):
    monkeypatch.delenv("IMAP_SERVER", raising=False)
    monkeypatch.delenv("IMAP_USERNAME", raising=False)
    monkeypatch.delenv("IMAP_PASSWORD", raising=False)
    monkeypatch.delenv("IMAP_PORT", raising=False)
    result = inboxAnalysis.stats_only()
    assert result["ok"] is False
    assert result["connected"] is False


def test_stats_only_connected(creds, mock_imap):
    mock_imap.select.return_value = ("OK", [b"7"])
    with patch("inboxAnalysis.connect_to_imap", return_value=mock_imap):
        result = inboxAnalysis.stats_only()
    assert result == {"ok": True, "inbox_count": 7, "connected": True}


# ── dashboard_stats_mode ───────────────────────────────────────────────────────

def test_dashboard_stats_mode_missing_credentials(monkeypatch, rules_path):
    monkeypatch.delenv("IMAP_SERVER", raising=False)
    monkeypatch.delenv("IMAP_USERNAME", raising=False)
    monkeypatch.delenv("IMAP_PASSWORD", raising=False)
    monkeypatch.delenv("IMAP_PORT", raising=False)

    result = inboxAnalysis.dashboard_stats_mode()

    assert result["ok"] is False
    assert result["connected"] is False
    # Rule/label counts should still be populated even without IMAP.
    assert result["label_count"] == 2
    assert result["rules_count"] == 4


def test_dashboard_stats_mode_connected(creds, mock_imap, rules_path):
    mock_imap.select.return_value = ("OK", [b"3"])
    with patch("inboxAnalysis.connect_to_imap", return_value=mock_imap):
        result = inboxAnalysis.dashboard_stats_mode()

    assert result["ok"] is True
    assert result["inbox_count"] == 3
    assert result["connected"] is True
    assert result["label_count"] == 2
    assert result["rules_count"] == 4
    assert len(result["recent_days"]) == 7


# ── summaries_list_mode ────────────────────────────────────────────────────────

def test_summaries_list_mode_empty(rules_path):
    result = inboxAnalysis.summaries_list_mode()
    assert result == {"ok": True, "files": []}


def test_summaries_list_mode_lists_saved_reports(rules_path):
    summary_dir = rules_path.parent / "emailSummary"
    summary_dir.mkdir()
    (summary_dir / "email_summary_2026-07-16.html").write_text("<html></html>")
    (summary_dir / "email_summary_2026-07-01.html").write_text("<html></html>")

    result = inboxAnalysis.summaries_list_mode()

    assert result["ok"] is True
    assert [f["date"] for f in result["files"]] == ["2026-07-16", "2026-07-01"]
    assert result["files"][0]["label"] == "July 16, 2026"


# ── cleanup_scan_mode / cleanup_collapse_domain_mode / cleanup_resolve_duplicate_mode ──

def test_cleanup_scan_mode_clean_rules(rules_path):
    result = inboxAnalysis.cleanup_scan_mode()
    assert result == {"ok": True, "collapses": [], "duplicates": []}


def test_cleanup_scan_mode_finds_collapses_and_duplicates(rules_path, rules_data):
    rules_data["labels"][1]["emailAddresses"].append("orders@shop.example.com")
    rules_data["labels"].append({
        "labelName": "MailMatrixCategories/VIP",
        "emailAddresses": ["boss@work.com"],
        "emailDomains": [],
    })
    rules_path.write_text(json.dumps(rules_data))

    result = inboxAnalysis.cleanup_scan_mode()

    assert result["ok"] is True
    assert len(result["collapses"]) == 1
    assert result["collapses"][0]["domain"] == "shop.example.com"
    assert len(result["duplicates"]) == 1
    assert result["duplicates"][0]["address"] == "boss@work.com"


def test_cleanup_collapse_domain_mode_removes_matching_addresses(rules_path, rules_data):
    rules_data["labels"][1]["emailAddresses"].append("orders@shop.example.com")
    rules_path.write_text(json.dumps(rules_data))

    result = inboxAnalysis.cleanup_collapse_domain_mode("shop.example.com")

    assert result == {"ok": True, "removed": 1}
    updated = json.loads(rules_path.read_text())
    shopping = next(e for e in updated["labels"] if e["labelName"] == "MailMatrixCategories/Shopping")
    assert "orders@shop.example.com" not in shopping["emailAddresses"]


def test_cleanup_collapse_domain_mode_no_domain_rule(rules_path):
    result = inboxAnalysis.cleanup_collapse_domain_mode("no-such-domain.com")
    assert result == {"ok": False, "error": "No domain rule found for @no-such-domain.com"}


def test_cleanup_resolve_duplicate_mode_keeps_one_label(rules_path, rules_data):
    rules_data["labels"].append({
        "labelName": "MailMatrixCategories/VIP",
        "emailAddresses": ["boss@work.com"],
        "emailDomains": [],
    })
    rules_path.write_text(json.dumps(rules_data))

    result = inboxAnalysis.cleanup_resolve_duplicate_mode("boss@work.com", "MailMatrixCategories/Work")

    assert result == {"ok": True, "removed": 1}
    updated = json.loads(rules_path.read_text())
    work = next(e for e in updated["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    vip = next(e for e in updated["labels"] if e["labelName"] == "MailMatrixCategories/VIP")
    assert "boss@work.com" in work["emailAddresses"]
    assert "boss@work.com" not in vip["emailAddresses"]


# ── accept_mode ──────────────────────────────────────────────────────────────

def test_accept_mode_rejects_invalid_address():
    result = inboxAnalysis.accept_mode("not-an-email", "MailMatrixCategories/Work")
    assert result["ok"] is False


def test_accept_mode_rejects_invalid_label():
    result = inboxAnalysis.accept_mode("user@example.com", "Work")
    assert result["ok"] is False


# ── rules_list_mode ────────────────────────────────────────────────────────────

def test_rules_list_mode_returns_groups(rules_path):
    result = inboxAnalysis.rules_list_mode()
    assert result["ok"] is True
    assert result["label_names"] == ["Shopping", "Work"]
    assert result["total_senders"] == 3
    assert result["total_domain_rules"] == 1


# ── rules_delete_mode ──────────────────────────────────────────────────────────

def test_rules_delete_mode_invalid_type(rules_path):
    result = inboxAnalysis.rules_delete_mode("bogus", "MailMatrixCategories/Work", "", "")
    assert result["ok"] is False


def test_rules_delete_mode_removes_sender(rules_path):
    result = inboxAnalysis.rules_delete_mode(
        "sender", "MailMatrixCategories/Work", "boss@work.com", "")
    assert result["ok"] is True
    saved = json.loads(rules_path.read_text())
    assert "boss@work.com" not in saved["labels"][0]["emailAddresses"]


def test_rules_delete_mode_removes_domain(rules_path):
    result = inboxAnalysis.rules_delete_mode(
        "domain", "MailMatrixCategories/Shopping", "", "shop.example.com")
    assert result["ok"] is True
    saved = json.loads(rules_path.read_text())
    assert "shop.example.com" not in saved["labels"][1]["emailDomains"]


# ── rules_update_sender_mode ───────────────────────────────────────────────────

def test_rules_update_sender_mode_missing_fields(rules_path):
    result = inboxAnalysis.rules_update_sender_mode("", "MailMatrixCategories/Work", "Shopping")
    assert result["ok"] is False


def test_rules_update_sender_mode_moves_sender_and_messages(rules_path, creds, mock_imap):
    mock_imap.search.return_value = ("OK", [b"1"])
    with patch("commonFunctions.connect_to_imap", return_value=mock_imap):
        result = inboxAnalysis.rules_update_sender_mode(
            "boss@work.com", "MailMatrixCategories/Work", "Shopping")
    assert result["ok"] is True
    assert result["moved"] == 1
    saved = json.loads(rules_path.read_text())
    assert "boss@work.com" not in saved["labels"][0]["emailAddresses"]
    assert "boss@work.com" in saved["labels"][1]["emailAddresses"]


# ── rules_convert_domain_mode ──────────────────────────────────────────────────

def test_rules_convert_domain_mode_missing_params(rules_path):
    result = inboxAnalysis.rules_convert_domain_mode("", "MailMatrixCategories/Work")
    assert result["ok"] is False


def test_rules_convert_domain_mode_creates_domain_rule(rules_path):
    result = inboxAnalysis.rules_convert_domain_mode("work.com", "MailMatrixCategories/Work")
    assert result["ok"] is True
    saved = json.loads(rules_path.read_text())
    assert "work.com" in saved["labels"][0]["emailDomains"]
    assert saved["labels"][0]["emailAddresses"] == []
