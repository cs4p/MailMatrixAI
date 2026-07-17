#!/usr/bin/env python3
"""
inboxAnalysis.py — CLI bridge between the MailMatrixAI Swift app and the
Python IMAP/Claude backend.

Every mode writes a single JSON object to stdout and logs to
inbox_analysis.log.  Stderr is silent so the Swift Process wrapper can safely
parse stdout as JSON without stripping log noise.

Mode 1 — full analysis (default):
    python inboxAnalysis.py
    → {"ok":true, "inbox_count":N, "emails":[...], "labels":[...],
       "action_required":[...], "filing_suggestions":[...], "error":null}

Mode 2 — stats only (fast, no Claude call):
    python inboxAnalysis.py --stats-only
    → {"ok":true, "inbox_count":N, "connected":true}

Mode 3 — accept a filing suggestion:
    python inboxAnalysis.py --accept --from-addr user@example.com \
                            --label "MailMatrixCategories/Work"
    → {"ok":true, "moved":3}

Mode 4 — list rules grouped by domain (for the Rules screen):
    python inboxAnalysis.py --rules-list
    → {"ok":true, "groups":[...], "label_names":[...],
       "total_senders":N, "total_domain_rules":N}

Mode 5 — delete a sender or domain rule:
    python inboxAnalysis.py --rules-delete --type sender \
                            --full-label "MailMatrixCategories/Work" \
                            --from-addr user@example.com
    python inboxAnalysis.py --rules-delete --type domain \
                            --full-label "MailMatrixCategories/Work" \
                            --domain example.com
    → {"ok":true}

Mode 6 — move a sender to a different (or new) label, then move matching
messages over IMAP:
    python inboxAnalysis.py --rules-update-sender \
                            --from-addr user@example.com \
                            --old-full-label "MailMatrixCategories/Work" \
                            --new-label Personal
    → {"ok":true, "moved":3, "imap":{...}}

Mode 7 — collapse a domain's sender rules into one domain-glob rule:
    python inboxAnalysis.py --rules-convert-domain \
                            --domain example.com \
                            --full-label "MailMatrixCategories/Work"
    → {"ok":true}

Mode 8 — dashboard stats (label/rule/summary counts + 7-day summary grid):
    python inboxAnalysis.py --dashboard-stats
    → {"ok":true, "inbox_count":N, "connected":true,
       "label_count":N, "rules_count":N, "summary_count":N,
       "recent_days":[{"date":"2026-07-16","label":"Today",
                        "has_summary":false,"filename":null}, ...]}

Mode 9 — list all saved summary reports (for the Summaries screen):
    python inboxAnalysis.py --summaries-list
    → {"ok":true, "files":[{"filename":"email_summary_2026-07-16.html",
                             "label":"July 16, 2026","date":"2026-07-16"}, ...]}

Mode 10 — cleanup scan (domain-collapse + duplicate-address opportunities):
    python inboxAnalysis.py --cleanup-scan
    → {"ok":true, "collapses":[...], "duplicates":[...]}

Mode 11 — collapse a domain's redundant individual sender rules (the domain
rule already covers them):
    python inboxAnalysis.py --cleanup-collapse-domain --domain example.com
    → {"ok":true, "removed":N}

Mode 12 — resolve a duplicate address by keeping one label and removing the
others:
    python inboxAnalysis.py --cleanup-resolve-duplicate \
                            --from-addr user@example.com \
                            --keep-label "MailMatrixCategories/Work"
    → {"ok":true, "removed":N}
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent

# Ensure that commonFunctions, emailSummary, etc. are importable even when this
# script is run from the app bundle (where __file__ is inside Resources/).
# PYTHONPATH is already set to the scripts dir by ScriptRunner.swift, so this
# insert is a belt-and-suspenders for direct CLI use.
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from datetime import date

from commonFunctions import (
    build_rule_groups,
    collapse_domain_rule,
    connect_to_imap,
    convert_domain_rule,
    dashboard_stats,
    delete_rule,
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
    setup_logging,
    summary_files,
    update_sender_rule,
    validate_email_address,
    validate_label,
)
from cleanupRules import find_domain_collapsible, find_duplicate_addresses
from emailSummary import accept_filing, analyze_with_claude, deduplicate_inbox_emails

RULES_PATH = _SCRIPT_DIR / "emailRules.json"

setup_logging("inbox_analysis.log")
log = logging.getLogger(__name__)


# ── Credential helpers ────────────────────────────────────────────────────────

def _credentials():
    server = get_credential("IMAP_SERVER")
    user   = get_credential("IMAP_USERNAME")
    pw     = get_credential("IMAP_PASSWORD")
    port   = int(get_credential("IMAP_PORT", "993"))
    return server, user, pw, port


# ── Mode 2: stats only ────────────────────────────────────────────────────────

def stats_only() -> dict:
    server, user, pw, port = _credentials()
    if not (server and user and pw):
        return {"ok": False, "inbox_count": -1, "connected": False,
                "error": "IMAP credentials not configured"}
    try:
        imap = connect_to_imap(server, user, pw, port)
        status, data = imap.select("INBOX", readonly=True)
        imap.logout()
        if status == "OK" and data and data[0]:
            return {"ok": True, "inbox_count": int(data[0]), "connected": True}
        return {"ok": True, "inbox_count": 0, "connected": True}
    except Exception as exc:
        log.error("stats_only failed: %s", exc)
        return {"ok": False, "inbox_count": -1, "connected": False, "error": str(exc)}


# ── Mode 8: dashboard stats ───────────────────────────────────────────────────

def dashboard_stats_mode() -> dict:
    stats = stats_only()
    rules = load_rules_file(RULES_PATH)
    stats.update(dashboard_stats(rules, RULES_PATH.parent / "emailSummary", date.today()))
    return stats


# ── Mode 9: list saved summary reports ────────────────────────────────────────

def summaries_list_mode() -> dict:
    return {"ok": True, "files": summary_files(RULES_PATH.parent / "emailSummary")}


# ── Mode 10: cleanup scan ──────────────────────────────────────────────────────

def cleanup_scan_mode() -> dict:
    data = load_rules_file(RULES_PATH)
    return {
        "ok": True,
        "collapses": find_domain_collapsible(data),
        "duplicates": find_duplicate_addresses(data),
    }


# ── Mode 11: cleanup collapse-domain ──────────────────────────────────────────

def cleanup_collapse_domain_mode(domain: str) -> dict:
    data = load_rules_file(RULES_PATH)
    removed = collapse_domain_rule(data, domain)
    if removed is None:
        return {"ok": False, "error": f"No domain rule found for @{domain}"}
    if removed:
        save_rules_file(RULES_PATH, data)
    log.info("Domain collapse *@%s: removed %d sender rule(s)", domain, removed)
    return {"ok": True, "removed": removed}


# ── Mode 12: cleanup resolve-duplicate ────────────────────────────────────────

def cleanup_resolve_duplicate_mode(address: str, keep_label: str) -> dict:
    data = load_rules_file(RULES_PATH)
    removed = resolve_duplicate_address(data, address, keep_label)
    if removed:
        save_rules_file(RULES_PATH, data)
    log.info("Duplicate resolved: kept <%s> in %s, removed from %d label(s)", address, keep_label, removed)
    return {"ok": True, "removed": removed}


# ── Mode 1: full analysis ─────────────────────────────────────────────────────

def full_analysis() -> dict:
    server, user, pw, port = _credentials()
    if not (server and user and pw):
        return {"ok": False, "error": "IMAP credentials not configured", "inbox_count": -1}

    try:
        imap = connect_to_imap(server, user, pw, port)
    except Exception as exc:
        return {"ok": False, "error": f"IMAP connection failed: {exc}", "inbox_count": -1}

    raw_emails = []
    labels = []
    inbox_count = 0
    try:
        labels = get_all_labels(imap, "MailMatrixCategories")

        status, sel_data = _imap_call(lambda: imap.select("INBOX", readonly=True))
        if status == "OK" and sel_data and sel_data[0]:
            inbox_count = int(sel_data[0])

        status, data = _imap_call(lambda: imap.search(None, "ALL"))
        msg_ids = data[0].split() if status == "OK" and data[0] else []
        log.info("INBOX fetch: %d messages", len(msg_ids))

        for msg_id in msg_ids:
            try:
                st, raw = _imap_call(
                    lambda mid=msg_id: imap.fetch(mid, "(BODY[HEADER.FIELDS (FROM SUBJECT DATE)])")
                )
                if st != "OK" or not raw or not raw[0]:
                    continue
                headers = parse_headers(raw[0][1].decode(errors="replace"))

                snippet = ""
                try:
                    st2, body_data = _imap_call(
                        lambda mid=msg_id: imap.fetch(mid, "(BODY[TEXT]<0.2000>)")
                    )
                    if st2 == "OK" and body_data and body_data[0] and isinstance(body_data[0], tuple):
                        raw_body = body_data[0][1]
                        if raw_body:
                            text = raw_body.decode(errors="replace")
                            text = re.sub(r"<[^>]+>", " ", text)
                            text = re.sub(r"\s+", " ", text).strip()
                            snippet = text[:400]
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
                log.error("Error fetching message %s: %s", msg_id, exc)
    finally:
        imap.logout()

    emails = deduplicate_inbox_emails(raw_emails)
    log.info("Deduplicated to %d unique senders; calling Claude", len(emails))
    analysis = analyze_with_claude(emails, labels)

    return {
        "ok": True,
        "inbox_count": inbox_count,
        "emails": [{k: v for k, v in em.items() if k != "msg_id"} for em in emails],
        "labels": labels,
        "action_required": analysis.get("action_required", []),
        "filing_suggestions": analysis.get("filing_suggestions", []),
        "error": analysis.get("_error"),
    }


# ── Mode 3: accept filing ─────────────────────────────────────────────────────

def accept_mode(from_addr: str, label: str) -> dict:
    if not validate_email_address(from_addr):
        return {"ok": False, "error": "Invalid email address"}
    if not validate_label(label):
        return {"ok": False, "error": "Invalid label (must start with MailMatrixCategories/)"}

    server, user, pw, port = _credentials()
    return accept_filing(
        body={"from_addr": from_addr, "label": label},
        imap_server=server,
        imap_port=port,
        username=user,
        password=pw,
        rules_path=str(RULES_PATH),
    )


# ── Mode 4: list rules grouped by domain ──────────────────────────────────────

def rules_list_mode() -> dict:
    data = load_rules_file(RULES_PATH)
    return {"ok": True, **build_rule_groups(data)}


# ── Mode 5: delete a sender or domain rule ────────────────────────────────────

def rules_delete_mode(kind: str, full_label: str, from_addr: str, domain: str) -> dict:
    if kind not in ("sender", "domain"):
        return {"ok": False, "error": "--type must be 'sender' or 'domain'"}
    if not validate_label(full_label):
        return {"ok": False, "error": "Invalid label"}

    data = load_rules_file(RULES_PATH)
    changed = delete_rule(data, kind, full_label, address=from_addr, domain=domain)
    if changed:
        save_rules_file(RULES_PATH, data)
        log.info("Rule deleted: %s → %s", from_addr or domain, full_label)
    return {"ok": True}


# ── Mode 6: move a sender to another label ────────────────────────────────────

def rules_update_sender_mode(from_addr: str, old_full_label: str, new_label: str) -> dict:
    new_full_label = full_label_name(new_label)
    if not from_addr or not old_full_label or not new_label:
        return {"ok": False, "error": "Missing required fields"}
    if not validate_label(new_full_label):
        return {"ok": False, "error": "Invalid label name"}

    data = load_rules_file(RULES_PATH)
    update_sender_rule(data, from_addr, old_full_label, new_full_label)
    save_rules_file(RULES_PATH, data)
    log.info("Rule updated: <%s> %s → %s", from_addr, old_full_label, new_full_label)

    server, user, pw, port = _credentials()
    move = move_imap_messages(
        from_addr, old_full_label, new_full_label,
        imap_server=server, imap_port=port, username=user, password=pw,
    )
    return {"ok": True, "moved": move.get("moved", 0), "imap": move}


# ── Mode 7: collapse a domain's senders into one domain rule ─────────────────

def rules_convert_domain_mode(domain: str, full_label: str) -> dict:
    if not domain or not full_label:
        return {"ok": False, "error": "Missing domain or label"}
    if not validate_label(full_label):
        return {"ok": False, "error": "Invalid label"}

    data = load_rules_file(RULES_PATH)
    convert_domain_rule(data, domain, full_label)
    save_rules_file(RULES_PATH, data)
    log.info("Domain rule created: *@%s → %s", domain, full_label)
    return {"ok": True}


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MailMatrixAI inbox analysis — outputs JSON to stdout"
    )
    parser.add_argument("--stats-only", action="store_true",
                        help="Fast inbox count only (no Claude)")
    parser.add_argument("--accept", action="store_true",
                        help="Accept a filing suggestion (moves email + patches rules)")
    parser.add_argument("--rules-list", action="store_true",
                        help="List emailRules.json grouped by sender domain")
    parser.add_argument("--rules-delete", action="store_true",
                        help="Delete a sender or domain rule")
    parser.add_argument("--rules-update-sender", action="store_true",
                        help="Move a sender to a different label (and matching messages over IMAP)")
    parser.add_argument("--rules-convert-domain", action="store_true",
                        help="Collapse a domain's sender rules into one domain-glob rule")
    parser.add_argument("--dashboard-stats", action="store_true",
                        help="Label/rule/summary counts + 7-day summary grid (for the Dashboard screen)")
    parser.add_argument("--summaries-list", action="store_true",
                        help="List all saved summary reports (for the Summaries screen)")
    parser.add_argument("--cleanup-scan", action="store_true",
                        help="Scan for domain-collapse and duplicate-address cleanup opportunities")
    parser.add_argument("--cleanup-collapse-domain", action="store_true",
                        help="Remove individual sender rules already covered by a domain rule")
    parser.add_argument("--cleanup-resolve-duplicate", action="store_true",
                        help="Keep one label for a duplicated address and remove the others")
    parser.add_argument("--from-addr", metavar="EMAIL",
                        help="Sender address (required with --accept, --rules-update-sender, "
                             "--cleanup-resolve-duplicate; used by --rules-delete --type sender)")
    parser.add_argument("--label", metavar="LABEL",
                        help="Target label (required with --accept)")
    parser.add_argument("--type", choices=["sender", "domain"],
                        help="Rule kind (required with --rules-delete)")
    parser.add_argument("--full-label", metavar="LABEL",
                        help="Fully-qualified MailMatrixCategories/ label "
                             "(required with --rules-delete, --rules-convert-domain)")
    parser.add_argument("--domain", metavar="DOMAIN",
                        help="Sender domain (used by --rules-delete --type domain, "
                             "required with --rules-convert-domain, --cleanup-collapse-domain)")
    parser.add_argument("--old-full-label", metavar="LABEL",
                        help="Sender's current fully-qualified label (required with --rules-update-sender)")
    parser.add_argument("--new-label", metavar="LABEL",
                        help="Label to move the sender to, bare or fully-qualified "
                             "(required with --rules-update-sender)")
    parser.add_argument("--keep-label", metavar="LABEL",
                        help="Label to keep for a duplicated address "
                             "(required with --cleanup-resolve-duplicate)")
    args = parser.parse_args()

    if args.stats_only:
        result = stats_only()
    elif args.accept:
        if not args.from_addr or not args.label:
            result = {"ok": False,
                      "error": "--from-addr and --label are required with --accept"}
        else:
            result = accept_mode(args.from_addr, args.label)
    elif args.rules_list:
        result = rules_list_mode()
    elif args.rules_delete:
        if not args.type or not args.full_label:
            result = {"ok": False,
                      "error": "--type and --full-label are required with --rules-delete"}
        else:
            result = rules_delete_mode(args.type, args.full_label,
                                       args.from_addr or "", args.domain or "")
    elif args.rules_update_sender:
        if not args.from_addr or not args.old_full_label or not args.new_label:
            result = {"ok": False,
                      "error": "--from-addr, --old-full-label and --new-label are "
                               "required with --rules-update-sender"}
        else:
            result = rules_update_sender_mode(args.from_addr, args.old_full_label, args.new_label)
    elif args.rules_convert_domain:
        if not args.domain or not args.full_label:
            result = {"ok": False,
                      "error": "--domain and --full-label are required with --rules-convert-domain"}
        else:
            result = rules_convert_domain_mode(args.domain, args.full_label)
    elif args.dashboard_stats:
        result = dashboard_stats_mode()
    elif args.summaries_list:
        result = summaries_list_mode()
    elif args.cleanup_scan:
        result = cleanup_scan_mode()
    elif args.cleanup_collapse_domain:
        if not args.domain:
            result = {"ok": False,
                      "error": "--domain is required with --cleanup-collapse-domain"}
        else:
            result = cleanup_collapse_domain_mode(args.domain)
    elif args.cleanup_resolve_duplicate:
        if not args.from_addr or not args.keep_label:
            result = {"ok": False,
                      "error": "--from-addr and --keep-label are required with --cleanup-resolve-duplicate"}
        else:
            result = cleanup_resolve_duplicate_mode(args.from_addr, args.keep_label)
    else:
        result = full_analysis()

    print(json.dumps(result))


if __name__ == "__main__":
    main()
