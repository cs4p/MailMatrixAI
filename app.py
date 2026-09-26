import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv, dotenv_values
from flask import Flask, g, jsonify, redirect, render_template, request, send_file, url_for

import commonFunctions
from emailSummary import (
    ANALYSIS_MODELS,
    DEFAULT_ANALYSIS_MODEL,
    SummaryError,
    accept_filing,
    analysis_model,
    analyze_senders,
    analyze_with_claude,
    collect_day,
    count_day,
    deduplicate_inbox_emails,
    open_imap,
)
import resortEmail
from resortEmail import resort, resort_max_messages
from sortEmail import load_rules as load_sort_rules
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
    folder_unread_counts,
    full_label_name,
    get_all_labels,
    get_attachment,
    get_credential,
    imap_call as _imap_call,
    list_folders,
    load_rules_file,
    merge_rules,
    move_imap_messages,
    move_message_uid,
    parse_headers,
    read_token_usage,
    resolve_duplicate_address,
    save_rules_file,
    send_smtp,
    set_credential,
    setup_logging,
    summarize_token_usage,
    uid_search_all,
    update_sender_rule,
    validate_email_address,
    validate_folder,
    validate_new_folder_name,
    validate_label,
    validate_rules_document,
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
# Persistent state (learned filing rules, token-usage log) lives in DATA_DIR.
# Defaults to the code directory so desktop/Electron/test runs are unchanged; set
# MAILMATRIX_DATA_DIR to a mounted volume to relocate it (used by the container image).
DATA_DIR = Path(os.environ.get("MAILMATRIX_DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
RULES_PATH = DATA_DIR / "emailRules.json"
ENV_PATH = BASE_DIR / ".env"
_sort_lock = threading.Lock()
# One resort at a time: two concurrent reconciles would race on the same
# COPY/EXPUNGE work and could double-file a message.
_resort_lock = threading.Lock()
# Whitelist for /api/config. RESORT_MAX_MESSAGES and ANALYSIS_MODEL are settings
# rather than credentials, but it rides in the same Keychain blob so the CLI scripts pick it
# up from os.environ the same way (see set_credential).
_CREDENTIAL_KEYS = {"IMAP_SERVER", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
                    "SMTP_SERVER", "SMTP_PORT", "ANTHROPIC_API_KEY",
                    "RESORT_MAX_MESSAGES", "ANALYSIS_MODEL"}

CLAUDE_BATCH_SIZE = 50
_INBOX_JOB_MAX_AGE = 600  # seconds a finished job's state is kept around for polling
# Background analysis jobs — both /inbox and /summary/<date> run through these.
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
    # Anything that moves mail in or out of INBOX changes the per-day summary
    # counts too.
    _invalidate_summary_counts()


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
    stats = dashboard_stats(rules, today)
    # Same "optimizations" count the /cleanup page uses to decide empty-vs-not.
    optimization_count = len(find_domain_collapsible(rules)) + len(find_duplicate_addresses(rules))

    return render_template(
        "dashboard.html",
        label_count=stats["label_count"],
        rules_count=stats["rules_count"],
        recent_days=stats["recent_days"],
        today=today.isoformat(),
        custom_default=stats["custom_default"],
        optimization_count=optimization_count,
    )


def _parse_day(value: str):
    """A YYYY-MM-DD string as a date, or None if it isn't one."""
    try:
        return date.fromisoformat(value) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or "") else None
    except ValueError:
        return None


@app.route("/summary")
@app.route("/summary/<day>")
def summary_page(day: str = None):
    """Live daily summary. The page is a shell; its data comes from a
    background job (/api/summary/<date>/start) so it renders immediately."""
    today = date.today()
    target = today if day is None else _parse_day(day)
    if target is None:
        return "Not found — use /summary/YYYY-MM-DD", 404
    return render_template(
        "summary.html",
        day=target.isoformat(),
        heading=f"{target.strftime('%B')} {target.day}, {target.year}",
        prev_day=(target - timedelta(days=1)).isoformat(),
        next_day=(target + timedelta(days=1)).isoformat() if target < today else None,
        today=today.isoformat(),
        model_labels={model: info["label"] for model, info in ANALYSIS_MODELS.items()},
    )


# Summaries used to be saved files; keep old bookmarks working.
@app.route("/summaries")
def summaries():
    return redirect(url_for("summary_page"))


@app.route("/summaries/<filename>")
def legacy_summary_file(filename: str):
    m = re.fullmatch(r"email_summary_(\d{4}-\d{2}-\d{2})\.html", filename)
    if not m or _parse_day(m.group(1)) is None:
        return "Not found", 404
    return redirect(url_for("summary_page", day=m.group(1)))


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
        "RESORT_MAX_MESSAGES": get_credential("RESORT_MAX_MESSAGES"),
        "ANALYSIS_MODEL": analysis_model(),
    }
    return render_template("config.html", cfg=cfg,
                           resort_default=resortEmail.DEFAULT_MAX_MESSAGES,
                           analysis_models=ANALYSIS_MODELS,
                           default_analysis_model=DEFAULT_ANALYSIS_MODEL)


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/inbox-stats")
def api_inbox_stats():
    count = _inbox_count()
    return jsonify({"inbox_count": count, "connected": count >= 0})


@app.route("/api/token-usage")
def api_token_usage():
    records = read_token_usage()
    return jsonify({
        "last_7_days": summarize_token_usage(records, 7),
        "last_30_days": summarize_token_usage(records, 30),
    })


def _child_env() -> dict:
    """Environment for the CLI subprocesses: pass the UI's already-resolved
    RULES_PATH so the child reads the exact same file, even when the working
    dir differs from MAILMATRIX_DATA_DIR (e.g. a container with WORKDIR /app)."""
    return {**os.environ, "RULES_PATH": str(RULES_PATH)}


@app.route("/api/sort", methods=["POST"])
def api_sort():
    log.info("Sort inbox triggered")
    t0 = time.monotonic()
    with _sort_lock:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "sortEmail.py")],
            capture_output=True, text=True, timeout=120, env=_child_env(),
        )
    elapsed = time.monotonic() - t0
    if result.returncode == 0:
        log.info("Sort completed in %.1fs", elapsed)
        _invalidate_inbox_count()
        return jsonify({"ok": True, "output": result.stdout[-2000:]})
    log.error("Sort failed in %.1fs (exit=%d): %s", elapsed, result.returncode, result.stderr[-500:])
    return jsonify({"ok": False, "error": result.stderr[-2000:]}), 500


@app.route("/api/resort", methods=["POST"])
def api_resort():
    """Reconcile every MailMatrixCategories/* folder against emailRules.json.

    `?dryrun=1` (or {"dryrun": true}) reports what would change without writing
    anything — the /rules button always asks for that first and only posts the
    applying call after the user confirms.
    """
    body = _json_body()
    dryrun = request.args.get("dryrun") == "1" or bool(body.get("dryrun"))

    if not _resort_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "A resort is already running"}), 409
    try:
        server = get_credential("IMAP_SERVER")
        user = get_credential("IMAP_USERNAME")
        pw = get_credential("IMAP_PASSWORD")
        port = int(get_credential("IMAP_PORT", "993") or "993")
        if not (server and user and pw):
            return jsonify({"ok": False, "error": "IMAP credentials not configured"}), 400

        try:
            email_to_labels, domain_to_labels = load_sort_rules(str(RULES_PATH))
        except (json.JSONDecodeError, OSError) as exc:
            return jsonify({"ok": False, "error": f"Could not load rules: {exc}"}), 400
        if not (email_to_labels or domain_to_labels):
            # With no rules every filed message looks unmatched, so a run would
            # be a guaranteed no-op — say so instead of scanning the mailbox.
            return jsonify({"ok": False, "error": "No rules configured — nothing to reconcile"}), 400

        log.info("Resort triggered (%s)", "dry run" if dryrun else "apply")
        t0 = time.monotonic()
        try:
            imap = connect_to_imap(server, user, pw, port)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"IMAP connection failed: {exc}"}), 502
        try:
            result = resort(
                imap, email_to_labels, domain_to_labels,
                apply=not dryrun,
                max_messages=resort_max_messages(),
            )
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except Exception as exc:
        log.exception("Resort failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        _resort_lock.release()

    log.info("Resort %s finished in %.1fs: %s",
             "dry run" if dryrun else "apply", time.monotonic() - t0, result["totals"])
    if not dryrun:
        _invalidate_inbox_count()
        _invalidate_folders_cache()
    return jsonify(result)


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
    limit = data.get("RESORT_MAX_MESSAGES")
    if limit not in (None, ""):
        # A junk value here would silently fall back to the default on every
        # run — reject it at the form instead.
        try:
            if int(str(limit)) < 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": "Max messages per resort must be a whole number (0 = no limit)"}), 400
    model = data.get("ANALYSIS_MODEL")
    if model not in (None, "") and model not in ANALYSIS_MODELS:
        return jsonify({"ok": False, "error": "Unknown analysis model"}), 400
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


# Guard against oversized uploads exhausting memory; a rules file is small JSON.
_RULES_IMPORT_MAX_BYTES = 5 * 1024 * 1024


@app.route("/api/rules/import", methods=["POST"])
def api_rules_import():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    raw = upload.read(_RULES_IMPORT_MAX_BYTES + 1)
    if len(raw) > _RULES_IMPORT_MAX_BYTES:
        return jsonify({"ok": False, "error": "File too large (max 5 MB)"}), 400

    try:
        incoming = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return jsonify({"ok": False, "error": f"Not valid JSON: {exc}"}), 400

    error = validate_rules_document(incoming)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    data = _load_rules()
    summary = merge_rules(data, incoming)
    _save_rules(data)

    log.info(
        "Rules imported from %s: +%d addresses, +%d domains across %d new / %d updated labels",
        upload.filename,
        summary["addresses_added"], summary["domains_added"],
        len(summary["labels_created"]), len(summary["labels_updated"]),
    )
    return jsonify({"ok": True, "summary": summary})


@app.route("/inbox")
def inbox():
    return render_template("inbox.html")


def _analyze_inbox(progress_cb=None, cancel_event=None) -> dict:
    """Fetch all current INBOX messages, then analyze them per sender through
    analyze_senders (batches of CLAUDE_BATCH_SIZE, cached — see
    emailSummary.ANALYSIS_CACHE). progress_cb(phase, current, total) is invoked
    during both the IMAP fetch phase and the Claude analysis phase;
    cancel_event is checked between chunks and between batches so a caller
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

    emails = [{k: v for k, v in em.items() if k != "msg_id"}
              for em in deduplicate_inbox_emails(raw_emails)]
    log.info("Deduplicated to %d unique senders", len(emails))

    # analyze_fn is passed explicitly (resolved from this module at call time)
    # so tests can patch app.analyze_with_claude.
    analysis = analyze_senders(
        emails, labels,
        caller="inbox-analyze",
        batch_size=CLAUDE_BATCH_SIZE,
        analyze_fn=analyze_with_claude,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
    )
    if analysis.get("cancelled"):
        return {"cancelled": True}

    return {
        "ok": True,
        "emails": emails,
        "labels": labels,
        "action_required": analysis["action_required"],
        "filing_suggestions": analysis["filing_suggestions"],
        "error": analysis["error"],
    }


def _prune_inbox_jobs() -> None:
    cutoff = time.time() - _INBOX_JOB_MAX_AGE
    for jid in [j for j, job in _inbox_jobs.items() if job["created_at"] < cutoff]:
        del _inbox_jobs[jid]


def _get_job(job_id: str):
    """Look up a job under the lock so a concurrent prune can't race the read.

    Inner job dicts need no locking: the worker thread is the only writer and
    always *replaces* values (progress dicts, day, result) atomically. It also
    sets "result" before "status", so a poller can never see status=done with
    result=None — keep that write order.
    """
    with _inbox_jobs_lock:
        return _inbox_jobs.get(job_id)


def _new_job(work, kind: str) -> str:
    """Register a job; work(job) runs in the background and returns the result
    dict ({"ok": True, ...}, {"ok": False, "error": ...} or {"cancelled": True})."""
    with _inbox_jobs_lock:
        _prune_inbox_jobs()
        job_id = uuid.uuid4().hex
        _inbox_jobs[job_id] = {
            "kind": kind,
            "work": work,
            "status": "running",
            "progress": {"phase": "fetching", "current": 0, "total": 0},
            "result": None,
            "cancel_event": threading.Event(),
            "created_at": time.time(),
        }
    return job_id


def _run_job(job_id: str) -> None:
    job = _get_job(job_id)
    if job is None:  # pruned before the thread got scheduled — nothing to do
        return
    try:
        result = job["work"](job)
    except Exception as exc:
        log.exception("%s job %s failed", job["kind"], job_id)
        job["result"] = {"ok": False, "error": str(exc)}
        job["status"] = "error"
        return

    if result.get("cancelled"):
        job["status"] = "cancelled"
    elif not result.get("ok"):
        job["result"] = result
        job["status"] = "error"
    else:
        job["result"] = result
        job["status"] = "done"


def _start_job_thread(job_id: str) -> None:
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()


def _job_progress_cb(job):
    def progress_cb(phase, current, total):
        job["progress"] = {"phase": phase, "current": current, "total": total}
    return progress_cb


def _job_of_kind(job_id: str, kind: str):
    job = _get_job(job_id)
    return job if job is not None and job["kind"] == kind else None


def _job_status_response(job, extra=None):
    body = {"status": job["status"], "progress": job["progress"], "result": job["result"]}
    body.update(extra or {})
    return jsonify(body)


@app.route("/api/inbox-analyze/start", methods=["POST"])
def api_inbox_analyze_start():
    def work(job):
        return _analyze_inbox(progress_cb=_job_progress_cb(job), cancel_event=job["cancel_event"])

    job_id = _new_job(work, "inbox")
    _start_job_thread(job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/inbox-analyze/status/<job_id>")
def api_inbox_analyze_status(job_id):
    job = _job_of_kind(job_id, "inbox")
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return _job_status_response(job)


@app.route("/api/inbox-analyze/cancel/<job_id>", methods=["POST"])
def api_inbox_analyze_cancel(job_id):
    job = _job_of_kind(job_id, "inbox")
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    job["cancel_event"].set()
    return jsonify({"ok": True})


# ── Live daily summary ────────────────────────────────────────────────────────
# Per-day counts (IMAP SEARCH only) are cached briefly because the dashboard
# asks for a week at a time; _invalidate_inbox_count() clears them after any
# move. The latest analysis per day supplies "need attention" without AI.

_SUMMARY_COUNTS_TTL = 300.0
_SUMMARY_COUNTS_MAX_DAYS = 31
_summary_counts_cache: dict = {}   # "YYYY-MM-DD" -> (monotonic time, {"filed", "unfiled"})
_day_analysis: dict = {}           # "YYYY-MM-DD" -> {"need_attention", "analyzed_at", "model"}
_summary_state_lock = threading.Lock()


def _invalidate_summary_counts() -> None:
    with _summary_state_lock:
        _summary_counts_cache.clear()


def _summary_work(target: date, force: bool):
    def work(job):
        progress_cb = _job_progress_cb(job)
        try:
            imap = open_imap()
        except SummaryError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"IMAP connection failed: {exc}"}
        try:
            day = collect_day(imap, target)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        # Mailbox data is visible to pollers before the AI step finishes.
        job["day"] = day
        with _summary_state_lock:
            _summary_counts_cache[day["date"]] = (
                time.monotonic(), {"filed": day["counts"]["filed"], "unfiled": day["counts"]["unfiled"]})

        analysis = analyze_senders(
            day["inbox"], day["labels"],
            force=force,
            caller="summary",
            batch_size=CLAUDE_BATCH_SIZE,
            analyze_fn=analyze_with_claude,
            progress_cb=progress_cb,
            cancel_event=job["cancel_event"],
        )
        if analysis.get("cancelled"):
            return {"cancelled": True}
        if not analysis["error"]:
            with _summary_state_lock:
                _day_analysis[day["date"]] = {
                    "need_attention": len(analysis["action_required"]),
                    "analyzed_at": analysis["analyzed_at"],
                    "model": analysis["model"],
                }
        return {"ok": True, "day": day, "analysis": analysis}
    return work


@app.route("/api/summary/<day>/start", methods=["POST"])
def api_summary_start(day):
    target = _parse_day(day)
    if target is None:
        return jsonify({"ok": False, "error": f"Invalid date: {day}"}), 400
    force = bool(_json_body().get("force"))
    job_id = _new_job(_summary_work(target, force), "summary")
    _start_job_thread(job_id)
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/summary/status/<job_id>")
def api_summary_status(job_id):
    job = _job_of_kind(job_id, "summary")
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return _job_status_response(job, {"day": job.get("day")})


@app.route("/api/summary/cancel/<job_id>", methods=["POST"])
def api_summary_cancel(job_id):
    job = _job_of_kind(job_id, "summary")
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    job["cancel_event"].set()
    return jsonify({"ok": True})


@app.route("/api/summary/counts")
def api_summary_counts():
    """Per-day {filed, unfiled, need_attention} for ?dates=YYYY-MM-DD,...

    need_attention is null until that day's summary has been analyzed.
    """
    raw = [d for d in (request.args.get("dates") or "").split(",") if d]
    if not raw or len(raw) > _SUMMARY_COUNTS_MAX_DAYS:
        return jsonify({"ok": False,
                        "error": f"Pass 1-{_SUMMARY_COUNTS_MAX_DAYS} dates as ?dates=YYYY-MM-DD,..."}), 400
    days = []
    for d in raw:
        parsed = _parse_day(d)
        if parsed is None:
            return jsonify({"ok": False, "error": f"Invalid date: {d}"}), 400
        days.append(parsed)

    now = time.monotonic()
    counts = {}
    with _summary_state_lock:
        for d in days:
            hit = _summary_counts_cache.get(d.isoformat())
            if hit and now - hit[0] < _SUMMARY_COUNTS_TTL:
                counts[d.isoformat()] = dict(hit[1])
    missing = [d for d in days if d.isoformat() not in counts]

    if missing:
        try:
            imap = open_imap()
        except SummaryError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"IMAP connection failed: {exc}"}), 502
        try:
            labels = get_all_labels(imap, "MailMatrixCategories")
            for d in missing:
                counts[d.isoformat()] = count_day(imap, labels, d)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        with _summary_state_lock:
            for d in missing:
                _summary_counts_cache[d.isoformat()] = (now, dict(counts[d.isoformat()]))

    with _summary_state_lock:
        for key, entry in counts.items():
            analyzed = _day_analysis.get(key)
            entry["need_attention"] = analyzed["need_attention"] if analyzed else None
    return jsonify({"ok": True, "counts": counts})


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


@app.route("/api/mail/unread")
def api_mail_unread():
    # One STATUS (UNSEEN) per selectable folder — not cached, so counts stay
    # fresh; the client calls this on load and after a move.
    imap, err = _mail_imap()
    if err:
        return err
    try:
        names = [f["name"] for f in list_folders(imap) if f["selectable"]]
        counts = folder_unread_counts(imap, names)
    except Exception as exc:
        log.error("Unread counts failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        imap.logout()
    return jsonify({"ok": True, "unread": counts})


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
