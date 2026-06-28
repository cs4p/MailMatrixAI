import json
import logging
import os
import sys
import time
from typing import Dict, List, Set

from commonFunctions import (
    connect_to_imap,
    extract_email_address,
    get_all_labels,
    get_credential,
    imap_call,
    parse_headers,
    setup_logging,
)

_PROGRESS_INTERVAL = 120

log = logging.getLogger(__name__)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def crawl_emails_in_label(
    imap,
    label: str,
    progress: Dict,
) -> Set[str]:
    email_addresses: Set[str] = set()

    try:
        status, _ = imap_call(lambda: imap.select(f'"{label}"', readonly=True))
        if status != 'OK':
            return email_addresses

        status, messages = imap_call(lambda: imap.search(None, 'ALL'))
        if status != 'OK':
            return email_addresses

        message_ids = messages[0].split()
        total = len(message_ids)
        log.info("Label '%s': %d messages to scan", label, total)

        # M1: initialise i so the post-loop log doesn't raise UnboundLocalError on empty labels
        i = 0
        for i, msg_id in enumerate(message_ids, 1):
            try:
                # M6: fetch only the From header instead of the full RFC822 body
                status, msg_data = imap_call(
                    lambda mid=msg_id: imap.fetch(mid, '(BODY[HEADER.FIELDS (FROM)])')
                )
                if status != 'OK' or not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0][1].decode(errors='replace') if isinstance(msg_data[0][1], bytes) else ''
                headers = parse_headers(raw)
                from_header = headers.get('from', '')

                if from_header:
                    email_addr = extract_email_address(from_header)
                    if email_addr and '@' in email_addr:
                        email_addresses.add(email_addr)

                progress['messages'] += 1

                now = time.monotonic()
                if now - progress['last_summary'] >= _PROGRESS_INTERVAL:
                    elapsed = now - progress['start']
                    log.info(
                        "Progress: %d messages processed across %d/%d labels | "
                        "%d unique addresses found | elapsed %s",
                        progress['messages'],
                        progress['labels_done'],
                        progress['labels_total'],
                        progress['unique_addresses'],
                        _fmt_elapsed(elapsed),
                    )
                    progress['last_summary'] = now

            except Exception as e:
                log.error("Error processing message %s in '%s': %s", msg_id, label, e)
                continue

        log.info("Label '%s': done (%d/%d messages, %d unique addresses)", label, i, total, len(email_addresses))

    except Exception as e:
        log.error("Error accessing label '%s': %s", label, e)

    return email_addresses


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_email_rules(imap) -> Dict:
    labels = get_all_labels(imap, parent_label="MailMatrixCategories")
    log.info("Found %d labels under MailMatrixCategories", len(labels))
    rules_data: Dict = {"labels": []}

    # L2: track globally unique addresses so the progress counter isn't inflated
    # by addresses that appear in multiple labels
    global_addresses: Set[str] = set()

    progress = {
        'start': time.monotonic(),
        'last_summary': time.monotonic(),
        'messages': 0,
        'labels_done': 0,
        'labels_total': len(labels),
        'unique_addresses': 0,
    }

    for label in labels:
        email_addresses = crawl_emails_in_label(imap, label, progress)
        progress['labels_done'] += 1
        global_addresses |= email_addresses
        progress['unique_addresses'] = len(global_addresses)

        rules_data["labels"].append({
            "labelName": label,
            "emailAddresses": sorted(list(email_addresses)),
            "emailDomains": [],
        })

    elapsed = time.monotonic() - progress['start']
    log.info(
        "Complete: %d messages processed, %d labels, %d unique addresses, elapsed %s",
        progress['messages'],
        progress['labels_done'],
        progress['unique_addresses'],
        _fmt_elapsed(elapsed),
    )

    return rules_data


def write_to_json(data: Dict, output_file: str) -> None:
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Email rules written to %s", output_file)


def main() -> None:
    setup_logging('email_rules.log')

    imap_server = get_credential("IMAP_SERVER")
    imap_port = int(get_credential("IMAP_PORT", "993"))
    username = get_credential("IMAP_USERNAME")
    password = get_credential("IMAP_PASSWORD")
    if not (imap_server and username and password):
        log.error("IMAP credentials not configured — set them via the web UI Config page")
        sys.exit(1)

    # L5: anchor output to the script's directory, not CWD
    rules_path = os.environ.get("RULES_PATH", os.path.join(_SCRIPT_DIR, "emailRules.json"))

    try:
        log.info("Connecting to %s:%d ...", imap_server, imap_port)
        imap = connect_to_imap(imap_server, username, password, imap_port)
        log.info("Authenticated as %s", username)

        rules_data = build_email_rules(imap)
        write_to_json(rules_data, rules_path)
        imap.logout()

    except Exception as e:
        log.exception("Fatal error: %s", e)


if __name__ == "__main__":
    main()
