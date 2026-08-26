"""Reconcile every MailMatrixCategories/* folder against emailRules.json.

This is the "once in a while" cleanup pass that `sortEmail.py` (which only ever
looks at today's INBOX) cannot do: it walks the whole MailMatrix category tree
and makes each folder match the rules — **removing** copies whose sender no
longer matches that label and **adding** copies to every MailMatrix label the
sender now matches.

Folders ARE labels here (plain IMAP, not Gmail's label model), so a message
"has" a label iff a physical copy sits in that folder. That makes "leave other
labels alone" automatic: every read and write is confined to the
`MailMatrixCategories/` prefix, so INBOX and unrelated folders are never
touched.

Safety invariants (the reason this file is longer than the algorithm):
  * A copy is only ever removed once the message is known to live in at least
    one label its sender *does* match — verified after the additive pass, so a
    failed COPY can never end with the message deleted everywhere.
  * A sender with no matching rule at all is left completely alone. Otherwise
    deleting a rule would silently destroy every message that used to match it.
  * Messages without a Message-ID are read-only: they can't be deduplicated
    across folders, so they're neither copied nor removed.
  * Matching always goes through `find_matching_labels` (address ∪ domain), so
    domain-matched mail is never mistaken for a stray copy.
  * Two different senders sharing one Message-ID are left alone — filing them
    as one message would file one of them by the wrong sender.

Dry-run is the default; `--apply` performs the writes.
"""

import argparse
import imaplib
import json
import logging
import os
import re
import sys
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

from commonFunctions import (
    connect_to_imap,
    ensure_mailbox,
    extract_email_address,
    fetch_many,
    get_all_labels,
    get_credential,
    imap_call,
    parse_headers,
    setup_logging,
    uid_search_all,
    validate_label,
)
from sortEmail import find_matching_labels, load_rules

log = logging.getLogger(__name__)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CATEGORY_PREFIX = "MailMatrixCategories"

# Cap on message copies examined in one run — a full-mailbox scan is the
# expensive part and this endpoint is synchronous. Overridable per install via
# the RESORT_MAX_MESSAGES setting (0 = no limit).
DEFAULT_MAX_MESSAGES = 2000

_HEADER_PARTS = '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])'

# Message-ID is a single unstructured token; folded continuation lines are
# joined before matching so a wrapped header still yields the full id.
_MSGID_RE = re.compile(r'^message-id:\s*(\S+)', re.I | re.M)

# A Message-ID used in an IMAP SEARCH is quoted straight into the command, so
# only printable ASCII minus `"` and `\` may go through: anything else is left
# unverified (and therefore uncopied) rather than escaped.
_SAFE_MSGID_RE = re.compile(r'^[!#-\[\]-~]{1,500}$')


def resort_max_messages(override: Optional[int] = None) -> int:
    """Resolve the per-run message cap: explicit override → RESORT_MAX_MESSAGES
    setting → DEFAULT_MAX_MESSAGES. 0 means "no limit"."""
    if override is not None:
        return max(0, override)
    raw = (get_credential("RESORT_MAX_MESSAGES", "") or "").strip()
    if not raw:
        return DEFAULT_MAX_MESSAGES
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("RESORT_MAX_MESSAGES=%r is not an integer — using %d",
                    raw, DEFAULT_MAX_MESSAGES)
        return DEFAULT_MAX_MESSAGES


def _extract_message_id(raw: str) -> str:
    m = _MSGID_RE.search(raw)
    return m.group(1).strip() if m else ""


# ── Index ─────────────────────────────────────────────────────────────────────

def build_index(
    imap,
    labels: List[str],
    max_messages: int = 0,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> Tuple[Dict[str, dict], int, bool, List[str]]:
    """Scan every category folder and index the copies of each message.

    Returns (messages, scanned, truncated, errors) where messages maps a message
    key (its Message-ID, or a per-copy synthetic key when it has none) to
    ``{"msgid", "from_addr", "subject", "date", "copies": {label: [uid, ...]}}``.

    `max_messages` caps the number of message *copies* fetched; when it bites,
    the newest UIDs in the folder being scanned are kept and `truncated` is
    True. A truncated index can't prove a message is absent from an unscanned
    folder, which is why the additive pass re-checks with a targeted SEARCH
    before every COPY.
    """
    progress_cb = progress_cb or (lambda phase, current, total: None)
    messages: Dict[str, dict] = {}
    errors: List[str] = []
    scanned = 0
    truncated = False

    for idx, label in enumerate(labels):
        progress_cb("scanning", idx, len(labels))
        if max_messages and scanned >= max_messages:
            truncated = True
            break
        try:
            uids = uid_search_all(imap, label)
        except imaplib.IMAP4.error as exc:
            log.warning("Skipping %s: %s", label, exc)
            errors.append(f"{label}: {exc}")
            continue

        if max_messages and scanned + len(uids) > max_messages:
            # Keep the newest messages in this folder — UIDs ascend with arrival.
            uids = uids[-(max_messages - scanned):]
            truncated = True

        fetched = fetch_many(imap, uids, _HEADER_PARTS, use_uid=True)
        for uid in uids:
            rec = fetched.get(uid)
            header_bytes = rec.get("header") if rec else None
            if header_bytes is None:
                errors.append(f"{label}: no headers returned for UID {uid.decode()}")
                continue
            scanned += 1
            raw = header_bytes.decode(errors='replace')
            headers = parse_headers(raw)
            from_addr = extract_email_address(headers.get('from', '')) if headers.get('from') else ''
            msgid = _extract_message_id(raw)
            uid_str = uid.decode()
            # Without a Message-ID the same message can't be recognised across
            # folders, so give the copy its own key: it stays visible in the
            # report but is excluded from both passes (see plan_resort).
            key = msgid or f"\x00nomsgid:{label}:{uid_str}"
            entry = messages.setdefault(key, {
                "msgid": msgid,
                "from_addr": from_addr,
                "subject": headers.get('subject', ''),
                "date": headers.get('date', ''),
                "copies": {},
                "conflict": False,
            })
            if not entry["from_addr"] and from_addr:
                entry["from_addr"] = from_addr
            elif from_addr and from_addr != entry["from_addr"]:
                # Two different messages sharing a Message-ID (forged, or a
                # broken sender). Treating them as one copy of one message
                # would file them by the wrong sender — leave both alone.
                entry["conflict"] = True
            entry["copies"].setdefault(label, []).append(uid_str)

    progress_cb("scanning", len(labels), len(labels))
    log.info("Indexed %d message copies (%d unique) across %d label(s)%s",
             scanned, len(messages), len(labels), " [truncated]" if truncated else "")
    return messages, scanned, truncated, errors


# ── Plan ──────────────────────────────────────────────────────────────────────

def plan_resort(
    messages: Dict[str, dict],
    email_to_labels: Dict[str, List[str]],
    domain_to_labels: Dict[str, List[str]],
) -> dict:
    """Decide what would change, without touching the server.

    Returns ``{"adds": [...], "removes": [...], "unmatched": n, "no_msgid": n}``.
    Each item carries a live reference to its index entry ("entry") so the apply
    pass can re-check presence after COPYs land; `report_from_plan` strips it.
    """
    adds: List[dict] = []
    removes: List[dict] = []
    unmatched = 0
    no_msgid = 0
    conflicts = 0

    for key, entry in messages.items():
        from_addr = entry["from_addr"]
        if not from_addr:
            continue
        if entry.get("conflict"):
            conflicts += 1
            continue
        if not entry["msgid"]:
            # Read-only: can't dedupe it across folders, so neither half of the
            # reconcile is safe (a removal could be the last copy).
            no_msgid += 1
            continue

        matched = [lbl for lbl in find_matching_labels(from_addr, email_to_labels, domain_to_labels)
                   if validate_label(lbl)]
        if not matched:
            # No rule at all for this sender (e.g. the rule was deleted). Filing
            # is undefined, not "wrong" — never delete on that basis.
            unmatched += 1
            continue

        present = set(entry["copies"])
        common = {
            "key": key,
            "msgid": entry["msgid"],
            "from_addr": from_addr,
            "subject": entry["subject"],
            "date": entry["date"],
        }

        # Every indexed message has at least one copy; the source is just the
        # first folder alphabetically so a run is reproducible.
        source = sorted(present)[0] if present else ""
        for label in matched:
            if label in present or not source:
                continue
            adds.append({**common, "target": label, "source": source,
                         "uid": entry["copies"][source][0], "entry": entry})

        for label in sorted(present):
            if label in matched:
                continue
            for uid in entry["copies"][label]:
                removes.append({**common, "folder": label, "uid": uid,
                                "keep": matched, "entry": entry})

    return {"adds": adds, "removes": removes, "unmatched": unmatched,
            "no_msgid": no_msgid, "conflicts": conflicts}


def report_from_plan(plan: dict) -> Dict[str, dict]:
    """Group the plan by label for the UI: {label: {"to_add", "to_remove"}}."""
    report: Dict[str, dict] = defaultdict(lambda: {"to_add": [], "to_remove": []})
    for item in plan["adds"]:
        report[item["target"]]["to_add"].append({
            "from_addr": item["from_addr"], "subject": item["subject"],
            "date": item["date"], "source": item["source"], "uid": item["uid"],
        })
    for item in plan["removes"]:
        report[item["folder"]]["to_remove"].append({
            "from_addr": item["from_addr"], "subject": item["subject"],
            "date": item["date"], "uid": item["uid"],
            "keep": [lbl.split('/', 1)[-1] for lbl in item["keep"]],
        })
    return dict(report)


# ── Apply ─────────────────────────────────────────────────────────────────────

def _message_exists(imap, folder: str, msgid: str) -> Optional[bool]:
    """True/False if `folder` holds a message with this Message-ID, None if the
    check couldn't run (unsafe id, or the folder wouldn't select/search)."""
    if not _SAFE_MSGID_RE.match(msgid):
        return None
    status, _ = imap_call(lambda: imap.select(f'"{folder}"', readonly=True))
    if status != 'OK':
        return None
    status, data = imap_call(
        lambda: imap.uid('SEARCH', None, 'HEADER', 'Message-ID', f'"{msgid}"'))
    if status != 'OK' or not data:
        return None
    return bool(data[0] and data[0].split())


def apply_plan(
    imap,
    plan: dict,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> Tuple[dict, List[str]]:
    """Perform the plan: COPY the additions first, then expunge the removals
    that are still safe. Returns (counts, errors)."""
    progress_cb = progress_cb or (lambda phase, current, total: None)
    counts = {"added": 0, "removed": 0, "skipped": 0}
    errors: List[str] = []

    # ── Additions ────────────────────────────────────────────────────────────
    # Verified target-by-target first (each check selects the target folder, so
    # interleaving them with the COPYs would thrash the selected mailbox), then
    # copied source-by-source.
    pending: List[dict] = []
    by_target: Dict[str, List[dict]] = defaultdict(list)
    for item in plan["adds"]:
        by_target[item["target"]].append(item)

    for target, items in sorted(by_target.items()):
        if not validate_label(target):
            errors.append(f"Refusing to write to non-category folder {target}")
            continue
        ensure_mailbox(imap, target)
        for item in items:
            exists = _message_exists(imap, target, item["msgid"])
            if exists is True:
                # Already there (the index was truncated or the folder changed
                # under us) — copying would duplicate it.
                counts["skipped"] += 1
                item["entry"]["copies"].setdefault(target, [])
                continue
            if exists is None:
                counts["skipped"] += 1
                errors.append(f"Could not verify {target} for <{item['msgid']}> — skipped")
                continue
            pending.append(item)

    by_source: Dict[str, List[dict]] = defaultdict(list)
    for item in pending:
        by_source[item["source"]].append(item)

    done = 0
    for source, items in sorted(by_source.items()):
        status, _ = imap_call(lambda s=source: imap.select(f'"{s}"'))
        if status != 'OK':
            errors.append(f"Cannot select {source} — {len(items)} copy(ies) skipped")
            counts["skipped"] += len(items)
            continue
        for item in items:
            done += 1
            progress_cb("copying", done, len(pending))
            status, _ = imap_call(
                lambda it=item: imap.uid('COPY', it["uid"], f'"{it["target"]}"'))
            if status != 'OK':
                counts["skipped"] += 1
                errors.append(f"COPY {source}:{item['uid']} → {item['target']} failed")
                continue
            counts["added"] += 1
            # Record the new copy so the removal pass can see the message is
            # now filed correctly somewhere.
            item["entry"]["copies"].setdefault(item["target"], [])
            log.info("+ %-45s → %s", item["from_addr"], item["target"])

    # ── Removals ─────────────────────────────────────────────────────────────
    by_folder: Dict[str, List[dict]] = defaultdict(list)
    for item in plan["removes"]:
        if not validate_label(item["folder"]):
            errors.append(f"Refusing to delete from non-category folder {item['folder']}")
            continue
        present = set(item["entry"]["copies"])
        if not present & set(item["keep"]):
            # The copy this one would be replaced by never landed — keep it.
            counts["skipped"] += 1
            errors.append(
                f"Not removing {item['folder']}:{item['uid']} — no copy in a matching label")
            continue
        by_folder[item["folder"]].append(item)

    done = 0
    total_removes = sum(len(v) for v in by_folder.values())
    for folder, items in sorted(by_folder.items()):
        status, _ = imap_call(lambda f=folder: imap.select(f'"{f}"'))
        if status != 'OK':
            errors.append(f"Cannot select {folder} — {len(items)} removal(s) skipped")
            counts["skipped"] += len(items)
            continue
        flagged = []
        for item in items:
            done += 1
            progress_cb("removing", done, total_removes)
            status, _ = imap_call(
                lambda it=item: imap.uid('STORE', it["uid"], '+FLAGS', r'\Deleted'))
            if status != 'OK':
                counts["skipped"] += 1
                errors.append(f"STORE \\Deleted failed for {folder}:{item['uid']}")
                continue
            flagged.append(item)
            log.info("- %-45s ← %s", item["from_addr"], folder)
        if not flagged:
            continue
        uid_set = ",".join(it["uid"] for it in flagged)
        try:
            status, _ = imap_call(lambda s=uid_set: imap.uid('EXPUNGE', s))
            if status != 'OK':
                raise imaplib.IMAP4.error(f'UID EXPUNGE answered {status}')
        except imaplib.IMAP4.error:
            # No UIDPLUS: plain EXPUNGE also drops any other \Deleted message in
            # the folder — same trade-off move_message_uid makes.
            log.warning("UID EXPUNGE unavailable in %s — falling back to plain EXPUNGE", folder)
            imap_call(lambda: imap.expunge())
        counts["removed"] += len(flagged)
        for item in flagged:
            uids = item["entry"]["copies"].get(folder, [])
            if item["uid"] in uids:
                uids.remove(item["uid"])
            if not uids:
                item["entry"]["copies"].pop(folder, None)

    return counts, errors


# ── Entry point ───────────────────────────────────────────────────────────────

def resort(
    imap,
    email_to_labels: Dict[str, List[str]],
    domain_to_labels: Dict[str, List[str]],
    apply: bool = False,
    max_messages: int = 0,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> dict:
    """Reconcile all MailMatrixCategories/* folders against the rules.

    With apply=False (the default) nothing is written — the returned report is
    what *would* happen, for the /rules confirmation step.
    """
    labels = [lbl for lbl in get_all_labels(imap, CATEGORY_PREFIX) if validate_label(lbl)]
    log.info("Resort %s over %d label(s), limit=%s",
             "APPLY" if apply else "dry run", len(labels), max_messages or "none")

    messages, scanned, truncated, errors = build_index(
        imap, labels, max_messages=max_messages, progress_cb=progress_cb)
    plan = plan_resort(messages, email_to_labels, domain_to_labels)

    totals = {
        "labels": len(labels),
        "messages": len(messages),
        "scanned": scanned,
        "to_add": len(plan["adds"]),
        "to_remove": len(plan["removes"]),
        "unmatched": plan["unmatched"],
        "no_msgid": plan["no_msgid"],
        "conflicts": plan["conflicts"],
        "added": 0,
        "removed": 0,
        "skipped": 0,
    }

    # Build the report before applying: entry["copies"] is mutated by the apply
    # pass, but the plan items themselves are what the user confirmed.
    report = report_from_plan(plan)

    if apply:
        counts, apply_errors = apply_plan(imap, plan, progress_cb=progress_cb)
        totals.update(counts)
        errors.extend(apply_errors)

    log.info("Resort done: %s", json.dumps(totals))
    return {
        "ok": True,
        "applied": apply,
        "truncated": truncated,
        "limit": max_messages,
        "labels": report,
        "totals": totals,
        "errors": errors[:100],
    }


def main() -> None:
    setup_logging('resort_email.log')

    parser = argparse.ArgumentParser(
        description="Reconcile MailMatrixCategories/* folders against emailRules.json")
    parser.add_argument("--apply", action="store_true",
                        help="perform the moves (default: dry run, report only)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="max message copies to examine (0 = no limit); "
                             "defaults to the RESORT_MAX_MESSAGES setting")
    args = parser.parse_args()

    imap_server = get_credential("IMAP_SERVER")
    imap_port = int(get_credential("IMAP_PORT", "993"))
    username = get_credential("IMAP_USERNAME")
    password = get_credential("IMAP_PASSWORD")
    rules_path = os.environ.get(
        "RULES_PATH",
        os.path.join(os.environ.get("MAILMATRIX_DATA_DIR", _SCRIPT_DIR), "emailRules.json"),
    )
    if not (imap_server and username and password):
        log.error("IMAP credentials not configured — set them via the web UI Config page")
        sys.exit(1)

    log.info("Loading rules from %s", rules_path)
    try:
        email_to_labels, domain_to_labels = load_rules(rules_path)
    except (json.JSONDecodeError, OSError) as exc:
        # Empty rules would make every filed message look unmatched — and with
        # apply on, that is a mailbox-wide no-op at best. Fail loudly.
        log.error("Could not load rules from %s: %s", rules_path, exc)
        sys.exit(1)

    imap = connect_to_imap(imap_server, username, password, imap_port)
    try:
        result = resort(
            imap, email_to_labels, domain_to_labels,
            apply=args.apply,
            max_messages=resort_max_messages(args.limit),
        )
    finally:
        imap.logout()

    t = result["totals"]
    if args.apply:
        print(f"Resort applied: {t['added']} copy(ies) added, {t['removed']} removed, "
              f"{t['skipped']} skipped, {t['scanned']} message copies scanned.")
    else:
        print(f"Dry run: {t['to_add']} copy(ies) to add, {t['to_remove']} to remove "
              f"across {t['labels']} label(s); {t['scanned']} message copies scanned.")
        for label, changes in sorted(result["labels"].items()):
            print(f"  {label}: +{len(changes['to_add'])} / -{len(changes['to_remove'])}")
        print("Re-run with --apply to perform these changes.")
    if result["truncated"]:
        print(f"NOTE: stopped after the {result['limit']}-message limit — "
              f"run again to continue.")
    for err in result["errors"]:
        print(f"  ! {err}")


if __name__ == "__main__":
    main()
