import json
from unittest.mock import patch

import pytest

import inboxAnalysis


@pytest.fixture(autouse=True)
def _no_real_keychain():
    # Credentials must come from os.environ in tests, not this machine's real Keychain.
    with patch("keyring.get_password", return_value=None):
        yield


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
