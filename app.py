import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv, set_key
from flask import Flask, g, jsonify, redirect, render_template, request, send_file, url_for

from emailSummary import accept_filing
from commonFunctions import connect_to_imap, imap_call as _imap_call, setup_logging

load_dotenv()
setup_logging("app.log")
logging.getLogger("werkzeug").setLevel(logging.ERROR)  # our after_request hook handles request logs

log = logging.getLogger(__name__)

app = Flask(__name__)


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
_rules_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_rules() -> dict:
    if RULES_PATH.exists():
        with open(RULES_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"labels": []}


def _inbox_count() -> int:
    try:
        server = os.getenv("IMAP_SERVER", "")
        user = os.getenv("IMAP_USERNAME", "")
        pw = os.getenv("IMAP_PASSWORD", "")
        port = int(os.getenv("IMAP_PORT", 993))
        if not (server and user and pw):
            return -1
        imap = connect_to_imap(server, user, pw, port)
        imap.select("INBOX", readonly=True)
        status, data = imap.search(None, "ALL")
        imap.logout()
        if status == "OK" and data[0]:
            count = len(data[0].split())
            log.debug("Inbox count: %d", count)
            return count
        return 0
    except Exception as exc:
        log.warning("Inbox count failed: %s", exc)
        return -1


def _full_label(name: str) -> str:
    if name.startswith("MailMatrixCategories/"):
        return name
    return f"MailMatrixCategories/{name}"


def _save_rules(data: dict) -> None:
    with _rules_lock:
        with open(RULES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def _move_imap_messages(address: str, from_full_label: str, to_full_label: str) -> dict:
    log.info("Moving messages for <%s>: %s → %s", address, from_full_label, to_full_label)
    try:
        server = os.getenv("IMAP_SERVER", "")
        user = os.getenv("IMAP_USERNAME", "")
        pw = os.getenv("IMAP_PASSWORD", "")
        port = int(os.getenv("IMAP_PORT", 993))
        if not (server and user and pw):
            log.warning("IMAP move skipped: credentials not configured")
            return {"ok": False, "error": "IMAP not configured", "moved": 0}

        imap = connect_to_imap(server, user, pw, port)
        try:
            ok, _ = _imap_call(lambda: imap.select(f'"{from_full_label}"'))
            if ok != "OK":
                log.warning("Cannot select folder %s", from_full_label)
                return {"ok": False, "error": f"Cannot select {from_full_label}", "moved": 0}

            ok, data = _imap_call(lambda: imap.search(None, f'FROM "{address}"'))
            msg_ids = data[0].split() if ok == "OK" and data[0] else []

            for mid in msg_ids:
                _imap_call(lambda m=mid: imap.copy(m, f'"{to_full_label}"'))
                _imap_call(lambda m=mid: imap.store(m, '+FLAGS', r'\Deleted'))

            if msg_ids:
                _imap_call(lambda: imap.expunge())

            log.info("Moved %d message(s) for <%s>", len(msg_ids), address)
            return {"ok": True, "moved": len(msg_ids)}
        finally:
            imap.logout()
    except Exception as exc:
        log.error("IMAP move failed for <%s>: %s", address, exc)
        return {"ok": False, "error": str(exc), "moved": 0}


def _summary_files() -> list[dict]:
    files = []
    if SUMMARY_DIR.exists():
        for p in sorted(SUMMARY_DIR.glob("email_summary_*.html"), reverse=True):
            date_part = p.stem.replace("email_summary_", "")
            try:
                d = date.fromisoformat(date_part)
                label = d.strftime("%B %d, %Y")
            except ValueError:
                label = date_part
            files.append({"filename": p.name, "label": label, "date": date_part})
    return files


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    rules = _load_rules()
    labels = rules.get("labels", [])
    label_count = len(labels)
    rules_count = sum(
        len(entry.get("emailAddresses", [])) + len(entry.get("emailDomains", []))
        for entry in labels
    )
    all_summaries = _summary_files()
    summary_count = len(all_summaries)
    existing_dates = {f["date"] for f in all_summaries}

    today = date.today()
    recent_days = []
    for i in range(7):
        d = today - timedelta(days=i)
        date_str = d.isoformat()
        if i == 0:
            label = "Today"
        elif i == 1:
            label = "Yesterday"
        else:
            label = f"{d.strftime('%a, %b')} {d.day}"
        has_summary = date_str in existing_dates
        recent_days.append({
            "date": date_str,
            "label": label,
            "has_summary": has_summary,
            "filename": f"email_summary_{date_str}.html" if has_summary else None,
        })

    return render_template(
        "dashboard.html",
        label_count=label_count,
        rules_count=rules_count,
        summary_count=summary_count,
        recent_days=recent_days,
        today=today.isoformat(),
    )


@app.route("/summaries")
def summaries():
    files = _summary_files()
    return render_template("summaries.html", files=files)


@app.route("/summaries/<filename>")
def view_summary(filename: str):
    path = SUMMARY_DIR / filename
    if not path.exists() or not path.suffix == ".html":
        return "Not found", 404
    # Serve the HTML file; Accept buttons POST to /accept on the app server
    return send_file(path, mimetype="text/html")


@app.route("/rules")
def rules():
    from collections import defaultdict
    data = _load_rules()
    labels = data.get("labels", [])

    domain_to_info = defaultdict(lambda: {"senders": [], "domain_rules": []})

    for entry in labels:
        short = entry["labelName"].replace("MailMatrixCategories/", "")
        full = entry["labelName"]
        for addr in entry.get("emailAddresses", []):
            domain = addr.split("@")[-1] if "@" in addr else "__no_domain__"
            domain_to_info[domain]["senders"].append({
                "address": addr, "label": short, "full_label": full,
            })
        for domain in entry.get("emailDomains", []):
            domain_to_info[domain]["domain_rules"].append({
                "domain": domain, "label": short, "full_label": full,
            })

    groups = []
    for domain in sorted(domain_to_info.keys()):
        info = domain_to_info[domain]
        senders = sorted(info["senders"], key=lambda x: x["address"])
        domain_rules = info["domain_rules"]
        # Unique (short_label, full_label) pairs for this domain's senders, sorted
        sender_label_pairs = sorted(
            {(s["label"], s["full_label"]) for s in senders},
            key=lambda x: x[0],
        )
        can_convert = (
            len(senders) > 0
            and len(domain_rules) == 0
            and domain != "__no_domain__"
        )
        all_group_labels = sorted({s["label"] for s in senders} | {r["label"] for r in domain_rules})
        groups.append({
            "domain": domain,
            "senders": senders,
            "domain_rules": domain_rules,
            "can_convert": can_convert,
            "sender_label_pairs": sender_label_pairs,
            "labels_in_group": all_group_labels,
        })

    all_label_names = sorted({
        entry["labelName"].replace("MailMatrixCategories/", "")
        for entry in labels
    })
    total_senders = sum(len(g["senders"]) for g in groups)
    total_domain_rules = sum(len(g["domain_rules"]) for g in groups)

    return render_template(
        "rules.html",
        groups=groups,
        label_names=all_label_names,
        total_senders=total_senders,
        total_domain_rules=total_domain_rules,
    )


@app.route("/config")
def config():
    cfg = {
        "IMAP_SERVER": os.getenv("IMAP_SERVER", ""),
        "IMAP_PORT": os.getenv("IMAP_PORT", "993"),
        "IMAP_USERNAME": os.getenv("IMAP_USERNAME", ""),
        "IMAP_PASSWORD": os.getenv("IMAP_PASSWORD", ""),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
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
    result = accept_filing(
        body=body,
        imap_server=os.getenv("IMAP_SERVER", ""),
        imap_port=int(os.getenv("IMAP_PORT", 993)),
        username=os.getenv("IMAP_USERNAME", ""),
        password=os.getenv("IMAP_PASSWORD", ""),
        rules_path=str(RULES_PATH),
    )
    return jsonify(result)


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(force=True)
    allowed = {"IMAP_SERVER", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD", "ANTHROPIC_API_KEY"}
    env_file = str(ENV_PATH)
    if not ENV_PATH.exists():
        ENV_PATH.write_text("")
    updated = []
    for key, val in data.items():
        if key in allowed and val is not None:
            set_key(env_file, key, str(val))
            os.environ[key] = str(val)
            updated.append(key)
    if updated:
        log.info("Config updated: %s", ", ".join(sorted(updated)))
    return jsonify({"ok": True})


@app.route("/api/test-connection")
def api_test_connection():
    server = os.getenv("IMAP_SERVER", "")
    user = os.getenv("IMAP_USERNAME", "")
    pw = os.getenv("IMAP_PASSWORD", "")
    port = int(os.getenv("IMAP_PORT", 993))
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
    data = _load_rules()
    changed = False

    if kind == "sender":
        address = body.get("address", "").strip()
        for entry in data.get("labels", []):
            if entry["labelName"] == full_label:
                if address in entry.get("emailAddresses", []):
                    entry["emailAddresses"].remove(address)
                    changed = True
                    log.info("Rule deleted: <%s> → %s", address, full_label)
                break
    elif kind == "domain":
        domain = body.get("domain", "").strip()
        for entry in data.get("labels", []):
            if entry["labelName"] == full_label:
                if domain in entry.get("emailDomains", []):
                    entry["emailDomains"].remove(domain)
                    changed = True
                    log.info("Domain rule deleted: *@%s → %s", domain, full_label)
                break
    else:
        return jsonify({"ok": False, "error": "Invalid type"}), 400

    if changed:
        _save_rules(data)
    return jsonify({"ok": True})


@app.route("/api/rules/update-sender", methods=["POST"])
def api_rules_update_sender():
    body = request.get_json(force=True)
    address = body.get("address", "").strip()
    old_full_label = body.get("old_full_label", "").strip()
    new_label_input = body.get("new_label", "").strip()
    new_full_label = _full_label(new_label_input)

    if not address or not old_full_label or not new_label_input:
        return jsonify({"ok": False, "error": "Missing required fields"}), 400

    data = _load_rules()

    # Remove from old label
    for entry in data.get("labels", []):
        if entry["labelName"] == old_full_label:
            addrs = entry.get("emailAddresses", [])
            if address in addrs:
                addrs.remove(address)
            break

    # Add to new label (or create it)
    for entry in data.get("labels", []):
        if entry["labelName"] == new_full_label:
            addrs = entry.setdefault("emailAddresses", [])
            if address not in addrs:
                addrs.append(address)
                addrs.sort()
            break
    else:
        data.setdefault("labels", []).append({
            "labelName": new_full_label,
            "emailAddresses": [address],
            "emailDomains": [],
        })

    _save_rules(data)
    log.info("Rule updated: <%s> %s → %s", address, old_full_label, new_full_label)

    # Move matching messages from old label to new label
    move = _move_imap_messages(address, old_full_label, new_full_label)
    return jsonify({"ok": True, "moved": move.get("moved", 0), "imap": move})


@app.route("/api/rules/convert-domain", methods=["POST"])
def api_rules_convert_domain():
    body = request.get_json(force=True)
    domain = body.get("domain", "").strip()
    full_label = body.get("full_label", "").strip()

    if not domain or not full_label:
        return jsonify({"ok": False, "error": "Missing domain or label"}), 400

    data = _load_rules()
    for entry in data.get("labels", []):
        if entry["labelName"] == full_label:
            domains = entry.setdefault("emailDomains", [])
            if domain not in domains:
                domains.append(domain)
                domains.sort()
            # Remove individual senders subsumed by this domain rule
            entry["emailAddresses"] = [
                a for a in entry.get("emailAddresses", [])
                if not a.lower().endswith(f"@{domain}")
            ]
            break
    else:
        data.setdefault("labels", []).append({
            "labelName": full_label,
            "emailAddresses": [],
            "emailDomains": [domain],
        })

    _save_rules(data)
    log.info("Domain rule created: *@%s → %s", domain, full_label)
    return jsonify({"ok": True})


if __name__ == "__main__":
    log.info("Starting MailMatrix AI on http://localhost:5000")
    app.run(debug=True, port=5000)
