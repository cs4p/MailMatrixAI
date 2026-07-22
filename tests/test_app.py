import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app as flask_app_module
import commonFunctions
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
    # The inbox count is cached at module level — reset it so tests don't see
    # each other's cached counts.
    flask_app_module._invalidate_inbox_count()

    with (
        patch.object(flask_app_module, "RULES_PATH", rules_file),
        patch.object(flask_app_module, "SUMMARY_DIR", summary_dir),
        patch.object(flask_app_module, "ENV_PATH", env_file),
    ):
        with flask_app.test_client() as c:
            c.environ_base['HTTP_X_REQUESTED_WITH'] = 'XMLHttpRequest'
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


def test_dashboard_custom_date_defaults_to_day_before_oldest_recent_day(client):
    from datetime import date, timedelta
    expected = (date.today() - timedelta(days=7)).isoformat()
    resp = client.get("/")
    assert f'id="custom-date"' in resp.data.decode()
    assert f'value="{expected}"' in resp.data.decode()


def test_dashboard_hides_cleanup_widget_when_no_optimizations(client):
    resp = client.get("/")
    assert b"cleanup-widget" not in resp.data


def test_dashboard_summary_row_shows_stats_from_sidecar(client):
    from datetime import date as _date
    today_str = _date.today().isoformat()
    summary_dir = flask_app_module.SUMMARY_DIR
    (summary_dir / f"email_summary_{today_str}.html").write_text("<html></html>")
    (summary_dir / f"email_summary_{today_str}.json").write_text(json.dumps({
        "processed": 42, "need_attention": 3, "unfiled": 5, "filed": 37,
        "generated_at": f"{today_str}T09:05:00",
    }))

    resp = client.get("/")
    html = resp.data.decode()
    assert "42" in html and "processed" in html
    assert "3" in html and "need attention" in html
    assert "5" in html and "unfiled" in html
    assert "Run" in html and "09:05 AM" in html


def test_dashboard_summary_row_no_stats_without_sidecar(client):
    resp = client.get("/")
    html = resp.data.decode()
    assert "summary-stats" not in html
    assert "summary-generated-at" not in html


def test_dashboard_shows_cleanup_widget_when_duplicate_address_exists(client):
    data = flask_app_module._load_rules()
    # File the same address under two labels -> a duplicate-address optimization
    data["labels"][1]["emailAddresses"].append("boss@work.com")
    flask_app_module._save_rules(data)

    resp = client.get("/")
    html = resp.data.decode()
    assert "cleanup-widget" in html
    assert ">1</span>" in html
    assert "optimization found" in html
    assert 'href="/cleanup"' in html


def test_summaries_page_returns_200(client):
    resp = client.get("/summaries")
    assert resp.status_code == 200


def test_summaries_page_shows_stats_from_sidecar(client):
    summary_dir = flask_app_module.SUMMARY_DIR
    (summary_dir / "email_summary_2026-07-16.html").write_text("<html></html>")
    (summary_dir / "email_summary_2026-07-16.json").write_text(json.dumps({
        "processed": 42, "need_attention": 3, "unfiled": 5, "filed": 37,
        "generated_at": "2026-07-16T09:05:00",
    }))

    resp = client.get("/summaries")
    html = resp.data.decode()
    assert "42" in html and "processed" in html
    assert "3" in html and "need attention" in html
    assert "5" in html and "unfiled" in html
    assert "Run Jul 16, 2026 at 09:05 AM" in html


def test_summaries_page_omits_stats_without_sidecar(client):
    summary_dir = flask_app_module.SUMMARY_DIR
    (summary_dir / "email_summary_2026-07-16.html").write_text("<html></html>")

    resp = client.get("/summaries")
    html = resp.data.decode()
    assert "summary-stats" not in html
    assert "summary-generated-at" not in html


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
    assert data["inbox_count"] == 5  # mock SELECT returns ("OK", [b"5"])


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
    assert not any(re.match(r'\d{4}-\d{2}-\d{2}$', str(a)) for a in call_args)


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


def test_api_rules_convert_domain_leaves_other_labels_by_default(client):
    data = flask_app_module._load_rules()
    shopping = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Shopping")
    shopping["emailAddresses"].append("other@work.com")
    flask_app_module._save_rules(data)

    resp = client.post(
        "/api/rules/convert-domain",
        data=json.dumps({"domain": "work.com", "full_label": "MailMatrixCategories/Work"}),
        content_type="application/json",
    )
    assert resp.get_json()["ok"] is True

    data = flask_app_module._load_rules()
    shopping = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Shopping")
    assert "other@work.com" in shopping["emailAddresses"]


def test_api_rules_convert_domain_purges_other_labels_when_requested(client):
    data = flask_app_module._load_rules()
    shopping = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Shopping")
    shopping["emailAddresses"].append("other@work.com")
    flask_app_module._save_rules(data)

    resp = client.post(
        "/api/rules/convert-domain",
        data=json.dumps({
            "domain": "work.com",
            "full_label": "MailMatrixCategories/Work",
            "purge_other_labels": True,
        }),
        content_type="application/json",
    )
    assert resp.get_json()["ok"] is True

    data = flask_app_module._load_rules()
    shopping = next(e for e in data["labels"] if e["labelName"] == "MailMatrixCategories/Shopping")
    assert "other@work.com" not in shopping["emailAddresses"]
    assert "orders@amazon.com" in shopping["emailAddresses"]  # unrelated address untouched


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


def test_api_config_rejected_without_csrf_header(client):
    resp = client.post(
        "/api/config",
        data=json.dumps({"IMAP_SERVER": "imap.example.com"}),
        content_type="application/json",
        headers={"X-Requested-With": ""},  # override the fixture's default
    )
    assert resp.status_code == 403


# ── /cleanup page ─────────────────────────────────────────────────────────────

def test_cleanup_page_returns_200(client):
    resp = client.get("/cleanup")
    assert resp.status_code == 200
    assert b"Cleanup" in resp.data


def test_cleanup_page_shows_empty_state_when_clean(client):
    # SAMPLE_RULES has no domain rules, so no collapses; no duplicate addresses
    resp = client.get("/cleanup")
    assert b"nothing to do" in resp.data.lower() or b"No optimizations" in resp.data


# ── /api/cleanup/collapse-domain ──────────────────────────────────────────────

def test_api_cleanup_collapse_domain_removes_matching_addresses(client, tmp_path):
    # Add a domain rule and a matching sender address to the rules
    rules = {
        "labels": [
            {
                "labelName": "MailMatrixCategories/Work",
                "emailAddresses": ["alice@work.com", "bob@work.com"],
                "emailDomains": ["work.com"],
            }
        ]
    }
    flask_app_module.RULES_PATH.write_text(json.dumps(rules))

    resp = client.post(
        "/api/cleanup/collapse-domain",
        data=json.dumps({"domain": "work.com"}),
        content_type="application/json",
    )
    data = resp.get_json()
    assert data["ok"] is True
    assert data["removed"] == 2

    updated = flask_app_module._load_rules()
    work = next(e for e in updated["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    assert work["emailAddresses"] == []


def test_api_cleanup_collapse_domain_cross_label(client):
    rules = {
        "labels": [
            {
                "labelName": "MailMatrixCategories/Work",
                "emailAddresses": [],
                "emailDomains": ["work.com"],
            },
            {
                "labelName": "MailMatrixCategories/VIP",
                "emailAddresses": ["vip@work.com"],
                "emailDomains": [],
            },
        ]
    }
    flask_app_module.RULES_PATH.write_text(json.dumps(rules))

    resp = client.post(
        "/api/cleanup/collapse-domain",
        data=json.dumps({"domain": "work.com"}),
        content_type="application/json",
    )
    assert resp.get_json()["ok"] is True
    assert resp.get_json()["removed"] == 1

    updated = flask_app_module._load_rules()
    vip = next(e for e in updated["labels"] if e["labelName"] == "MailMatrixCategories/VIP")
    assert "vip@work.com" not in vip["emailAddresses"]


def test_api_cleanup_collapse_domain_missing_param(client):
    resp = client.post(
        "/api/cleanup/collapse-domain",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400


# ── /api/cleanup/resolve-duplicate ───────────────────────────────────────────

def test_api_cleanup_resolve_duplicate_removes_from_other_labels(client):
    rules = {
        "labels": [
            {
                "labelName": "MailMatrixCategories/Work",
                "emailAddresses": ["shared@example.com"],
                "emailDomains": [],
            },
            {
                "labelName": "MailMatrixCategories/VIP",
                "emailAddresses": ["shared@example.com"],
                "emailDomains": [],
            },
        ]
    }
    flask_app_module.RULES_PATH.write_text(json.dumps(rules))

    resp = client.post(
        "/api/cleanup/resolve-duplicate",
        data=json.dumps({
            "address": "shared@example.com",
            "keep_label": "MailMatrixCategories/Work",
        }),
        content_type="application/json",
    )
    data = resp.get_json()
    assert data["ok"] is True
    assert data["removed"] == 1

    updated = flask_app_module._load_rules()
    work = next(e for e in updated["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    vip = next(e for e in updated["labels"] if e["labelName"] == "MailMatrixCategories/VIP")
    assert "shared@example.com" in work["emailAddresses"]
    assert "shared@example.com" not in vip["emailAddresses"]


def test_api_cleanup_resolve_duplicate_missing_params(client):
    resp = client.post(
        "/api/cleanup/resolve-duplicate",
        data=json.dumps({"address": "a@b.com"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


# ── /inbox page ───────────────────────────────────────────────────────────────

def test_inbox_page_returns_200(client):
    resp = client.get("/inbox")
    assert resp.status_code == 200
    assert b"Inbox" in resp.data


# ── /api/inbox-analyze/* (job-based) ───────────────────────────────────────────

_RAW_HEADERS = b"From: Alice Work <alice@work.com>\r\nSubject: Q2 Report\r\nDate: Fri, 27 Jun 2026\r\n"
_RAW_BODY = b"Please review the attached."


def _combined_fetch(headers_for):
    """Build a fetch side_effect emulating a real batched multi-message FETCH
    response: imap.fetch receives a comma-joined ID set and returns, per
    message, a header tuple (prefixed with its sequence number), a body-text
    continuation tuple (no sequence number, and the server echoes the partial
    request <0.2000> back as just <0>), then a bare b")" terminator."""
    def _fetch(id_set, what):
        data = []
        for mid in id_set.split(b","):
            headers = headers_for(mid)
            data.append((mid + b" (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {%d}" % len(headers), headers))
            if "TEXT" in what:
                data.append((b" BODY[TEXT]<0> {%d}" % len(_RAW_BODY), _RAW_BODY))
            data.append(b")")
        return ("OK", data)
    return _fetch


def _inbox_imap(msg_ids: bytes = b"1"):
    """Return a mock IMAP whose fetch answers batched header+body requests."""
    imap = MagicMock()
    imap.select.return_value = ("OK", [b"1"])
    imap.search.return_value = ("OK", [msg_ids])
    imap.logout.return_value = ("BYE", [b""])
    imap.fetch.side_effect = _combined_fetch(lambda mid: _RAW_HEADERS)
    return imap


def _per_sender_headers(mid: bytes) -> bytes:
    n = int(mid)
    return f"From: Sender {n} <sender{n}@work.com>\r\nSubject: Msg {n}\r\nDate: Fri, 27 Jun 2026\r\n".encode()


def _imap_env(monkeypatch):
    monkeypatch.setenv("IMAP_SERVER", "imap.test.com")
    monkeypatch.setenv("IMAP_USERNAME", "user@test.com")
    monkeypatch.setenv("IMAP_PASSWORD", "pw")


def _run_inbox_job_sync(monkeypatch):
    """Make /start run the job synchronously in the request thread instead of
    spawning a real background thread, so status is immediately final."""
    monkeypatch.setattr(flask_app_module, "_start_job_thread", flask_app_module._run_inbox_job)


def _start_and_get_status(client):
    resp = client.post("/api/inbox-analyze/start", headers={"X-Requested-With": "XMLHttpRequest"})
    job_id = resp.get_json()["job_id"]
    return client.get(f"/api/inbox-analyze/status/{job_id}").get_json()


def test_api_inbox_analyze_missing_credentials_returns_error_status(client, monkeypatch):
    monkeypatch.delenv("IMAP_SERVER", raising=False)
    monkeypatch.delenv("IMAP_USERNAME", raising=False)
    monkeypatch.delenv("IMAP_PASSWORD", raising=False)
    _run_inbox_job_sync(monkeypatch)

    data = _start_and_get_status(client)
    assert data["status"] == "error"
    assert data["result"]["ok"] is False


def test_api_inbox_analyze_connection_failure_returns_error_status(client, monkeypatch):
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)

    with patch("app.connect_to_imap", side_effect=OSError("Connection refused")):
        data = _start_and_get_status(client)

    assert data["status"] == "error"
    assert "Connection refused" in data["result"]["error"]


def test_api_inbox_analyze_empty_inbox(client, monkeypatch):
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)
    imap = _inbox_imap(msg_ids=b"")
    imap.search.return_value = ("OK", [b""])

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=["MailMatrixCategories/Work"]),
        patch("app.analyze_with_claude", return_value={"action_required": [], "filing_suggestions": []}),
    ):
        data = _start_and_get_status(client)

    assert data["status"] == "done"
    assert data["result"]["emails"] == []
    assert data["result"]["labels"] == ["MailMatrixCategories/Work"]


def test_api_inbox_analyze_returns_emails_and_suggestions(client, monkeypatch):
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)
    imap = _inbox_imap(msg_ids=b"1")
    analysis = {
        "action_required": [{"index": 1, "from": "alice@work.com", "subject": "Q2 Report", "reason": "Reply needed"}],
        "filing_suggestions": [{"index": 1, "from": "alice@work.com", "suggested_label": "MailMatrixCategories/Work", "is_new_label": False, "reason": "Work email"}],
    }

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=["MailMatrixCategories/Work"]),
        patch("app.analyze_with_claude", return_value=analysis),
    ):
        data = _start_and_get_status(client)

    result = data["result"]
    assert data["status"] == "done"
    assert len(result["emails"]) == 1
    assert result["emails"][0]["from_addr"] == "alice@work.com"
    assert result["emails"][0]["subject"] == "Q2 Report"
    assert result["action_required"] == analysis["action_required"]
    assert result["filing_suggestions"] == analysis["filing_suggestions"]
    assert result["labels"] == ["MailMatrixCategories/Work"]
    assert result["error"] is None


def test_api_inbox_analyze_deduplicates_same_sender(client, monkeypatch):
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)
    # Two message IDs, both from the same sender
    imap = _inbox_imap(msg_ids=b"1 2")

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=[]),
        patch("app.analyze_with_claude", return_value={"action_required": [], "filing_suggestions": []}),
    ):
        data = _start_and_get_status(client)

    assert len(data["result"]["emails"]) == 1
    assert data["result"]["emails"][0]["count"] == 2


def test_api_inbox_analyze_claude_error_surfaces_in_response(client, monkeypatch):
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)
    imap = _inbox_imap(msg_ids=b"1")

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=[]),
        patch("app.analyze_with_claude", return_value={"action_required": [], "filing_suggestions": [], "_error": "API key invalid"}),
    ):
        data = _start_and_get_status(client)

    assert data["status"] == "done"
    assert data["result"]["error"] == "API key invalid"


def test_api_inbox_analyze_msg_id_not_in_response(client, monkeypatch):
    """IMAP msg_id (bytes) must be stripped — it can't serialize to JSON."""
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)
    imap = _inbox_imap(msg_ids=b"1")

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=[]),
        patch("app.analyze_with_claude", return_value={"action_required": [], "filing_suggestions": []}),
    ):
        data = _start_and_get_status(client)

    for em in data["result"]["emails"]:
        assert "msg_id" not in em


def test_api_inbox_analyze_no_retrieval_cap(client, monkeypatch):
    """All INBOX messages are fetched — there is no hard cap on raw retrieval."""
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)
    msg_id_list = " ".join(str(i) for i in range(1, 151)).encode()
    imap = _inbox_imap(msg_ids=msg_id_list)

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=[]),
        patch("app.analyze_with_claude", return_value={"action_required": [], "filing_suggestions": []}),
    ):
        data = _start_and_get_status(client)

    assert data["status"] == "done"
    # 150 raw messages, all from the same sender in _RAW_HEADERS -> 1 unique sender
    assert data["result"]["emails"][0]["count"] == 150


def test_api_inbox_analyze_batches_claude_calls_and_remaps_indices(client, monkeypatch):
    """More than CLAUDE_BATCH_SIZE unique senders must be split into multiple
    Claude calls, with each batch's local index offset back to the sender's
    global position in the deduped list."""
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)

    n_senders = 120
    msg_id_list = " ".join(str(i) for i in range(1, n_senders + 1)).encode()
    imap = _inbox_imap(msg_ids=msg_id_list)
    imap.fetch.side_effect = _combined_fetch(_per_sender_headers)

    def _fake_analyze(batch, labels):
        return {
            "action_required": [],
            "filing_suggestions": [
                {"index": i + 1, "from": em["from_addr"], "suggested_label": "MailMatrixCategories/Work",
                 "is_new_label": False, "reason": "x"}
                for i, em in enumerate(batch)
            ],
        }

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=[]),
        patch("app.analyze_with_claude", side_effect=_fake_analyze) as mock_analyze,
    ):
        data = _start_and_get_status(client)

    assert data["status"] == "done"
    assert mock_analyze.call_count == 3  # 50 + 50 + 20
    suggestions = data["result"]["filing_suggestions"]
    global_indices = sorted(s["index"] for s in suggestions)
    assert global_indices == list(range(1, n_senders + 1))


def _fixed_job_id(monkeypatch, job_id="fixed-test-job-id"):
    """The job runs synchronously inside POST /start (via _run_inbox_job_sync),
    so a test-side callback that wants to flip cancel_event mid-run needs the
    job_id before the response comes back. Pin uuid4 so it's known upfront."""
    monkeypatch.setattr(flask_app_module.uuid, "uuid4", lambda: type("_U", (), {"hex": job_id})())
    return job_id


def test_api_inbox_analyze_cancel_during_fetch(client, monkeypatch):
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)
    job_id = _fixed_job_id(monkeypatch)
    # Shrink the FETCH chunk size so 10 messages take multiple round trips,
    # then cancel after the second chunk — later chunks must not be fetched.
    monkeypatch.setattr(commonFunctions, "FETCH_CHUNK_SIZE", 3)
    msg_id_list = " ".join(str(i) for i in range(1, 11)).encode()
    imap = _inbox_imap(msg_ids=msg_id_list)

    fetch_count = {"n": 0}
    real_fetch = imap.fetch.side_effect

    def _counting_fetch(id_set, what):
        fetch_count["n"] += 1
        if fetch_count["n"] == 2:
            flask_app_module._inbox_jobs[job_id]["cancel_event"].set()
        return real_fetch(id_set, what)

    imap.fetch.side_effect = _counting_fetch

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=[]),
        patch("app.analyze_with_claude") as mock_analyze,
    ):
        client.post("/api/inbox-analyze/start", headers={"X-Requested-With": "XMLHttpRequest"})

    data = client.get(f"/api/inbox-analyze/status/{job_id}").get_json()
    assert data["status"] == "cancelled"
    assert fetch_count["n"] == 2  # chunks 3 and 4 never fetched
    mock_analyze.assert_not_called()


def test_api_inbox_analyze_cancel_between_batches(client, monkeypatch):
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)
    job_id = _fixed_job_id(monkeypatch)
    n_senders = 120
    msg_id_list = " ".join(str(i) for i in range(1, n_senders + 1)).encode()
    imap = _inbox_imap(msg_ids=msg_id_list)
    imap.fetch.side_effect = _combined_fetch(_per_sender_headers)

    def _cancel_after_first_batch(batch, labels):
        flask_app_module._inbox_jobs[job_id]["cancel_event"].set()
        return {"action_required": [], "filing_suggestions": []}

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=[]),
        patch("app.analyze_with_claude", side_effect=_cancel_after_first_batch) as mock_analyze,
    ):
        client.post("/api/inbox-analyze/start", headers={"X-Requested-With": "XMLHttpRequest"})

    data = client.get(f"/api/inbox-analyze/status/{job_id}").get_json()
    assert data["status"] == "cancelled"
    mock_analyze.assert_called_once()  # second batch never ran


def test_api_inbox_analyze_unknown_job_returns_404(client):
    resp = client.get("/api/inbox-analyze/status/does-not-exist")
    assert resp.status_code == 404


# ── malformed POST bodies must not 500 ────────────────────────────────────────

@pytest.mark.parametrize("raw_body", ["null", "[1, 2]", "{broken", ""])
def test_post_handlers_tolerate_malformed_json_bodies(client, raw_body):
    endpoints = [
        "/accept",
        "/api/config",
        "/api/rules/delete",
        "/api/rules/update-sender",
        "/api/rules/convert-domain",
        "/api/cleanup/collapse-domain",
        "/api/cleanup/resolve-duplicate",
        "/api/generate-summary",
    ]
    # /api/generate-summary treats an empty body as "today" and would spawn
    # the real emailSummary.py subprocess — stub it out.
    summary_result = MagicMock(returncode=0, stdout="", stderr="")
    with patch("app.subprocess.run", return_value=summary_result):
        for endpoint in endpoints:
            resp = client.post(endpoint, data=raw_body, content_type="application/json")
            # Handlers fall through to their own validation, never a 500 —
            # /api/config treats an empty dict as "nothing to update" (200).
            assert resp.status_code in (200, 400), f"{endpoint} returned {resp.status_code} for {raw_body!r}"


# ── _reindexed guards malformed Claude analysis items ─────────────────────────

def test_reindexed_offsets_and_drops_bad_indices():
    items = [
        {"index": 1, "from": "a@x.com"},
        {"index": 2, "from": "b@x.com"},
        {"from": "missing-index@x.com"},
        {"index": "3", "from": "string-index@x.com"},
        {"index": True, "from": "bool-index@x.com"},
        {"index": 99, "from": "out-of-range@x.com"},
        "not-a-dict",
    ]
    out = flask_app_module._reindexed(items, batch_start=50, batch_len=50)
    assert [o["index"] for o in out] == [51, 52]


def test_api_inbox_analyze_survives_malformed_analysis_items(client, monkeypatch):
    _imap_env(monkeypatch)
    _run_inbox_job_sync(monkeypatch)
    imap = _inbox_imap(msg_ids=b"1")
    analysis = {
        "action_required": [{"from": "no-index@x.com"}],
        "filing_suggestions": [{"index": 1, "from": "alice@work.com",
                                "suggested_label": "MailMatrixCategories/Work"}],
    }

    with (
        patch("app.connect_to_imap", return_value=imap),
        patch("app.get_all_labels", return_value=[]),
        patch("app.analyze_with_claude", return_value=analysis),
    ):
        data = _start_and_get_status(client)

    assert data["status"] == "done"  # bad item dropped, job not failed
    assert data["result"]["action_required"] == []
    assert data["result"]["filing_suggestions"][0]["index"] == 1


# ── inbox-count caching ───────────────────────────────────────────────────────

def test_api_inbox_stats_caches_count_between_polls(client, mock_imap):
    with (
        patch.dict(os.environ, {"IMAP_SERVER": "imap.gmail.com", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p"}),
        patch("app.connect_to_imap", return_value=mock_imap) as mock_connect,
    ):
        first = client.get("/api/inbox-stats").get_json()
        second = client.get("/api/inbox-stats").get_json()

    assert first["inbox_count"] == second["inbox_count"] == 5
    mock_connect.assert_called_once()  # second poll served from cache


def test_api_inbox_stats_failure_not_cached(client):
    with patch.dict(os.environ, {"IMAP_SERVER": "", "IMAP_USERNAME": "", "IMAP_PASSWORD": ""}):
        assert client.get("/api/inbox-stats").get_json()["connected"] is False
    # Credentials appear later — the earlier failure must not be served back
    mock_imap = MagicMock()
    mock_imap.select.return_value = ("OK", [b"7"])
    mock_imap.logout.return_value = ("BYE", [b""])
    with (
        patch.dict(os.environ, {"IMAP_SERVER": "imap.gmail.com", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p"}),
        patch("app.connect_to_imap", return_value=mock_imap),
    ):
        data = client.get("/api/inbox-stats").get_json()
    assert data["connected"] is True
    assert data["inbox_count"] == 7


def test_api_sort_invalidates_inbox_count_cache(client, mock_imap):
    env = {"IMAP_SERVER": "imap.gmail.com", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p"}
    with (
        patch.dict(os.environ, env),
        patch("app.connect_to_imap", return_value=mock_imap) as mock_connect,
    ):
        client.get("/api/inbox-stats")

        sort_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("app.subprocess.run", return_value=sort_result):
            client.post("/api/sort")

        client.get("/api/inbox-stats")

    assert mock_connect.call_count == 2  # cache was invalidated by the sort
