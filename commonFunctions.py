import email
import imaplib
import json
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from email.header import decode_header as _decode_header
from email.message import Message
from pathlib import Path
from typing import Callable, List, Optional, Union

import keyring

KEYCHAIN_SERVICE = "MailMatrixAI"

# All credentials live in a single Keychain item (one JSON blob) rather than
# one item per key — must match _CREDENTIALS_ACCOUNT's value below on any
# other client of this Keychain item (the Swift app's KeychainService.swift
# uses the same "credentials" account name). Storing everything in one item
# means macOS prompts for Keychain access once instead of once per key.
_CREDENTIALS_ACCOUNT = "credentials"

# The 5 keys previously stored as separate Keychain items (account=key) before
# the single-blob consolidation above — used only to pull old values forward
# on first run under the new scheme. Must match _CREDENTIAL_KEYS in app.py and
# KeychainService.credentialKeys in the Swift app.
_LEGACY_CREDENTIAL_KEYS = (
    "IMAP_SERVER", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD", "ANTHROPIC_API_KEY",
)

_RATE_LIMIT_PHRASES = ('throttl', 'rate limit', 'too many', 'overquota', 'slow down')
_MAX_RETRIES = 5
_BASE_DELAY = 2.0

# Shared lock so app.py and emailSummary.py both protect emailRules.json (H6)
rules_lock = threading.Lock()

# Compiled once for get_all_labels — handles quoted and unquoted mailbox names (L9)
_LIST_RE = re.compile(rb'\([^)]*\) "([^"]*)" "?([^"]*)"?')

log = logging.getLogger(__name__)


def _read_legacy_credential_items() -> dict:
    """Read values still stored under the old one-item-per-key scheme
    (pre-consolidation), without deleting anything.
    """
    legacy = {}
    for key in _LEGACY_CREDENTIAL_KEYS:
        val = keyring.get_password(KEYCHAIN_SERVICE, key)
        if val is not None:
            legacy[key] = val
    return legacy


def _load_credentials() -> dict:
    """Load the single JSON credentials blob from Keychain, migrating forward
    from the old per-key items on first run under the new scheme.
    """
    raw = keyring.get_password(KEYCHAIN_SERVICE, _CREDENTIALS_ACCOUNT)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Keychain credentials blob was not valid JSON — ignoring it")

    legacy = _read_legacy_credential_items()
    if legacy:
        # Write the consolidated blob before removing the old items, so a
        # failed write can't lose data.
        keyring.set_password(KEYCHAIN_SERVICE, _CREDENTIALS_ACCOUNT, json.dumps(legacy))
        for key in legacy:
            try:
                keyring.delete_password(KEYCHAIN_SERVICE, key)
            except keyring.errors.KeyringError:
                pass
        log.info("Migrated %d credential(s) from per-key Keychain items into one blob",
                 len(legacy))
    return legacy


def get_credential(key: str, default: str = "") -> str:
    """Return a credential from the single macOS Keychain blob, falling back to os.environ."""
    creds = _load_credentials()
    if key in creds:
        return creds[key]
    return os.environ.get(key, default)


def set_credential(key: str, value: str) -> None:
    """Write a credential into the single macOS Keychain blob and keep os.environ in sync."""
    creds = _load_credentials()
    creds[key] = value
    keyring.set_password(KEYCHAIN_SERVICE, _CREDENTIALS_ACCOUNT, json.dumps(creds))
    os.environ[key] = value


def credential_keys_in_keychain() -> set:
    """Return the set of credential keys currently stored in the Keychain blob
    (ignores the os.environ fallback — used by the .env migration to avoid
    clobbering a value the user already configured).
    """
    return set(_load_credentials().keys())


def setup_logging(log_file: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-8s  %(name)-14s  %(message)s',
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
            if not _is_rate_limit_error(e):
                log.error("IMAP call failed: %s", e)
                raise
            if attempt == _MAX_RETRIES - 1:
                log.error("IMAP call still rate-limited after %d attempt(s), giving up: %s",
                          _MAX_RETRIES, e)
                raise
            delay = _BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
            log.warning("Rate limited, retrying in %.1fs (attempt %d/%d)...", delay, attempt + 1, _MAX_RETRIES)
            time.sleep(delay)
    raise RuntimeError("Unreachable")


def connect_to_imap(server: str, username: str, password: str, port: int = 993) -> imaplib.IMAP4_SSL:
    log.info("Connecting to IMAP %s:%d as %s", server, port, username)
    try:
        imap = imaplib.IMAP4_SSL(server, port)
        imap.login(username, password)
    except (imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError) as exc:
        if _is_rate_limit_error(exc):
            log.error("IMAP login rate-limited for %s: %s", username, exc)
        else:
            log.error("IMAP login failed for %s: %s", username, exc)
        raise
    log.info("IMAP login succeeded for %s", username)
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


def _decode_mime_part(part: Message) -> str:
    """Decode a MIME part's payload (quoted-printable/base64-aware) to text."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if not payload:
        raw = part.get_payload(decode=False)
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def extract_body_snippet(header_bytes: bytes, body_bytes: bytes, max_len: int = 400) -> str:
    """Build a clean preview snippet from a message's headers (used here only
    for Content-Type) and its BODY[TEXT] bytes (often byte-range-truncated).

    Properly decodes quoted-printable/base64 and prefers the message's
    text/plain part over showing raw MIME boundary lines and part headers
    as if they were message text — which is what a naive decode-and-strip-
    HTML-tags approach produces for any multipart message.
    """
    text = ""
    try:
        msg = email.message_from_bytes(header_bytes + body_bytes)
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get_content_type() in ("text/plain", "text/html"):
                    candidate = _decode_mime_part(part)
                    if part.get_content_type() == "text/html":
                        candidate = re.sub(r"<[^>]+>", " ", candidate)
                    if candidate.strip():
                        text = candidate
                        break
        else:
            text = _decode_mime_part(msg)
            if msg.get_content_type() == "text/html":
                text = re.sub(r"<[^>]+>", " ", text)
    except Exception:
        text = ""

    if not text.strip():
        # Fall back to a raw decode if MIME parsing didn't yield anything usable
        text = body_bytes.decode(errors="replace")
        text = re.sub(r"<[^>]+>", " ", text)

    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


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


def summary_files(summary_dir: Union[str, Path]) -> List[dict]:
    """List saved email_summary_*.html reports, newest first."""
    summary_dir = Path(summary_dir)
    files = []
    if summary_dir.exists():
        for p in sorted(summary_dir.glob("email_summary_*.html"), reverse=True):
            date_part = p.stem.replace("email_summary_", "")
            try:
                d = date.fromisoformat(date_part)
                label = d.strftime("%B %d, %Y")
            except ValueError:
                label = date_part
            files.append({"filename": p.name, "label": label, "date": date_part})
    return files


def dashboard_stats(rules_data: dict, summary_dir: Union[str, Path], today: date) -> dict:
    """Aggregate the label/rule/summary counts and 7-day summary grid for the Dashboard."""
    labels = rules_data.get("labels", [])
    label_count = len(labels)
    rules_count = sum(
        len(entry.get("emailAddresses", [])) + len(entry.get("emailDomains", []))
        for entry in labels
    )
    all_summaries = summary_files(summary_dir)
    summary_count = len(all_summaries)
    existing_dates = {f["date"] for f in all_summaries}

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

    custom_default = (today - timedelta(days=7)).isoformat()

    return {
        "label_count": label_count,
        "rules_count": rules_count,
        "summary_count": summary_count,
        "recent_days": recent_days,
        "custom_default": custom_default,
    }


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


def collapse_domain_rule(data: dict, domain: str) -> Optional[int]:
    """Remove every individual sender address under `domain`, since an existing
    domain-glob rule already covers them (even across labels). Returns the
    removed count, or None if no domain rule exists for `domain`.
    """
    has_domain_rule = any(
        domain in entry.get("emailDomains", [])
        for entry in data.get("labels", [])
    )
    if not has_domain_rule:
        return None

    removed = 0
    for entry in data.get("labels", []):
        addrs = entry.get("emailAddresses", [])
        before = len(addrs)
        entry["emailAddresses"] = [a for a in addrs if not a.lower().endswith(f"@{domain}")]
        removed += before - len(entry["emailAddresses"])
    return removed


def resolve_duplicate_address(data: dict, address: str, keep_label: str) -> int:
    """Remove `address` from every label except `keep_label`. Returns the removed count."""
    removed = 0
    for entry in data.get("labels", []):
        if entry["labelName"] == keep_label:
            continue
        addrs = entry.get("emailAddresses", [])
        if address in addrs:
            addrs.remove(address)
            removed += 1
    return removed


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


def convert_domain_rule(data: dict, domain: str, full_label: str, purge_other_labels: bool = False) -> None:
    """Add a domain-glob rule to full_label and drop individual senders it now subsumes.

    If purge_other_labels, also remove individual sender rules for this domain from
    every other label — otherwise those senders would keep matching their old label
    AND the new domain rule, filing mail to both.
    """
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
            break
    else:
        data.setdefault("labels", []).append({
            "labelName": full_label,
            "emailAddresses": [],
            "emailDomains": [domain],
        })

    if purge_other_labels:
        for entry in data.get("labels", []):
            if entry["labelName"] == full_label:
                continue
            entry["emailAddresses"] = [
                a for a in entry.get("emailAddresses", [])
                if not a.lower().endswith(f"@{domain}")
            ]


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
