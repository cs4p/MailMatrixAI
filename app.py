import logging
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv, dotenv_values
from flask import Flask, g, jsonify, redirect, render_template, request, send_file, url_for

from emailSummary import accept_filing, analyze_with_claude, deduplicate_inbox_emails
from cleanupRules import find_domain_collapsible, find_duplicate_addresses
from commonFunctions import (
    build_rule_groups,
    collapse_domain_rule,
    connect_to_imap,
    convert_domain_rule,
    credential_keys_in_keychain,
    dashboard_stats,
    delete_rule,
    extract_body_snippet,
    extract_email_address,
    full_label_name,
    get_all_labels,
    get_credential,
    imap_call as _imap_call,
    load_rules_file,
    move_imap_messages,
    parse_headers,
    resolve_duplicate_address,
    save_rules_file,
    set_credential,
    setup_logging,
    summary_files,
    update_sender_rule,
    validate_label,
)

load_dotenv()  # kept for .env → Keychain migration only (see _migrate_env_to_keychain)
setup_logging("app.log")
logging.getLogger("werkzeug").setLevel(logging.ERROR)  # our after_request hook handles request logs

log = logging.getLogger(__name__)

app = Flask(__name__)


@app.before_request
def _check_csrf():
    if request.method == "POST":
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            log.warning("CSRF check failed: %s %s", request.method, request.path)
            return jsonify({"ok": False, "error": "Forbidden"}), 403


@app.before_request
def _record_start():
    g.start = time.monotonic()


@app.after_request
def _log_request(response):
    ms = (time.monotonic() - getattr(g, "start", time.monotonic())) * 1000
    level = logging.DEBUG if request.path == "/api/inbox-stats" else logging.INFO
    log.log(level, "%s %s → %d  (%.0f ms)", request.method, request.path, response.status_code, ms)
    return response


@app.teardown_request
def _log_exception(exc):
    if exc:
        log.exception("Unhandled exception in %s %s", request.method, request.path)


BASE_DIR = Path(__file__).parent
RULES_PATH = BASE_DIR / "emailRules.json"
SUMMARY_DIR = BASE_DIR / "emailSummary"
ENV_PATH = BASE_DIR / ".env"
_sort_lock = threading.Lock()
_CREDENTIAL_KEYS = {"IMAP_SERVER", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD", "ANTHROPIC_API_KEY"}


def _migrate_env_to_keychain() -> None:
    """One-time migration: copy .env credentials to macOS Keychain on first run."""
    if not ENV_PATH.exists():
        return
    try:
        values = dotenv_values(str(ENV_PATH))
        existing_keys = credential_keys_in_keychain()
        migrated = []
        for key in _CREDENTIAL_KEYS:
            val = (values.get(key) or "").strip()
            if val and key not in existing_keys:
                set_credential(key, val)
                migrated.append(key)
        if migrated:
            log.info("Migrated %d credential(s) from .env to macOS Keychain: %s",
                     len(migrated), ", ".join(sorted(migrated)))
            log.info("You may now delete .env — credentials are stored in macOS Keychain.")
    except Exception as exc:
        log.warning("Could not migrate .env to Keychain: %s", exc)


_migrate_env_to_keychain()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_rules() -> dict:
    return load_rules_file(RULES_PATH)


def _inbox_count() -> int:
    try:
        server = get_credential("IMAP_SERVER")
        user = get_credential("IMAP_USERNAME")
        pw = get_credential("IMAP_PASSWORD")
        port = int(get_credential("IMAP_PORT", "993"))
        if not (server and user and pw):
            return -1
        imap = connect_to_imap(server, user, pw, port)
        # M7: SELECT returns the EXISTS count directly — no need for SEARCH ALL
        status, data = imap.select("INBOX", readonly=True)
        imap.logout()
        if status == "OK" and data and data[0]:
            count = int(data[0])
            log.debug("Inbox count: %d", count)
            return count
        return 0
    except Exception as exc:
        log.warning("Inbox count failed: %s", exc)
        return -1


def _save_rules(data: dict) -> None:
    save_rules_file(RULES_PATH, data)


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    rules = _load_rules()
    today = date.today()
    stats = dashboard_stats(rules, SUMMARY_DIR, today)

    return render_template(
        "dashboard.html",
        label_count=stats["label_count"],
        rules_count=stats["rules_count"],
        summary_count=stats["summary_count"],
        recent_days=stats["recent_days"],
        today=today.isoformat(),
    )


@app.route("/summaries")
def summaries():
    files = summary_files(SUMMARY_DIR)
    return render_template("summaries.html", files=files)


@app.route("/summaries/<filename>")
def view_summary(filename: str):
    path = (SUMMARY_DIR / filename).resolve()
    # M2: confirm the resolved path is actually inside SUMMARY_DIR
    if not path.is_relative_to(SUMMARY_DIR.resolve()) or path.suffix != ".html" or not path.exists():
        return "Not found", 404
    # Serve the HTML file; Accept buttons POST to /accept on the app server
    return send_file(path, mimetype="text/html")


@app.route("/rules")
def rules():
    data = _load_rules()
    grouped = build_rule_groups(data)

    return render_template(
        "rules.html",
        groups=grouped["groups"],
        label_names=grouped["label_names"],
        total_senders=grouped["total_senders"],
        total_domain_rules=grouped["total_domain_rules"],
    )


@app.route("/config")
def config():
    cfg = {
        "IMAP_SERVER": get_credential("IMAP_SERVER"),
        "IMAP_PORT": get_credential("IMAP_PORT", "993"),
        "IMAP_USERNAME": get_credential("IMAP_USERNAME"),
        "IMAP_PASSWORD": get_credential("IMAP_PASSWORD"),
        "ANTHROPIC_API_KEY": get_credential("ANTHROPIC_API_KEY"),
    }
    return render_template("config.html", cfg=cfg)


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/inbox-stats")
def api_inbox_stats():
    count = _inbox_count()
    return jsonify({"inbox_count": count, "connected": count >= 0})


@app.route("/api/sort", methods=["POST"])
def api_sort():
    log.info("Sort inbox triggered")
    t0 = time.monotonic()
    with _sort_lock:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "sortEmail.py")],
            capture_output=True, text=True, timeout=120,
        )
    elapsed = time.monotonic() - t0
    if result.returncode == 0:
        log.info("Sort completed in %.1fs", elapsed)
        return jsonify({"ok": True, "output": result.stdout[-2000:]})
    log.error("Sort failed in %.1fs (exit=%d): %s", elapsed, result.returncode, result.stderr[-500:])
    return jsonify({"ok": False, "error": result.stderr[-2000:]}), 500


@app.route("/api/generate-summary", methods=["POST"])
def api_generate_summary():
    body = request.get_json(force=True) or {}
    target_date = (body.get("date") or "").strip()
    if target_date:
        try:
            date.fromisoformat(target_date)
        except ValueError:
            return jsonify({"ok": False, "error": f"Invalid date: {target_date}"}), 400

    cmd = [sys.executable, str(BASE_DIR / "emailSummary.py"), "--no-serve"]
    if target_date:
        cmd.append(target_date)

    log.info("Summary generation triggered for %s", target_date or "today")
    t0 = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    elapsed = time.monotonic() - t0

    if result.returncode == 0:
        resolved_date = target_date or date.today().isoformat()
        filename = f"email_summary_{resolved_date}.html"
        log.info("Summary generated in %.1fs: %s", elapsed, filename)
        return jsonify({"ok": True, "filename": filename})
    log.error("Summary failed in %.1fs (exit=%d): %s", elapsed, result.returncode, result.stderr[-500:])
    return jsonify({"ok": False, "error": result.stderr[-2000:]}), 500


@app.route("/accept", methods=["POST"])
def accept():
    body = request.get_json(force=True)
    # H3: validate label before passing to accept_filing
    label = (body.get("label") or "").strip()
    if not validate_label(label):
        return jsonify({"ok": False, "error": "Invalid label"}), 400
    result = accept_filing(
        body=body,
        imap_server=get_credential("IMAP_SERVER"),
        imap_port=int(get_credential("IMAP_PORT", "993")),
        username=get_credential("IMAP_USERNAME"),
        password=get_credential("IMAP_PASSWORD"),
        rules_path=str(RULES_PATH),
    )
    return jsonify(result)


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(force=True)
    updated = []
    for key, val in data.items():
        if key in _CREDENTIAL_KEYS and val is not None:
            set_credential(key, str(val))
            updated.append(key)
    if updated:
        log.info("Config updated: %s", ", ".join(sorted(updated)))
    return jsonify({"ok": True})


@app.route("/api/test-connection")
def api_test_connection():
    server = get_credential("IMAP_SERVER")
    user = get_credential("IMAP_USERNAME")
    pw = get_credential("IMAP_PASSWORD")
    port = int(get_credential("IMAP_PORT", "993"))
    if not (server and user and pw):
        log.warning("Test connection failed: credentials not configured")
        return jsonify({"ok": False, "error": "IMAP credentials not configured"})
    log.info("Testing IMAP connection to %s:%d as %s", server, port, user)
    try:
        imap = connect_to_imap(server, user, pw, port)
        imap.logout()
        log.info("IMAP connection test OK: %s", server)
        return jsonify({"ok": True, "message": f"Connected to {server}"})
    except Exception as exc:
        log.warning("IMAP connection test failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/rules/delete", methods=["POST"])
def api_rules_delete():
    body = request.get_json(force=True)
    kind = body.get("type")          # "sender" or "domain"
    full_label = body.get("full_label", "").strip()
    if kind not in ("sender", "domain"):
        return jsonify({"ok": False, "error": "Invalid type"}), 400

    data = _load_rules()
    changed = delete_rule(
        data, kind, full_label,
        address=body.get("address", "").strip(),
        domain=body.get("domain", "").strip(),
    )
    if changed:
        _save_rules(data)
        log.info("Rule deleted: %s → %s", body.get("address") or body.get("domain"), full_label)
    return jsonify({"ok": True})


@app.route("/api/rules/update-sender", methods=["POST"])
def api_rules_update_sender():
    body = request.get_json(force=True)
    address = body.get("address", "").strip()
    old_full_label = body.get("old_full_label", "").strip()
    new_label_input = body.get("new_label", "").strip()
    new_full_label = full_label_name(new_label_input)

    if not address or not old_full_label or not new_label_input:
        return jsonify({"ok": False, "error": "Missing required fields"}), 400
    if not validate_label(new_full_label):  # M9: reject malformed or traversal labels
        return jsonify({"ok": False, "error": "Invalid label name"}), 400

    data = _load_rules()
    update_sender_rule(data, address, old_full_label, new_full_label)
    _save_rules(data)
    log.info("Rule updated: <%s> %s → %s", address, old_full_label, new_full_label)

    # Move matching messages from old label to new label
    move = move_imap_messages(
        address, old_full_label, new_full_label,
        imap_server=get_credential("IMAP_SERVER"),
        imap_port=int(get_credential("IMAP_PORT", "993")),
        username=get_credential("IMAP_USERNAME"),
        password=get_credential("IMAP_PASSWORD"),
    )
    return jsonify({"ok": True, "moved": move.get("moved", 0), "imap": move})


@app.route("/api/rules/convert-domain", methods=["POST"])
def api_rules_convert_domain():
    body = request.get_json(force=True)
    domain = body.get("domain", "").strip()
    full_label = body.get("full_label", "").strip()

    if not domain or not full_label:
        return jsonify({"ok": False, "error": "Missing domain or label"}), 400

    data = _load_rules()
    convert_domain_rule(data, domain, full_label)
    _save_rules(data)
    log.info("Domain rule created: *@%s → %s", domain, full_label)
    return jsonify({"ok": True})


@app.route("/inbox")
def inbox():
    return render_template("inbox.html")


def _analyze_inbox() -> dict:
    """Fetch all current INBOX messages, call Claude, return analysis JSON."""
    server = get_credential("IMAP_SERVER")
    user = get_credential("IMAP_USERNAME")
    pw = get_credential("IMAP_PASSWORD")
    port = int(get_credential("IMAP_PORT", "993"))
    if not (server and user and pw):
        return {"ok": False, "error": "IMAP credentials not configured"}

    try:
        imap = connect_to_imap(server, user, pw, port)
    except Exception as exc:
        return {"ok": False, "error": f"IMAP connection failed: {exc}"}

    raw_emails = []
    labels = []
    try:
        labels = get_all_labels(imap, "MailMatrixCategories")
        _imap_call(lambda: imap.select("INBOX", readonly=True))
        status, data = _imap_call(lambda: imap.search(None, "ALL"))
        msg_ids = data[0].split() if status == "OK" and data[0] else []
        log.info("INBOX fetch: %d messages", len(msg_ids))

        for msg_id in msg_ids:
            try:
                st, raw_data = _imap_call(
                    lambda mid=msg_id: imap.fetch(mid, "(BODY[HEADER.FIELDS (FROM SUBJECT DATE CONTENT-TYPE)])")
                )
                if st != "OK" or not raw_data or not raw_data[0]:
                    continue
                header_bytes = raw_data[0][1]
                headers = parse_headers(header_bytes.decode(errors="replace"))

                snippet = ""
                try:
                    st2, body_data = _imap_call(
                        lambda mid=msg_id: imap.fetch(mid, "(BODY[TEXT]<0.2000>)")
                    )
                    if st2 == "OK" and body_data and body_data[0] and isinstance(body_data[0], tuple):
                        raw_body = body_data[0][1]
                        if raw_body:
                            snippet = extract_body_snippet(header_bytes, raw_body)
                except Exception:
                    pass

                raw_emails.append({
                    "msg_id": msg_id,
                    "from_display": headers["from"],
                    "from_addr": extract_email_address(headers["from"]),
                    "subject": headers["subject"] or "(no subject)",
                    "date": headers["date"],
                    "body_snippet": snippet,
                })
            except Exception as exc:
                log.error("Error fetching INBOX message %s: %s", msg_id, exc)
    finally:
        imap.logout()

    emails = deduplicate_inbox_emails(raw_emails)
    log.info("Deduplicated to %d unique senders; calling Claude", len(emails))
    analysis = analyze_with_claude(emails, labels)

    return {
        "ok": True,
        "emails": [
            {k: v for k, v in em.items() if k != "msg_id"}
            for em in emails
        ],
        "labels": labels,
        "action_required": analysis.get("action_required", []),
        "filing_suggestions": analysis.get("filing_suggestions", []),
        "error": analysis.get("_error"),
    }


@app.route("/api/inbox-analyze")
def api_inbox_analyze():
    result = _analyze_inbox()
    if not result.get("ok"):
        return jsonify(result), 500
    return jsonify(result)


@app.route("/cleanup")
def cleanup():
    data = _load_rules()
    collapses = find_domain_collapsible(data)
    duplicates = find_duplicate_addresses(data)
    return render_template("cleanup.html", collapses=collapses, duplicates=duplicates)


@app.route("/api/cleanup/collapse-domain", methods=["POST"])
def api_cleanup_collapse_domain():
    body = request.get_json(force=True)
    domain = body.get("domain", "").strip()
    if not domain:
        return jsonify({"ok": False, "error": "Missing domain"}), 400

    data = _load_rules()
    # M8: refuse to collapse if there's no domain rule to take over routing
    removed = collapse_domain_rule(data, domain)
    if removed is None:
        return jsonify({"ok": False, "error": f"No domain rule found for @{domain}"}), 400

    if removed:
        _save_rules(data)
    log.info("Domain collapse *@%s: removed %d sender rule(s)", domain, removed)
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/cleanup/resolve-duplicate", methods=["POST"])
def api_cleanup_resolve_duplicate():
    body = request.get_json(force=True)
    address = body.get("address", "").strip()
    keep_label = body.get("keep_label", "").strip()
    if not address or not keep_label:
        return jsonify({"ok": False, "error": "Missing address or keep_label"}), 400

    data = _load_rules()
    removed = resolve_duplicate_address(data, address, keep_label)

    if removed:
        _save_rules(data)
    log.info("Duplicate resolved: kept <%s> in %s, removed from %d label(s)", address, keep_label, removed)
    return jsonify({"ok": True, "removed": removed})


if __name__ == "__main__":
    log.info("Starting MailMatrix AI on http://localhost:5000")
    app.run(debug=True, port=5000)
