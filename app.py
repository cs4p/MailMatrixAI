import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv, dotenv_values
from flask import Flask, g, jsonify, redirect, render_template, request, send_file, url_for

import commonFunctions
from emailSummary import accept_filing, analyze_with_claude, deduplicate_inbox_emails
from cleanupRules import find_domain_collapsible, find_duplicate_addresses
from commonFunctions import (
    add_sender_to_label_rule,
    build_rule_groups,
    collapse_domain_rule,
    connect_to_imap,
    convert_domain_rule,
    credential_keys_in_keychain,
    dashboard_stats,
    delete_rule,
    extract_body_snippet,
    extract_email_address,
    extract_message_parts,
    fetch_many,
    full_label_name,
    get_all_labels,
    get_attachment,
    get_credential,
    imap_call as _imap_call,
    list_folders,
    load_rules_file,
    move_imap_messages,
    move_message_uid,
    parse_headers,
    resolve_duplicate_address,
    save_rules_file,
    send_smtp,
    set_credential,
    setup_logging,
    summary_files,
    uid_search_all,
    update_sender_rule,
    validate_email_address,
    validate_folder,
    validate_new_folder_name,
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
_CREDENTIAL_KEYS = {"IMAP_SERVER", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
                    "SMTP_SERVER", "SMTP_PORT", "ANTHROPIC_API_KEY"}

CLAUDE_BATCH_SIZE = 50
_INBOX_JOB_MAX_AGE = 600  # seconds a finished job's state is kept around for polling
_inbox_jobs: dict = {}
_inbox_jobs_lock = threading.Lock()


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


def _json_body() -> dict:
    """POST body as a dict — {} for empty, malformed, or non-object bodies
    (so handlers fall through to their own "missing field" 400s instead of
    500ing on `None.get`).
    """
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


# The dashboard polls /api/inbox-stats, and each uncached call is a full IMAP
# connect+login. Cache the count briefly; sort/accept invalidate it so the
# dashboard reflects those moves immediately.
_INBOX_COUNT_TTL = 30.0
_inbox_count_cache = {"value": None, "at": 0.0}
_inbox_count_lock = threading.Lock()


def _invalidate_inbox_count() -> None:
    with _inbox_count_lock:
        _inbox_count_cache["value"] = None
        _inbox_count_cache["at"] = 0.0


def _inbox_count() -> int:
    with _inbox_count_lock:
        fresh = (
            _inbox_count_cache["value"] is not None
            and time.monotonic() - _inbox_count_cache["at"] < _INBOX_COUNT_TTL
        )
        if fresh:
            return _inbox_count_cache["value"]
    count = _inbox_count_uncached()
    if count >= 0:  # don't cache failures — retry on the next poll
        with _inbox_count_lock:
            _inbox_count_cache["value"] = count
            _inbox_count_cache["at"] = time.monotonic()
    return count


def _inbox_count_uncached() -> int:
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
    # Same "optimizations" count the /cleanup page uses to decide empty-vs-not.
    optimization_count = len(find_domain_collapsible(rules)) + len(find_duplicate_addresses(rules))

    return render_template(
        "dashboard.html",
        label_count=stats["label_count"],
        rules_count=stats["rules_count"],
        summary_count=stats["summary_count"],
        recent_days=stats["recent_days"],
        today=today.isoformat(),
        custom_default=stats["custom_default"],
        optimization_count=optimization_count,
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
        "SMTP_SERVER": get_credential("SMTP_SERVER"),
        "SMTP_PORT": get_credential("SMTP_PORT"),
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
        _invalidate_inbox_count()
        return jsonify({"ok": True, "output": result.stdout[-2000:]})
    log.error("Sort failed in %.1fs (exit=%d): %s", elapsed, result.returncode, result.stderr[-500:])
    return jsonify({"ok": False, "error": result.stderr[-2000:]}), 500


@app.route("/api/generate-summary", methods=["POST"])
def api_generate_summary():
    body = _json_body()
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
    body = _json_body()
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
    if result.get("ok"):
        _invalidate_inbox_count()
    return jsonify(result)


@app.route("/api/config", methods=["POST"])
def api_config():
    data = _json_body()
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
    body = _json_body()
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
    body = _json_body()
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
    body = _json_body()
    domain = body.get("domain", "").strip()
    full_label = body.get("full_label", "").strip()
    purge_other_labels = bool(body.get("purge_other_labels"))

    if not domain or not full_label:
        return jsonify({"ok": False, "error": "Missing domain or label"}), 400

    data = _load_rules()
    convert_domain_rule(data, domain, full_label, purge_other_labels=purge_other_labels)
    _save_rules(data)
    log.info("Domain rule created: *@%s → %s%s", domain, full_label,
              " (purged other-label rules for this domain)" if purge_other_labels else "")
    return jsonify({"ok": True})


@app.route("/inbox")
def inbox():
    return render_template("inbox.html")


def _reindexed(items: list, batch_start: int, batch_len: int) -> list:
    """Offset Claude's 1-based per-batch indices to positions in the full
    deduped sender list, dropping items whose index is missing or malformed
    (Claude occasionally returns imperfect JSON) instead of failing the job.
    """
    out = []
    for item in items:
        idx = item.get("index") if isinstance(item, dict) else None
        if not isinstance(idx, int) or isinstance(idx, bool) or not (1 <= idx <= batch_len):
            log.warning("Dropping analysis item with bad index: %r", item)
            continue
        out.append({**item, "index": idx + batch_start})
    return out


def _analyze_inbox(progress_cb=None, cancel_event=None) -> dict:
    """Fetch all current INBOX messages, then call Claude in batches of
    CLAUDE_BATCH_SIZE unique senders at a time. progress_cb(phase, current, total)
    is invoked during both the IMAP fetch phase and the Claude analysis phase;
    cancel_event is checked between messages and between batches so a caller
    running this in a background thread can stop it early.
    """
    progress_cb = progress_cb or (lambda phase, current, total: None)
    cancel_event = cancel_event or threading.Event()

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
    cancelled = False
    try:
        labels = get_all_labels(imap, "MailMatrixCategories")
        _imap_call(lambda: imap.select("INBOX", readonly=True))
        status, data = _imap_call(lambda: imap.search(None, "ALL"))
        msg_ids = data[0].split() if status == "OK" and data[0] else []
        log.info("INBOX fetch: %d messages", len(msg_ids))

        # Combined header+body FETCH, one chunk of messages per round trip;
        # cancellation and progress are checked between chunks. Read the chunk
        # size through the module so tests can patch it.
        chunk_size = commonFunctions.FETCH_CHUNK_SIZE
        for chunk_start in range(0, len(msg_ids), chunk_size):
            if cancel_event.is_set():
                cancelled = True
                break
            chunk = msg_ids[chunk_start:chunk_start + chunk_size]
            progress_cb("fetching", chunk_start, len(msg_ids))
            try:
                fetched = fetch_many(
                    imap, chunk,
                    "(BODY[HEADER.FIELDS (FROM SUBJECT DATE CONTENT-TYPE)] BODY[TEXT]<0.2000>)",
                )
            except Exception as exc:
                log.error("Error fetching INBOX chunk at %d: %s", chunk_start, exc)
                continue
            for msg_id in chunk:
                entry = fetched.get(msg_id, {})
                header_bytes = entry.get("header")
                if header_bytes is None:
                    continue
                headers = parse_headers(header_bytes.decode(errors="replace"))
                body_bytes = entry.get("text") or b""
                snippet = extract_body_snippet(header_bytes, body_bytes) if body_bytes else ""
                raw_emails.append({
                    "msg_id": msg_id,
                    "from_display": headers["from"],
                    "from_addr": extract_email_address(headers["from"]),
                    "subject": headers["subject"] or "(no subject)",
                    "date": headers["date"],
                    "body_snippet": snippet,
                })
            progress_cb("fetching", min(chunk_start + len(chunk), len(msg_ids)), len(msg_ids))
    finally:
        imap.logout()

    if cancelled:
        return {"cancelled": True}

    emails = deduplicate_inbox_emails(raw_emails)
    total_senders = len(emails)
    log.info("Deduplicated to %d unique senders; calling Claude in batches of %d",
              total_senders, CLAUDE_BATCH_SIZE)

    action_required = []
    filing_suggestions = []
    error = None
    for batch_start in range(0, total_senders, CLAUDE_BATCH_SIZE):
        if cancel_event.is_set():
            cancelled = True
            break
        batch = emails[batch_start:batch_start + CLAUDE_BATCH_SIZE]
        progress_cb("analyzing", batch_start, total_senders)
        analysis = analyze_with_claude(batch, labels)
        if analysis.get("_error") and not error:
            error = analysis["_error"]
        # analyze_with_claude numbers emails 1..len(batch) local to this call;
        # offset back to the sender's position in the full deduped list.
        action_required.extend(_reindexed(analysis.get("action_required", []), batch_start, len(batch)))
        filing_suggestions.extend(_reindexed(analysis.get("filing_suggestions", []), batch_start, len(batch)))
        progress_cb("analyzing", batch_start + len(batch), total_senders)

    if cancelled:
        return {"cancelled": True}

    return {
        "ok": True,
        "emails": [
            {k: v for k, v in em.items() if k != "msg_id"}
            for em in emails
        ],
        "labels": labels,
        "action_required": action_required,
        "filing_suggestions": filing_suggestions,
        "error": error,
    }


def _prune_inbox_jobs() -> None:
    cutoff = time.time() - _INBOX_JOB_MAX_AGE
    for jid in [j for j, job in _inbox_jobs.items() if job["created_at"] < cutoff]:
        del _inbox_jobs[jid]


def _get_job(job_id: str):
    """Look up a job under the lock so a concurrent prune can't race the read.

    Inner job dicts need no locking: the worker thread is the only writer and
    always *replaces* values (progress dicts, result) atomically. It also sets
    "result" before "status", so a poller can never see status=done with
    result=None — keep that write order.
    """
    with _inbox_jobs_lock:
        return _inbox_jobs.get(job_id)


def _run_inbox_job(job_id: str) -> None:
    job = _get_job(job_id)
    if job is None:  # pruned before the thread got scheduled — nothing to do
        return

    def progress_cb(phase, current, total):
        job["progress"] = {"phase": phase, "current": current, "total": total}

    try:
        result = _analyze_inbox(progress_cb=progress_cb, cancel_event=job["cancel_event"])
    except Exception as exc:
        log.exception("Inbox analysis job %s failed", job_id)
        job["status"] = "error"
        job["result"] = {"ok": False, "error": str(exc)}
        return

    if result.get("cancelled"):
        job["status"] = "cancelled"
    elif not result.get("ok"):
        job["status"] = "error"
        job["result"] = result
    else:
        job["status"] = "done"
        job["result"] = result


def _start_job_thread(job_id: str) -> None:
    threading.Thread(target=_run_inbox_job, args=(job_id,), daemon=True).start()


@app.route("/api/inbox-analyze/start", methods=["POST"])
def api_inbox_analyze_start():
    with _inbox_jobs_lock:
        _prune_inbox_jobs()
        job_id = uuid.uuid4().hex
        _inbox_jobs[job_id] = {
            "status": "running",
            "progress": {"phase": "fetching", "current": 0, "total": 0},
            "result": None,
            "cancel_event": threading.Event(),
            "created_at": time.time(),
        }
    _start_job_thread(job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/inbox-analyze/status/<job_id>")
def api_inbox_analyze_status(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "result": job["result"],
    })


@app.route("/api/inbox-analyze/cancel/<job_id>", methods=["POST"])
def api_inbox_analyze_cancel(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    job["cancel_event"].set()
    return jsonify({"ok": True})


@app.route("/cleanup")
def cleanup():
    data = _load_rules()
    collapses = find_domain_collapsible(data)
    duplicates = find_duplicate_addresses(data)
    return render_template("cleanup.html", collapses=collapses, duplicates=duplicates)


@app.route("/api/cleanup/collapse-domain", methods=["POST"])
def api_cleanup_collapse_domain():
    body = _json_body()
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
    body = _json_body()
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


# ── Mail client (UID-based /api/mail surface for the /mail page) ─────────────

# Folder list changes rarely but the sidebar asks for it on every page load —
# cache it like the inbox count; folder creation invalidates.
_FOLDERS_TTL = 60.0
_folders_cache = {"value": None, "at": 0.0}
_folders_lock = threading.Lock()

_UID_RE = re.compile(r"^\d+$")
_MAIL_PAGE_SIZE = 50


def _invalidate_folders_cache() -> None:
    with _folders_lock:
        _folders_cache["value"] = None
        _folders_cache["at"] = 0.0


def _mail_imap():
    """Connect for one /api/mail request. Returns (imap, None) on success or
    (None, (response, status)) ready to return from the handler."""
    server = get_credential("IMAP_SERVER")
    user = get_credential("IMAP_USERNAME")
    pw = get_credential("IMAP_PASSWORD")
    port = int(get_credential("IMAP_PORT", "993") or "993")
    if not (server and user and pw):
        return None, (jsonify({"ok": False, "error": "IMAP credentials not configured"}), 400)
    try:
        return connect_to_imap(server, user, pw, port), None
    except Exception as exc:
        return None, (jsonify({"ok": False, "error": f"IMAP connection failed: {exc}"}), 502)


def _fetch_full_message(imap, folder: str, uid: str, readonly: bool = True):
    """Return the raw RFC822 bytes of one message via BODY.PEEK[], or None."""
    status, _ = _imap_call(lambda: imap.select(f'"{folder}"', readonly=readonly))
    if status != "OK":
        return None
    status, data = _imap_call(lambda: imap.uid("FETCH", uid, "(BODY.PEEK[])"))
    if status != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


@app.route("/mail")
def mail():
    # The client needs the user's own address to exclude it from Reply-All.
    return render_template("mail.html", username=get_credential("IMAP_USERNAME"))


@app.route("/api/mail/folders")
def api_mail_folders():
    with _folders_lock:
        fresh = (
            _folders_cache["value"] is not None
            and time.monotonic() - _folders_cache["at"] < _FOLDERS_TTL
        )
        if fresh:
            return jsonify({"ok": True, "folders": _folders_cache["value"]})

    imap, err = _mail_imap()
    if err:
        return err
    try:
        folders = list_folders(imap)
    except Exception as exc:
        log.error("Folder list failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        imap.logout()

    with _folders_lock:
        _folders_cache["value"] = folders
        _folders_cache["at"] = time.monotonic()
    return jsonify({"ok": True, "folders": folders})


@app.route("/api/mail/messages")
def api_mail_messages():
    folder = request.args.get("folder", "")
    if not validate_folder(folder):
        return jsonify({"ok": False, "error": "Invalid folder"}), 400
    try:
        page = max(1, int(request.args.get("page", "1")))
        page_size = min(200, max(1, int(request.args.get("page_size", str(_MAIL_PAGE_SIZE)))))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid page"}), 400

    imap, err = _mail_imap()
    if err:
        return err
    try:
        uids = uid_search_all(imap, folder)
        total = len(uids)
        page_uids = uids[::-1][(page - 1) * page_size: page * page_size]  # newest first
        fetched = fetch_many(
            imap, page_uids,
            "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
            use_uid=True,
        )
        messages = []
        for uid in page_uids:
            entry = fetched.get(uid)
            if not entry or entry.get("header") is None:
                continue
            headers = parse_headers(entry["header"].decode(errors="replace"))
            flags = entry.get("flags") or b""
            messages.append({
                "uid": uid.decode(),
                "from_display": headers["from"],
                "from_addr": extract_email_address(headers["from"]),
                "subject": headers["subject"] or "(no subject)",
                "date": headers["date"],
                "seen": b"\\Seen" in flags,
            })
        return jsonify({"ok": True, "folder": folder, "total": total,
                        "page": page, "page_size": page_size, "messages": messages})
    except Exception as exc:
        log.error("Message list failed for %s: %s", folder, exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        imap.logout()


@app.route("/api/mail/message")
def api_mail_message():
    folder = request.args.get("folder", "")
    uid = request.args.get("uid", "")
    if not validate_folder(folder) or not _UID_RE.match(uid):
        return jsonify({"ok": False, "error": "Invalid folder or uid"}), 400

    imap, err = _mail_imap()
    if err:
        return err
    try:
        raw = _fetch_full_message(imap, folder, uid, readonly=False)
        if raw is None:
            return jsonify({"ok": False, "error": "Message not found"}), 404
        parts = extract_message_parts(raw)
        # Opening a message marks it read — standard client behavior. Best
        # effort: a failed STORE shouldn't fail the view.
        try:
            _imap_call(lambda: imap.uid("STORE", uid, "+FLAGS", "\\Seen"))
        except Exception as exc:
            log.warning("Could not mark %s uid %s as seen: %s", folder, uid, exc)
        return jsonify({
            "ok": True,
            "uid": uid,
            "headers": parts["headers"],
            "body_text": parts["text"],
            "body_html": parts["html"],
            "attachments": parts["attachments"],
        })
    except Exception as exc:
        log.error("Message fetch failed for %s uid %s: %s", folder, uid, exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        imap.logout()


@app.route("/api/mail/attachment")
def api_mail_attachment():
    folder = request.args.get("folder", "")
    uid = request.args.get("uid", "")
    part = request.args.get("part", "")
    if not validate_folder(folder) or not _UID_RE.match(uid) or not _UID_RE.match(part):
        return jsonify({"ok": False, "error": "Invalid parameters"}), 400

    imap, err = _mail_imap()
    if err:
        return err
    try:
        raw = _fetch_full_message(imap, folder, uid, readonly=True)
        if raw is None:
            return jsonify({"ok": False, "error": "Message not found"}), 404
        attachment = get_attachment(raw, int(part))
        if attachment is None:
            return jsonify({"ok": False, "error": "Attachment not found"}), 404
        filename, _content_type, payload = attachment
        # Always octet-stream: server-fetched HTML/SVG must never render
        # same-origin with the app.
        return send_file(BytesIO(payload), as_attachment=True,
                         download_name=filename, mimetype="application/octet-stream")
    except Exception as exc:
        log.error("Attachment fetch failed for %s uid %s part %s: %s", folder, uid, part, exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        imap.logout()


@app.route("/api/mail/move", methods=["POST"])
def api_mail_move():
    body = _json_body()
    folder = (body.get("folder") or "").strip()
    uid = (body.get("uid") or "").strip()
    target = (body.get("target") or "").strip()
    if not validate_folder(folder) or not validate_folder(target) or not _UID_RE.match(uid):
        return jsonify({"ok": False, "error": "Invalid folder, target, or uid"}), 400
    if folder == target:
        return jsonify({"ok": False, "error": "Source and target are the same folder"}), 400

    imap, err = _mail_imap()
    if err:
        return err
    try:
        result = move_message_uid(imap, folder, uid, target)
    except Exception as exc:
        log.error("Move failed for %s uid %s → %s: %s", folder, uid, target, exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        imap.logout()

    if not result["ok"]:
        status = 404 if result.get("error") == "Message not found" else 502
        return jsonify({"ok": False, "error": result.get("error", "Move failed")}), status

    if "INBOX" in (folder.upper(), target.upper()):
        _invalidate_inbox_count()

    # Dropping onto a MailMatrixCategories/* label also creates a sender rule;
    # any other target (Trash, Archive, ...) is just a move.
    rule_created = False
    from_addr = result.get("from_addr", "")
    if validate_label(target) and validate_email_address(from_addr):
        data = _load_rules()
        add_sender_to_label_rule(data, from_addr, target)
        _save_rules(data)
        rule_created = True
        log.info("Rule created via drag-drop: %s → %s", from_addr, target)

    return jsonify({"ok": True, "rule_created": rule_created, "from_addr": from_addr})


def _append_to_sent(msg: EmailMessage) -> bool:
    """Save a copy of a just-sent message to the Sent folder. Fastmail (unlike
    Gmail) doesn't auto-save SMTP-sent mail. Best-effort — the mail already
    went out, so failures only mean no Sent copy."""
    imap, err = _mail_imap()
    if err:
        return False
    try:
        sent = next((f["name"] for f in list_folders(imap) if f["special"] == "sent"), None)
        if not sent:
            log.warning("No Sent folder found — sent copy not saved")
            return False
        status, _ = _imap_call(lambda: imap.append(f'"{sent}"', "\\Seen", None, msg.as_bytes()))
        return status == "OK"
    except Exception as exc:
        log.warning("Could not save sent copy: %s", exc)
        return False
    finally:
        imap.logout()


@app.route("/api/mail/send", methods=["POST"])
def api_mail_send():
    body = _json_body()

    def _addr_list(field: str) -> list:
        value = body.get(field) or []
        if isinstance(value, str):
            value = [v.strip() for v in re.split(r"[,;]", value) if v.strip()]
        return [v for v in value if v]

    to = _addr_list("to")
    cc = _addr_list("cc")
    bcc = _addr_list("bcc")
    subject = (body.get("subject") or "").strip()
    text = body.get("body") or ""
    in_reply_to = (body.get("in_reply_to") or "").strip()
    references = (body.get("references") or "").strip()

    if not to:
        return jsonify({"ok": False, "error": "Missing recipient"}), 400
    for addr in to + cc + bcc:
        if not validate_email_address(addr):
            return jsonify({"ok": False, "error": f"Invalid recipient: {addr}"}), 400
    for value in (subject, in_reply_to, references):
        if "\r" in value or "\n" in value:  # header injection
            return jsonify({"ok": False, "error": "Invalid header value"}), 400

    username = get_credential("IMAP_USERNAME")
    password = get_credential("IMAP_PASSWORD")
    if not (username and password):
        return jsonify({"ok": False, "error": "Credentials not configured"}), 400
    smtp_server = get_credential("SMTP_SERVER") or "smtp.fastmail.com"
    try:
        smtp_port = int(get_credential("SMTP_PORT") or "465")
    except ValueError:
        smtp_port = 465

    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        # smtplib.send_message uses Bcc for the envelope but strips the header
        # from the transmitted copy; the Sent copy keeps it (sender's record).
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(text)

    try:
        send_smtp(smtp_server, smtp_port, username, password, msg)
    except Exception as exc:
        log.error("SMTP send failed: %s", exc)
        return jsonify({"ok": False, "error": f"Send failed: {exc}"}), 502
    log.info("Mail sent to %s (%d recipient(s))", ", ".join(to), len(to + cc + bcc))

    return jsonify({"ok": True, "sent_copy_saved": _append_to_sent(msg)})


@app.route("/api/mail/folders/create", methods=["POST"])
def api_mail_folders_create():
    body = _json_body()
    name = (body.get("name") or "").strip()
    if not validate_new_folder_name(name):
        return jsonify({"ok": False, "error": "Invalid folder name"}), 400

    imap, err = _mail_imap()
    if err:
        return err
    try:
        status, data = _imap_call(lambda: imap.create(f'"{name}"'))
    except Exception as exc:
        log.error("Folder create failed for %s: %s", name, exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        imap.logout()

    if status != "OK":
        detail = data[0].decode(errors="replace") if data and data[0] else "CREATE failed"
        return jsonify({"ok": False, "error": detail}), 502
    _invalidate_folders_cache()
    log.info("Folder created: %s", name)
    return jsonify({"ok": True})


if __name__ == "__main__":
    # MAILMATRIX_HOST/PORT let the Electron wrapper (electron/main.js) run the
    # server on a free port; debug (the Werkzeug reloader + debugger) is
    # opt-in rather than always-on.
    host = os.environ.get("MAILMATRIX_HOST", "127.0.0.1")
    port = int(os.environ.get("MAILMATRIX_PORT", "5000"))
    debug = os.environ.get("MAILMATRIX_DEBUG", "").lower() in ("1", "true", "yes")
    log.info("Starting MailMatrix AI on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=debug)
