import imaplib
import logging
import random
import re
import threading
import time
from datetime import date
from email.header import decode_header as _decode_header
from typing import Callable, List, Optional

_RATE_LIMIT_PHRASES = ('throttl', 'rate limit', 'too many', 'overquota', 'slow down')
_MAX_RETRIES = 5
_BASE_DELAY = 2.0

# Shared lock so app.py and emailSummary.py both protect emailRules.json (H6)
rules_lock = threading.Lock()

# Compiled once for get_all_labels — handles quoted and unquoted mailbox names (L9)
_LIST_RE = re.compile(rb'\([^)]*\) "([^"]*)" "?([^"]*)"?')

log = logging.getLogger(__name__)


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
