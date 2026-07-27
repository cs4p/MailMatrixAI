import base64
import email
import imaplib
import json
import logging
import os
import random
import re
import shutil
import smtplib
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from email import policy as email_policy
from email.header import decode_header as _decode_header
from email.message import EmailMessage, Message
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

import keyring

KEYCHAIN_SERVICE = "MailMatrixAI"

# All credentials live in a single Keychain item (one JSON blob) rather than
# one item per key — storing everything in one item means macOS prompts for
# Keychain access once instead of once per key.
_CREDENTIALS_ACCOUNT = "credentials"

# The 5 keys previously stored as separate Keychain items (account=key) before
# the single-blob consolidation above — used only to pull old values forward
# on first run under the new scheme. Must match _CREDENTIAL_KEYS in app.py.
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


# The decoded Keychain blob is cached so repeated get_credential() calls
# (several per request in app.py) don't each hit the Keychain. The cache
# stores the blob, not resolved values, so the os.environ fallback for keys
# missing from the blob still works. Invalidated by set_credential().
_creds_cache: Optional[dict] = None
_creds_cache_lock = threading.Lock()


def _invalidate_credentials_cache() -> None:
    global _creds_cache
    with _creds_cache_lock:
        _creds_cache = None


def _load_credentials_uncached() -> dict:
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


def _load_credentials() -> dict:
    """Load the single JSON credentials blob from Keychain (cached), migrating
    forward from the old per-key items on first run under the new scheme.
    """
    global _creds_cache
    with _creds_cache_lock:
        if _creds_cache is None:
            _creds_cache = _load_credentials_uncached()
        # Callers get a copy so they can't mutate the cache in place.
        return dict(_creds_cache)


def get_credential(key: str, default: str = "") -> str:
    """Return a credential from the single macOS Keychain blob, falling back to os.environ."""
    creds = _load_credentials()
    if key in creds:
        return creds[key]
    return os.environ.get(key, default)


def set_credential(key: str, value: str) -> None:
    """Write a credential into the single macOS Keychain blob and keep os.environ in sync."""
    global _creds_cache
    creds = _load_credentials()
    creds[key] = value
    keyring.set_password(KEYCHAIN_SERVICE, _CREDENTIALS_ACCOUNT, json.dumps(creds))
    with _creds_cache_lock:
        _creds_cache = dict(creds)
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


def validate_folder(name: str) -> bool:
    """Like validate_label but for any IMAP folder (Sent, Trash, ...) — still
    rejects quoting/CRLF injection and traversal, not just non-category names."""
    return bool(
        name
        and len(name) <= 500
        and '"' not in name
        and '\\' not in name
        and '\n' not in name
        and '\r' not in name
        and '..' not in name
        and not name.startswith('/')
    )


def validate_new_folder_name(name: str) -> bool:
    """Folder creation is restricted to printable ASCII: browsing decodes
    modified-UTF-7 names for display, but there is no encode path, so a
    non-ASCII name would be created with the wrong on-the-wire bytes."""
    return validate_folder(name) and all(32 <= ord(c) < 127 for c in name)


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


# Matches a <style>/<script> block through to its closing tag, or (since
# BODY[TEXT] fetches here are byte-range-truncated) through to the end of the
# string if the closing tag was never reached — otherwise a big inline <style>
# block in a truncated marketing-template email leaves its raw CSS behind as
# if it were message text, since stripping bare "<tag>" markup only removes
# the tags themselves, not the (non-tag) text content between them.
_STYLE_SCRIPT_RE = re.compile(r"<(style|script)\b[^>]*>.*?(</\1\s*>|\Z)", re.I | re.S)


def _strip_html_noise(html: str) -> str:
    """Strip <style>/<script> blocks (including unclosed/truncated ones), then tags."""
    html = _STYLE_SCRIPT_RE.sub(" ", html)
    return re.sub(r"<[^>]+>", " ", html)


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
                        candidate = _strip_html_noise(candidate)
                    if candidate.strip():
                        text = candidate
                        break
        else:
            text = _decode_mime_part(msg)
            if msg.get_content_type() == "text/html":
                text = _strip_html_noise(text)
    except Exception:
        text = ""

    if not text.strip():
        # Fall back to a raw decode if MIME parsing didn't yield anything usable
        text = body_bytes.decode(errors="replace")
        text = _strip_html_noise(text)

    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def imap_date(d: date) -> str:
    return f"{d.day}-{d.strftime('%b')}-{d.strftime('%Y')}"


# First FETCH response tuple of each message starts with its sequence number.
_FETCH_MSGNUM_RE = re.compile(rb"^(\d+)\s+\(")

# In UID FETCH responses the stable identifier is the UID item, not the leading
# sequence number; FLAGS ride along in the same response prefix.
_FETCH_UID_RE = re.compile(rb"UID (\d+)")
_FETCH_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")

# Messages fetched per FETCH command in fetch_many() — module-level so tests
# (and callers that need finer progress granularity) can override it.
FETCH_CHUNK_SIZE = 100


def fetch_many(imap, msg_ids: List[bytes], parts: str,
               chunk_size: Optional[int] = None, use_uid: bool = False) -> Dict[bytes, dict]:
    """Fetch `parts` for many message sequence numbers with batched FETCH
    commands (comma-joined ID sets) instead of one round trip per message.

    Returns {msg_id: {"header": bytes|None, "text": bytes|None}}; messages the
    server didn't return are simply absent. Sections are classified by HEADER/
    TEXT keywords in the response prefix — never by echoing the request string,
    because servers echo a partial request like BODY[TEXT]<0.2000> back as
    BODY[TEXT]<0>. Only valid while no expunge happens between the caller's
    SEARCH and this fetch (sequence numbers must stay stable).

    With use_uid=True, msg_ids are UIDs, the fetch runs as UID FETCH, results
    are keyed by UID (stable across expunges), and each record gains a "flags"
    key holding the raw FLAGS bytes when the server included them.
    """
    chunk_size = chunk_size or FETCH_CHUNK_SIZE
    results: Dict[bytes, dict] = {}
    for start in range(0, len(msg_ids), chunk_size):
        id_set = b",".join(msg_ids[start:start + chunk_size])
        if use_uid:
            status, data = imap_call(lambda s=id_set: imap.uid('FETCH', s.decode(), parts))
        else:
            status, data = imap_call(lambda s=id_set: imap.fetch(s, parts))
        if status != 'OK' or not data:
            continue
        current = None
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                # A FLAGS-only response has no literal, so it arrives as plain
                # bytes rather than a tuple — capture it before ending the
                # current message.
                if use_uid and isinstance(item, bytes):
                    um = _FETCH_UID_RE.search(item)
                    fm = _FETCH_FLAGS_RE.search(item)
                    if um and fm:
                        rec = results.setdefault(
                            um.group(1), {"header": None, "text": None, "flags": None})
                        rec["flags"] = fm.group(1)
                current = None  # bare b')' (or other noise) ends the message
                continue
            prefix, payload = item[0], item[1]
            if not isinstance(prefix, bytes):
                continue
            m = _FETCH_UID_RE.search(prefix) if use_uid else _FETCH_MSGNUM_RE.match(prefix)
            if m:
                current = m.group(1)
                if use_uid:
                    rec = results.setdefault(current, {"header": None, "text": None, "flags": None})
                    fm = _FETCH_FLAGS_RE.search(prefix)
                    if fm:
                        rec["flags"] = fm.group(1)
                else:
                    results.setdefault(current, {"header": None, "text": None})
            if current is None:
                continue
            if b"HEADER" in prefix:
                results[current]["header"] = payload or b""
            elif b"TEXT" in prefix:
                results[current]["text"] = payload or b""
    return results


# ── Rules management (used by app.py's /rules and /cleanup routes) ──────────

def load_rules_file(path: Union[str, Path]) -> dict:
    """Read emailRules.json, defaulting to an empty rule set if it doesn't exist yet.

    A corrupt file is backed up (so a later save through the mutation routes
    can't silently overwrite the only copy) and treated as empty.
    """
    path = Path(path)
    if not path.exists():
        return {"labels": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("rules root is not a JSON object")
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            shutil.copy2(path, backup)
            log.warning("Corrupt rules file %s (%s) — backed up to %s, using empty rules",
                        path, exc, backup.name)
        except OSError:
            log.warning("Corrupt rules file %s (%s) — backup failed, using empty rules", path, exc)
        return {"labels": []}


def save_rules_file(path: Union[str, Path], data: dict) -> None:
    """Write emailRules.json, guarded by the shared rules_lock (H6)."""
    with rules_lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def summary_files(summary_dir: Union[str, Path]) -> List[dict]:
    """List saved email_summary_*.html reports, newest first.

    Each entry also carries processed/need_attention/unfiled/filed counts and a
    human-readable generated_at label when a matching .json sidecar (written by
    emailSummary.py's generate_report()) exists — older reports predating that
    sidecar just won't have these keys.
    """
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
            entry = {"filename": p.name, "label": label, "date": date_part}

            meta_path = p.with_suffix(".json")
            meta = None
            if meta_path.exists():
                try:
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                except (json.JSONDecodeError, OSError):
                    meta = None
            if meta is not None:
                entry["processed"] = meta.get("processed")
                entry["need_attention"] = meta.get("need_attention")
                entry["unfiled"] = meta.get("unfiled")
                entry["filed"] = meta.get("filed")
                generated_at = meta.get("generated_at")
                if generated_at:
                    try:
                        entry["generated_at"] = datetime.fromisoformat(generated_at).strftime("%b %d, %Y at %I:%M %p")
                    except ValueError:
                        entry["generated_at"] = generated_at

            files.append(entry)
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
    by_date = {f["date"]: f for f in all_summaries}

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
        summary = by_date.get(date_str)
        recent_days.append({
            "date": date_str,
            "label": label,
            "has_summary": summary is not None,
            "filename": summary["filename"] if summary else None,
            "processed": summary.get("processed") if summary else None,
            "need_attention": summary.get("need_attention") if summary else None,
            "unfiled": summary.get("unfiled") if summary else None,
            "generated_at": summary.get("generated_at") if summary else None,
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

    # Most senders first; alphabetical by domain as a stable tiebreaker.
    groups.sort(key=lambda g: (-len(g["senders"]), g["domain"]))

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


def add_sender_to_label_rule(data: dict, from_addr: str, label: str) -> None:
    """Add a sender→label rule, creating the label entry if needed (idempotent).

    Callers hold rules_lock around the surrounding read-modify-write.
    """
    for entry in data.get('labels', []):
        if entry['labelName'] == label:
            addrs = entry.setdefault('emailAddresses', [])
            if from_addr not in addrs:
                addrs.append(from_addr)
                addrs.sort()
            return
    data.setdefault('labels', []).append({
        'labelName': label,
        'emailAddresses': [from_addr],
        'emailDomains': [],
    })


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

        moved = copy_failed = 0
        for mid in msg_ids:
            # Never delete the original unless the copy actually succeeded —
            # a NO response (missing folder, quota, ...) would otherwise
            # destroy the message.
            st, _ = imap_call(lambda m=mid: imap.copy(m, f'"{to_full_label}"'))
            if st != "OK":
                copy_failed += 1
                log.error("COPY to %s failed for message %s — leaving it in %s",
                          to_full_label, mid, from_full_label)
                continue
            imap_call(lambda m=mid: imap.store(m, '+FLAGS', r'\Deleted'))
            moved += 1

        if moved:
            imap_call(lambda: imap.expunge())

        log.info("Moved %d message(s) for <%s> (%d copy failure(s))", moved, address, copy_failed)
        if copy_failed and not moved:
            return {"ok": False, "error": f"COPY to {to_full_label} failed",
                    "moved": 0, "copy_failed": copy_failed}
        return {"ok": True, "moved": moved, "copy_failed": copy_failed}
    except Exception as exc:
        log.error("IMAP move failed for <%s>: %s", address, exc)
        return {"ok": False, "error": str(exc), "moved": 0}
    finally:
        imap.logout()


# ── Mail-client helpers (UID-based, used by app.py's /api/mail routes) ────────

# LIST response line: (\Flags...) "delimiter" "Folder Name"  — the folder name
# may be unquoted when it has no specials. Unlike _LIST_RE this captures the
# flags group, which carries SPECIAL-USE attributes and \Noselect.
_LIST_FULL_RE = re.compile(rb'^\(([^)]*)\)\s+"([^"]*)"\s+"?(.*?)"?$')

# RFC 6154 SPECIAL-USE attribute → the client's folder role.
_SPECIAL_USE_MAP = {
    '\\sent': 'sent',
    '\\trash': 'trash',
    '\\junk': 'spam',
    '\\drafts': 'drafts',
    '\\archive': 'archive',
    '\\flagged': 'starred',
    '\\all': 'all',
}

# Fallback when the server omits SPECIAL-USE attributes from plain LIST.
_SPECIAL_NAME_FALLBACK = {
    'sent': 'sent', 'sent items': 'sent', 'sent messages': 'sent',
    'drafts': 'drafts',
    'trash': 'trash', 'deleted items': 'trash',
    'spam': 'spam', 'junk': 'spam', 'junk mail': 'spam',
    'archive': 'archive',
}


def decode_modified_utf7(name: str) -> str:
    """Decode an IMAP modified-UTF-7 folder name (RFC 3501 §5.1.3) for display.

    Display only — there is deliberately no encode counterpart; the raw wire
    name is the canonical folder identifier everywhere else.
    """
    out = []
    i = 0
    while i < len(name):
        ch = name[i]
        if ch != '&':
            out.append(ch)
            i += 1
            continue
        end = name.find('-', i)
        if end == -1:  # unterminated shift — leave the rest as-is
            out.append(name[i:])
            break
        b64 = name[i + 1:end]
        if not b64:
            out.append('&')  # "&-" is a literal ampersand
        else:
            try:
                padded = b64.replace(',', '/')
                padded += '=' * (-len(padded) % 4)
                out.append(base64.b64decode(padded).decode('utf-16-be'))
            except Exception:
                out.append(name[i:end + 1])
        i = end + 1
    return ''.join(out)


def list_folders(imap) -> List[dict]:
    """List every folder with its flags, hierarchy delimiter, special-use role,
    and selectability — the full-tree counterpart of get_all_labels()."""
    status, data = imap_call(lambda: imap.list())
    if status != 'OK':
        return []
    folders = []
    for item in data:
        if not isinstance(item, bytes):
            continue
        m = _LIST_FULL_RE.match(item)
        if not m:
            continue
        flags = m.group(1).decode(errors='replace').split()
        delimiter = m.group(2).decode(errors='replace')
        name = m.group(3).decode(errors='replace')
        if not name:
            continue
        special = None
        for flag in flags:
            special = _SPECIAL_USE_MAP.get(flag.lower())
            if special:
                break
        if special is None:
            special = _SPECIAL_NAME_FALLBACK.get(name.lower())
        folders.append({
            'name': name,
            'display': decode_modified_utf7(name),
            'delimiter': delimiter,
            'flags': flags,
            'special': special,
            'selectable': not any(f.lower() == '\\noselect' for f in flags),
        })
    folders.sort(key=lambda f: f['name'].lower())
    return folders


def uid_search_all(imap, folder: str) -> List[bytes]:
    """Readonly-select `folder` and return every message UID, ascending."""
    status, _ = imap_call(lambda: imap.select(f'"{folder}"', readonly=True))
    if status != 'OK':
        raise imaplib.IMAP4.error(f"Cannot select folder {folder}")
    status, data = imap_call(lambda: imap.uid('SEARCH', None, 'ALL'))
    if status != 'OK' or not data or not data[0]:
        return []
    return data[0].split()


_STATUS_UNSEEN_RE = re.compile(rb"UNSEEN (\d+)")


def folder_unread_counts(imap, folder_names: List[str]) -> Dict[str, int]:
    """Return {folder_name: unseen_count} via one STATUS (UNSEEN) per folder.

    Issued without selecting any mailbox; folders that error (\\Noselect,
    permissions, ...) are skipped. One round trip each, so call sparingly —
    on folder load and after a move, not on every action.
    """
    counts: Dict[str, int] = {}
    for name in folder_names:
        try:
            status, data = imap_call(lambda n=name: imap.status(f'"{n}"', '(UNSEEN)'))
        except imaplib.IMAP4.error:
            continue
        if status != 'OK' or not data or not data[0]:
            continue
        raw = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
        m = _STATUS_UNSEEN_RE.search(raw)
        if m:
            counts[name] = int(m.group(1))
    return counts


_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?(</script\s*>|\Z)", re.I | re.S)


def _safe_filename(name: str) -> str:
    """Reduce an attachment filename to a safe basename for Content-Disposition."""
    name = decode_header_value(name)
    name = os.path.basename(name.replace('\\', '/'))
    name = ''.join(c for c in name if c.isprintable())
    return name.strip() or 'attachment'


_MESSAGE_HEADER_KEYS = {
    'from': 'From', 'to': 'To', 'cc': 'Cc', 'subject': 'Subject', 'date': 'Date',
    'message_id': 'Message-ID', 'in_reply_to': 'In-Reply-To', 'references': 'References',
}


def extract_message_parts(raw: bytes) -> dict:
    """Split a full RFC822 message into headers, viewable bodies, and
    attachment metadata.

    Returns {"headers": {from,to,cc,subject,date,message_id,in_reply_to,
    references}, "text": str|None, "html": str|None, "attachments": [{part,
    filename, content_type, size}]}. The part index is the part's position in
    msg.walk() order — stable for identical raw bytes, which is what
    get_attachment() relies on. HTML has <script> blocks stripped as defense
    in depth; the sandboxed iframe on the client is the primary barrier.
    """
    try:
        msg = email.message_from_bytes(raw, policy=email_policy.default)
    except Exception:
        return {'headers': {k: '' for k in _MESSAGE_HEADER_KEYS},
                'text': raw.decode(errors='replace'), 'html': None, 'attachments': []}

    headers = {}
    for key, header_name in _MESSAGE_HEADER_KEYS.items():
        try:
            headers[key] = str(msg.get(header_name) or '')
        except Exception:  # a single defective header shouldn't sink the view
            headers[key] = ''

    text = html = None
    attachments = []
    for idx, part in enumerate(msg.walk()):
        if part.get_content_maintype() == 'multipart':
            continue
        try:
            filename = part.get_filename()
            disposition = part.get_content_disposition()
        except Exception:
            filename, disposition = None, None
        if filename or disposition == 'attachment':
            try:
                payload = part.get_payload(decode=True) or b''
            except Exception:
                payload = b''
            attachments.append({
                'part': idx,
                'filename': _safe_filename(filename or f'attachment-{idx}'),
                'content_type': part.get_content_type(),
                'size': len(payload),
            })
            continue
        if part.get_content_type() == 'text/plain' and text is None:
            text = _decode_mime_part(part)
        elif part.get_content_type() == 'text/html' and html is None:
            html = _decode_mime_part(part)

    if html is not None:
        html = _SCRIPT_RE.sub('', html)
    return {'headers': headers, 'text': text, 'html': html, 'attachments': attachments}


def get_attachment(raw: bytes, part_index: int) -> Optional[Tuple[str, str, bytes]]:
    """Return (safe_filename, content_type, payload) for the attachment at
    `part_index` in msg.walk() order (matching extract_message_parts), or None."""
    try:
        msg = email.message_from_bytes(raw, policy=email_policy.default)
    except Exception:
        return None
    for idx, part in enumerate(msg.walk()):
        if idx != part_index:
            continue
        if part.get_content_maintype() == 'multipart':
            return None
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        try:
            filename = part.get_filename()
        except Exception:
            filename = None
        return (
            _safe_filename(filename or f'attachment-{idx}'),
            part.get_content_type(),
            payload or b'',
        )
    return None


def move_message_uid(imap, source_folder: str, uid: str, target_folder: str) -> dict:
    """Move one message (by UID) between folders on an already-connected client.

    Fetches the sender server-side (for rule creation — never trusted from the
    client), then UID COPY → only on OK: \\Deleted + UID EXPUNGE (plain EXPUNGE
    fallback for servers without UIDPLUS). A failed copy never destroys the
    original. Returns {"ok", "from_addr", "error"?}.
    """
    status, _ = imap_call(lambda: imap.select(f'"{source_folder}"'))
    if status != 'OK':
        return {'ok': False, 'from_addr': '', 'error': f'Cannot select {source_folder}'}

    status, data = imap_call(
        lambda: imap.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (FROM)])'))
    from_addr = ''
    found = False
    if status == 'OK' and data:
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                found = True
                headers = parse_headers(item[1].decode(errors='replace'))
                if headers.get('from'):
                    from_addr = extract_email_address(headers['from'])
                break
    if not found:
        return {'ok': False, 'from_addr': '', 'error': 'Message not found'}

    st, _ = imap_call(lambda: imap.uid('COPY', uid, f'"{target_folder}"'))
    if st != 'OK':
        # Never delete the original unless the copy actually succeeded.
        log.error("UID COPY %s → %s failed — leaving the message in %s",
                  uid, target_folder, source_folder)
        return {'ok': False, 'from_addr': from_addr,
                'error': f'Copy to {target_folder} failed'}

    imap_call(lambda: imap.uid('STORE', uid, '+FLAGS', r'\Deleted'))
    try:
        st, _ = imap_call(lambda: imap.uid('EXPUNGE', uid))
        if st != 'OK':
            raise imaplib.IMAP4.error(f'UID EXPUNGE answered {st}')
    except imaplib.IMAP4.error:
        # No UIDPLUS: plain EXPUNGE also removes any other \Deleted messages
        # in the folder — rare outside exotic servers, so just warn.
        log.warning("UID EXPUNGE unavailable in %s — falling back to plain EXPUNGE",
                    source_folder)
        imap_call(lambda: imap.expunge())
    return {'ok': True, 'from_addr': from_addr}


def send_smtp(server: str, port: int, username: str, password: str, msg: EmailMessage) -> None:
    """Send msg over SMTP — implicit TLS on port 465, STARTTLS otherwise.
    Raises smtplib exceptions on failure; the caller maps them to API errors."""
    log.info("Sending mail via SMTP %s:%d as %s", server, port, username)
    if port == 465:
        with smtplib.SMTP_SSL(server, port) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(server, port) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)
