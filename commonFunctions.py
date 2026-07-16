import imaplib
import json
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict
from datetime import date
from email.header import decode_header as _decode_header
from pathlib import Path
from typing import Callable, List, Optional, Union

import keyring

KEYCHAIN_SERVICE = "MailMatrixAI"

_RATE_LIMIT_PHRASES = ('throttl', 'rate limit', 'too many', 'overquota', 'slow down')
_MAX_RETRIES = 5
_BASE_DELAY = 2.0

# Shared lock so app.py and emailSummary.py both protect emailRules.json (H6)
rules_lock = threading.Lock()

# Compiled once for get_all_labels — handles quoted and unquoted mailbox names (L9)
_LIST_RE = re.compile(rb'\([^)]*\) "([^"]*)" "?([^"]*)"?')

log = logging.getLogger(__name__)


def get_credential(key: str, default: str = "") -> str:
    """Return a credential from macOS Keychain, falling back to os.environ."""
    val = keyring.get_password(KEYCHAIN_SERVICE, key)
    if val is not None:
        return val
    return os.environ.get(key, default)


def set_credential(key: str, value: str) -> None:
    """Write a credential to macOS Keychain and keep os.environ in sync."""
    keyring.set_password(KEYCHAIN_SERVICE, key, value)
    os.environ[key] = value


def setup_logging(log_file: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-8s  %(message)s',
        datefmt='%H:%M:%S',
        handlers=[logging.StreamHandler(), logging.FileHandler(log_file)],
    )


def validate_email_address(addr: str) -> bool:
    """Return True iff addr looks like an email and contains no IMAP injection chars."""
    return bool(
        addr
        and '@' in addr
        and '"' not in addr
        and '\\' not in addr
        and '\n' not in addr
        and '\r' not in addr
    )


def validate_label(label: str) -> bool:
    """Return True iff label is a safe MailMatrixCategories/ label with no traversal."""
    return bool(
        label
        and label.startswith('MailMatrixCategories/')
        and len(label) > len('MailMatrixCategories/')
        and '..' not in label
        and '"' not in label
        and '\n' not in label
        and '\r' not in label
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _RATE_LIMIT_PHRASES)


def imap_call(fn: Callable[[], tuple]) -> tuple:
    for attempt in range(_MAX_RETRIES):
        try:
            status, data = fn()
            if status == 'NO' and data and any(
                phrase in str(data[0]).lower() for phrase in _RATE_LIMIT_PHRASES
            ):
                raise imaplib.IMAP4.error(f"Rate limited: {data[0]}")
            return status, data
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as e:
            if not _is_rate_limit_error(e) or attempt == _MAX_RETRIES - 1:
                raise
            delay = _BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
            log.warning("Rate limited, retrying in %.1fs (attempt %d/%d)...", delay, attempt + 1, _MAX_RETRIES)
            time.sleep(delay)
    raise RuntimeError("Unreachable")


def connect_to_imap(server: str, username: str, password: str, port: int = 993) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(server, port)
    imap.login(username, password)
    return imap


def extract_email_address(header_value: str) -> str:
    if '<' in header_value and '>' in header_value:
        start = header_value.index('<') + 1
        end = header_value.index('>')
        return header_value[start:end].lower()
    return header_value.strip().lower()


def get_all_labels(imap: imaplib.IMAP4_SSL, parent_label: Optional[str] = None) -> List[str]:
    status, folders = imap_call(lambda: imap.list())
    if status != 'OK':
        return []
    prefix = f"{parent_label}/" if parent_label else None
    labels = []
    for folder in folders:
        if not isinstance(folder, bytes):
            continue
        m = _LIST_RE.search(folder)
        if not m:
            continue
        folder_name = m.group(2).decode(errors='replace').strip('"')
        if prefix is None or folder_name.startswith(prefix):
            labels.append(folder_name)
    return sorted(labels)


def decode_header_value(value: str) -> str:
    try:
        parts = _decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or 'utf-8', errors='replace'))
            else:
                decoded.append(str(part))
        return ''.join(decoded).strip()
    except Exception:
        return value.strip()


def parse_headers(raw: str) -> dict:
    result = {'from': '', 'subject': '', 'date': ''}
    current_key: Optional[str] = None
    current_val: List[str] = []

    for line in raw.splitlines():
        if line and line[0] in (' ', '\t') and current_key:
            current_val.append(line.strip())
        elif ':' in line:
            if current_key and current_val:
                result[current_key] = decode_header_value(' '.join(current_val))
            key = line[:line.index(':')].lower()
            val = line[line.index(':') + 1:].strip()
            if key in result:
                current_key = key
                current_val = [val]
            else:
                current_key = None
                current_val = []

    if current_key and current_val:
        result[current_key] = decode_header_value(' '.join(current_val))

    return result


def imap_date(d: date) -> str:
    return f"{d.day}-{d.strftime('%b')}-{d.strftime('%Y')}"


# ── Rules management (shared by app.py's /rules routes and inboxAnalysis.py) ──

def load_rules_file(path: Union[str, Path]) -> dict:
    """Read emailRules.json, defaulting to an empty rule set if it doesn't exist yet."""
    path = Path(path)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"labels": []}


def save_rules_file(path: Union[str, Path], data: dict) -> None:
    """Write emailRules.json, guarded by the shared rules_lock (H6)."""
    with rules_lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def full_label_name(name: str) -> str:
    """Prefix a bare label name with MailMatrixCategories/ unless already qualified."""
    if name.startswith("MailMatrixCategories/"):
        return name
    return f"MailMatrixCategories/{name}"


def build_rule_groups(data: dict) -> dict:
    """Group emailRules.json entries by sender domain for the Rules UI.

    Returns one card per domain with its individual sender rules and any
    domain-glob rule, plus whether the senders can be collapsed into one.
    """
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
    return {
        "groups": groups,
        "label_names": all_label_names,
        "total_senders": sum(len(g["senders"]) for g in groups),
        "total_domain_rules": sum(len(g["domain_rules"]) for g in groups),
    }


def delete_rule(data: dict, kind: str, full_label: str, address: str = "", domain: str = "") -> bool:
    """Remove a sender or domain rule from a label entry. Returns True if something changed."""
    for entry in data.get("labels", []):
        if entry["labelName"] != full_label:
            continue
        if kind == "sender" and address in entry.get("emailAddresses", []):
            entry["emailAddresses"].remove(address)
            return True
        if kind == "domain" and domain in entry.get("emailDomains", []):
            entry["emailDomains"].remove(domain)
            return True
        break
    return False


def update_sender_rule(data: dict, address: str, old_full_label: str, new_full_label: str) -> None:
    """Move a sender address from one label entry to another, creating the new one if needed."""
    for entry in data.get("labels", []):
        if entry["labelName"] == old_full_label:
            addrs = entry.get("emailAddresses", [])
            if address in addrs:
                addrs.remove(address)
            break

    for entry in data.get("labels", []):
        if entry["labelName"] == new_full_label:
            addrs = entry.setdefault("emailAddresses", [])
            if address not in addrs:
                addrs.append(address)
                addrs.sort()
            return

    data.setdefault("labels", []).append({
        "labelName": new_full_label,
        "emailAddresses": [address],
        "emailDomains": [],
    })


def convert_domain_rule(data: dict, domain: str, full_label: str) -> None:
    """Add a domain-glob rule to full_label and drop individual senders it now subsumes."""
    for entry in data.get("labels", []):
        if entry["labelName"] == full_label:
            domains = entry.setdefault("emailDomains", [])
            if domain not in domains:
                domains.append(domain)
                domains.sort()
            entry["emailAddresses"] = [
                a for a in entry.get("emailAddresses", [])
                if not a.lower().endswith(f"@{domain}")
            ]
            return

    data.setdefault("labels", []).append({
        "labelName": full_label,
        "emailAddresses": [],
        "emailDomains": [domain],
    })


def move_imap_messages(address: str, from_full_label: str, to_full_label: str,
                        imap_server: str, imap_port: int, username: str, password: str) -> dict:
    """Copy every message from `address` in from_full_label into to_full_label and expunge the originals."""
    if not validate_email_address(address):
        return {"ok": False, "error": "Invalid address", "moved": 0}
    if not (imap_server and username and password):
        log.warning("IMAP move skipped: credentials not configured")
        return {"ok": False, "error": "IMAP not configured", "moved": 0}

    log.info("Moving messages for <%s>: %s → %s", address, from_full_label, to_full_label)
    try:
        imap = connect_to_imap(imap_server, username, password, imap_port)
    except Exception as exc:
        log.error("IMAP move failed for <%s>: %s", address, exc)
        return {"ok": False, "error": str(exc), "moved": 0}

    try:
        status, _ = imap_call(lambda: imap.select(f'"{from_full_label}"'))
        if status != "OK":
            log.warning("Cannot select folder %s", from_full_label)
            return {"ok": False, "error": f"Cannot select {from_full_label}", "moved": 0}

        status, search_data = imap_call(lambda: imap.search(None, f'FROM "{address}"'))
        msg_ids = search_data[0].split() if status == "OK" and search_data[0] else []

        for mid in msg_ids:
            imap_call(lambda m=mid: imap.copy(m, f'"{to_full_label}"'))
            imap_call(lambda m=mid: imap.store(m, '+FLAGS', r'\Deleted'))

        if msg_ids:
            imap_call(lambda: imap.expunge())

        log.info("Moved %d message(s) for <%s>", len(msg_ids), address)
        return {"ok": True, "moved": len(msg_ids)}
    except Exception as exc:
        log.error("IMAP move failed for <%s>: %s", address, exc)
        return {"ok": False, "error": str(exc), "moved": 0}
    finally:
        imap.logout()
