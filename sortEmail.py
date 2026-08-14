import imaplib
import json
import logging
import os
import sys
from collections import defaultdict
from typing import Dict, List, Set, Tuple

from commonFunctions import (
    connect_to_imap,
    extract_email_address,
    fetch_many,
    get_credential,
    imap_call,
    setup_logging,
)

log = logging.getLogger(__name__)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_rules(path: str = "emailRules.json") -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    email_to_labels: Dict[str, List[str]] = defaultdict(list)
    domain_to_labels: Dict[str, List[str]] = defaultdict(list)

    for entry in data.get('labels', []):
        label = entry['labelName']
        for addr in entry.get('emailAddresses', []):
            email_to_labels[addr.lower()].append(label)
        for domain in entry.get('emailDomains', []):
            domain_to_labels[domain.lower()].append(label)

    return dict(email_to_labels), dict(domain_to_labels)


def find_matching_labels(
    addr: str,
    email_to_labels: Dict[str, List[str]],
    domain_to_labels: Dict[str, List[str]],
) -> List[str]:
    matches: Set[str] = set()
    if addr in email_to_labels:
        matches.update(email_to_labels[addr])
    domain = addr.split('@')[-1] if '@' in addr else ''
    if domain and domain in domain_to_labels:
        matches.update(domain_to_labels[domain])
    return sorted(matches)


def sort_inbox(
    imap: imaplib.IMAP4_SSL,
    email_to_labels: Dict[str, List[str]],
    domain_to_labels: Dict[str, List[str]],
) -> None:
    status, _ = imap_call(lambda: imap.select('INBOX'))
    if status != 'OK':
        log.error("Failed to select INBOX")
        return

    status, messages = imap_call(lambda: imap.search(None, 'ALL'))
    if status != 'OK':
        log.error("Failed to search INBOX")
        return

    message_ids = messages[0].split()
    log.info("Found %d messages in INBOX", len(message_ids))

    moved = skipped = errors = 0

    # One batched FETCH for all headers instead of a round trip per message.
    fetched = fetch_many(imap, message_ids, '(BODY[HEADER.FIELDS (FROM)])')

    for msg_id in message_ids:
        try:
            header_bytes = fetched.get(msg_id, {}).get('header')
            if header_bytes is None:
                errors += 1
                continue

            raw = header_bytes.decode(errors='replace')
            from_header = ''
            for line in raw.splitlines():
                if line.lower().startswith('from:'):
                    from_header = line[5:].strip()
                    break

            if not from_header:
                skipped += 1
                continue

            addr = extract_email_address(from_header)
            labels = find_matching_labels(addr, email_to_labels, domain_to_labels)

            if not labels:
                skipped += 1
                log.debug("No rule for %s — leaving in INBOX", addr)
                continue

            copy_failed = False
            for label in labels:
                status, _ = imap_call(lambda lbl=label, mid=msg_id: imap.copy(mid, f'"{lbl}"'))
                if status != 'OK':
                    # Never delete the original unless every copy succeeded.
                    # If an earlier label already got a copy, the message is
                    # duplicated — recoverable, unlike a deleted original.
                    copy_failed = True
                    log.error("COPY failed for %s → %s — leaving message %s in INBOX",
                              addr, label, msg_id)
                    break
            if copy_failed:
                errors += 1
                continue

            imap_call(lambda mid=msg_id: imap.store(mid, '+FLAGS', '\\Deleted'))
            moved += 1
            log.info("%-45s → %s", addr, ', '.join(labels))

        except Exception as e:
            log.error("Error on message %s: %s", msg_id, e)
            errors += 1

    imap_call(lambda: imap.expunge())
    log.info("Done: %d moved, %d left in INBOX (no rule), %d errors", moved, skipped, errors)


def main() -> None:
    setup_logging('sort_email.log')

    imap_server = get_credential("IMAP_SERVER")
    imap_port = int(get_credential("IMAP_PORT", "993"))
    username = get_credential("IMAP_USERNAME")
    password = get_credential("IMAP_PASSWORD")
    # Default the rules path to MAILMATRIX_DATA_DIR (like app.py / emailSummary.py)
    # so the CLI reads the same file the web UI writes when the data dir differs
    # from the working dir (e.g. a container with MAILMATRIX_DATA_DIR=/data).
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
        # Empty rules would make sorting a silent no-op — fail loudly instead.
        log.error("Could not load rules from %s: %s", rules_path, exc)
        sys.exit(1)
    log.info(
        "Rules loaded: %d sender addresses, %d domains",
        len(email_to_labels),
        len(domain_to_labels),
    )

    log.info("Connecting to %s:%d ...", imap_server, imap_port)
    imap = connect_to_imap(imap_server, username, password, imap_port)
    log.info("Authenticated as %s", username)

    try:
        sort_inbox(imap, email_to_labels, domain_to_labels)
    finally:
        imap.logout()


if __name__ == "__main__":
    main()
