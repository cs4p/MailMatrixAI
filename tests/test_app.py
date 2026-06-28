import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app as flask_app_module
from app import app as flask_app


# ── fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_RULES = {
    "labels": [
        {
            "labelName": "MailMatrixCategories/Work",
            "emailAddresses": ["boss@work.com"],
            "emailDomains": [],
        },
        {
            "labelName": "MailMatrixCategories/Shopping",
            "emailAddresses": ["orders@amazon.com"],
            "emailDomains": ["shop.example.com"],
        },
    ]
}


@pytest.fixture
def client(tmp_path):
    rules_file = tmp_path / "emailRules.json"
    rules_file.write_text(json.dumps(SAMPLE_RULES))

    summary_dir = tmp_path / "emailSummary"
    summary_dir.mkdir()

    env_file = tmp_path / ".env"

    flask_app.config["TESTING"] = True

    with (
        patch.object(flask_app_module, "RULES_PATH", rules_file),
        patch.object(flask_app_module, "SUMMARY_DIR", summary_dir),
        patch.object(flask_app_module, "ENV_PATH", env_file),
    ):
        with flask_app.test_client() as c:
            yield c


@pytest.fixture
def mock_imap():
    imap = MagicMock()
    imap.login.return_value = ("OK", [b"OK"])
    imap.select.return_value = ("OK", [b"5"])
    imap.search.return_value = ("OK", [b"1 2 3"])
    imap.logout.return_value = ("BYE", [b"Bye"])
    return imap


# ── page routes ───────────────────────────────────────────────────────────────

def test_dashboard_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Dashboard" in resp.data


def test_summaries_page_returns_200(client):
    resp = client.get("/summaries")
    assert resp.status_code == 200


def test_rules_page_returns_200(client):
    resp = client.get("/rules")
    assert resp.status_code == 200
    assert b"Email Rules" in resp.data


def test_rules_page_shows_senders(client):
    resp = client.get("/rules")
    assert b"boss@work.com" in resp.data
    assert b"orders@amazon.com" in resp.data


def test_config_page_returns_200(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert b"IMAP_SERVER" in resp.data


def test_view_summary_not_found(client):
    resp = client.get("/summaries/nonexistent.html")
    assert resp.status_code == 404


def test_view_summary_rejects_non_html(client):
    resp = client.get("/summaries/rules.json")
    assert resp.status_code == 404


# ── /api/inbox-stats ──────────────────────────────────────────────────────────

def test_api_inbox_stats_not_connected_when_no_credentials(client):
    with patch.dict(os.environ, {"IMAP_SERVER": "", "IMAP_USERNAME": "", "IMAP_PASSWORD": ""}):
        resp = client.get("/api/inbox-stats")
    data = resp.get_json()
    assert data["connected"] is False


def test_api_inbox_stats_returns_count_with_mock_imap(client, mock_imap):
    with (
        patch.dict(os.environ, {"IMAP_SERVER": "imap.gmail.com", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p"}),
        patch("app.connect_to_imap", return_value=mock_imap),
    ):
        resp = client.get("/api/inbox-stats")
    data = resp.get_json()
    assert data["connected"] is True
    assert data["inbox_count"] == 3  # "1 2 3".split() has 3 ids


# ── /api/sort ────────────────────────────────────────────────────────────────

def test_api_sort_success(client):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Sorted 5 messages\n"
    mock_result.stderr = ""

    with patch("app.subprocess.run", return_value=mock_result):
        resp = client.post("/api/sort")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True


def test_api_sort_failure(client):
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "IMAP connection refused"

    with patch("app.subprocess.run", return_value=mock_result):
        resp = client.post("/api/sort")

    assert resp.status_code == 500
    data = resp.get_json()
    assert data["ok"] is False
    assert "IMAP" in data["error"]


# ── /api/generate-summary ─────────────────────────────────────────────────────

def test_api_generate_summary_success(client):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("app.subprocess.run", return_value=mock_result):
        resp = client.post(
            "/api/generate-summary",
            data=json.dumps({"date": "2026-06-28"}),
            content_type="application/json",
        )

    data = resp.get_json()
    assert data["ok"] is True
    assert data["filename"] == "email_summary_2026-06-28.html"


def test_api_generate_summary_invalid_date(client):
    resp = client.post(
        "/api/generate-summary",
        data=json.dumps({"date": "not-a-date"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False


def test_api_generate_summary_no_date_uses_today(client):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("app.subprocess.run", return_value=mock_result) as mock_run:
        resp = client.post("/api/generate-summary", data="{}", content_type="application/json")

    data = resp.get_json()
    assert data["ok"] is True
    # Should not have a date arg appended (only "--no-serve")
    call_args = mock_run.call_args[0][0]
    assert "--no-serve" in call_args
    assert "2026" not in call_args  # no date arg was added


# ── /api/rules/delete ─────────────────────────────────────────────────────────

def test_api_rules_delete_sender(client, tmp_path):
    resp = client.post(
        "/api/rules/delete",
        data=json.dumps({
            "type": "sender",
            "address": "boss@work.com",
            "full_label": "MailMatrixCategories/Work",
        }),
        content_type="application/json",
    )
    assert resp.get_json()["ok"] is True

    # Verify it was actually removed
    with patch.object(flask_app_module, "RULES_PATH"):
        flask_app_module.RULES_PATH = (
            next(p for p in [flask_app_module.RULES_PATH] if True)
        )
    # Read current rules via the load helper
    data = flask_app_module._load_rules()
    work = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    assert "boss@work.com" not in work["emailAddresses"]


def test_api_rules_delete_domain(client):
    resp = client.post(
        "/api/rules/delete",
        data=json.dumps({
            "type": "domain",
            "domain": "shop.example.com",
            "full_label": "MailMatrixCategories/Shopping",
        }),
        content_type="application/json",
    )
    assert resp.get_json()["ok"] is True

    data = flask_app_module._load_rules()
    shopping = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Shopping")
    assert "shop.example.com" not in shopping["emailDomains"]


def test_api_rules_delete_invalid_type(client):
    resp = client.post(
        "/api/rules/delete",
        data=json.dumps({"type": "unknown"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


# ── /api/rules/update-sender ──────────────────────────────────────────────────

def test_api_rules_update_sender_moves_to_existing_label(client, mock_imap):
    with patch("app.connect_to_imap", return_value=mock_imap):
        resp = client.post(
            "/api/rules/update-sender",
            data=json.dumps({
                "address": "boss@work.com",
                "old_full_label": "MailMatrixCategories/Work",
                "new_label": "MailMatrixCategories/Shopping",
            }),
            content_type="application/json",
        )

    assert resp.get_json()["ok"] is True

    data = flask_app_module._load_rules()
    work = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    shopping = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Shopping")
    assert "boss@work.com" not in work["emailAddresses"]
    assert "boss@work.com" in shopping["emailAddresses"]


def test_api_rules_update_sender_creates_new_label(client, mock_imap):
    with patch("app.connect_to_imap", return_value=mock_imap):
        resp = client.post(
            "/api/rules/update-sender",
            data=json.dumps({
                "address": "boss@work.com",
                "old_full_label": "MailMatrixCategories/Work",
                "new_label": "NewCategory",
            }),
            content_type="application/json",
        )

    assert resp.get_json()["ok"] is True

    data = flask_app_module._load_rules()
    new_entry = next(
        (e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/NewCategory"),
        None,
    )
    assert new_entry is not None
    assert "boss@work.com" in new_entry["emailAddresses"]


def test_api_rules_update_sender_missing_fields(client):
    resp = client.post(
        "/api/rules/update-sender",
        data=json.dumps({"address": "boss@work.com"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


# ── /api/rules/convert-domain ─────────────────────────────────────────────────

def test_api_rules_convert_domain(client):
    resp = client.post(
        "/api/rules/convert-domain",
        data=json.dumps({
            "domain": "work.com",
            "full_label": "MailMatrixCategories/Work",
        }),
        content_type="application/json",
    )
    assert resp.get_json()["ok"] is True

    data = flask_app_module._load_rules()
    work = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    assert "work.com" in work["emailDomains"]


def test_api_rules_convert_domain_removes_subsumed_senders(client):
    # Add a @work.com sender to Work label first
    with patch.object(flask_app_module, "RULES_PATH", flask_app_module.RULES_PATH):
        data = flask_app_module._load_rules()
        work = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Work")
        work["emailAddresses"].append("extra@work.com")
        flask_app_module._save_rules(data)

    resp = client.post(
        "/api/rules/convert-domain",
        data=json.dumps({
            "domain": "work.com",
            "full_label": "MailMatrixCategories/Work",
        }),
        content_type="application/json",
    )
    assert resp.get_json()["ok"] is True

    data = flask_app_module._load_rules()
    work = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    # @work.com senders should be removed since the domain rule covers them
    for addr in work["emailAddresses"]:
        assert not addr.endswith("@work.com")


def test_api_rules_convert_domain_missing_params(client):
    resp = client.post(
        "/api/rules/convert-domain",
        data=json.dumps({"domain": "work.com"}),  # missing full_label
        content_type="application/json",
    )
    assert resp.status_code == 400


# ── /api/test-connection ──────────────────────────────────────────────────────

def test_api_test_connection_no_credentials(client):
    with patch.dict(os.environ, {"IMAP_SERVER": "", "IMAP_USERNAME": "", "IMAP_PASSWORD": ""}):
        resp = client.get("/api/test-connection")
    data = resp.get_json()
    assert data["ok"] is False
    assert "credentials" in data["error"].lower()


def test_api_test_connection_success(client, mock_imap):
    with (
        patch.dict(os.environ, {"IMAP_SERVER": "imap.gmail.com", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p"}),
        patch("app.connect_to_imap", return_value=mock_imap),
    ):
        resp = client.get("/api/test-connection")
    data = resp.get_json()
    assert data["ok"] is True
    assert "imap.gmail.com" in data["message"]
    mock_imap.logout.assert_called_once()


def test_api_test_connection_imap_error(client):
    with (
        patch.dict(os.environ, {"IMAP_SERVER": "imap.gmail.com", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p"}),
        patch("app.connect_to_imap", side_effect=Exception("Connection refused")),
    ):
        resp = client.get("/api/test-connection")
    data = resp.get_json()
    assert data["ok"] is False
    assert "Connection refused" in data["error"]


# ── /api/config ───────────────────────────────────────────────────────────────

def test_api_config_saves_allowed_keys(client, tmp_path):
    resp = client.post(
        "/api/config",
        data=json.dumps({"IMAP_SERVER": "imap.example.com", "IMAP_PORT": "993"}),
        content_type="application/json",
    )
    assert resp.get_json()["ok"] is True
    assert os.environ.get("IMAP_SERVER") == "imap.example.com"


def test_api_config_ignores_disallowed_keys(client):
    resp = client.post(
        "/api/config",
        data=json.dumps({"DANGEROUS_KEY": "value", "IMAP_SERVER": "imap.test.com"}),
        content_type="application/json",
    )
    assert resp.get_json()["ok"] is True
    assert "DANGEROUS_KEY" not in os.environ
