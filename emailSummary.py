import hashlib
import imaplib
import json
import logging
import re
import sys
import threading
import time
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

import anthropic
from commonFunctions import (
    add_sender_to_label_rule,
    connect_to_imap,
    ensure_mailbox,
    extract_body_snippet,
    extract_email_address,
    fetch_many,
    get_all_labels,
    get_credential,
    imap_call,
    imap_date,
    log_token_usage,
    parse_headers,
    rules_lock,
    setup_logging,
    validate_email_address,
    validate_label,
)

log = logging.getLogger(__name__)

# Models selectable for inbox analysis (ANALYSIS_MODEL setting on /config).
# Classification + filing suggestions is a simple structured task, so the
# default is the cheapest model; per-model request params keep each one on a
# config it accepts (Haiku 4.5 has no adaptive thinking; Sonnet 5 runs adaptive
# unless told otherwise, so it gets low effort; Opus 4.8 keeps the original
# adaptive-thinking setup for anyone who wants the old quality/cost).
DEFAULT_ANALYSIS_MODEL = "claude-haiku-4-5"
ANALYSIS_MODELS = {
    "claude-haiku-4-5": {
        "label": "Claude Haiku 4.5 — lowest cost",
        "params": {},
    },
    "claude-sonnet-5": {
        "label": "Claude Sonnet 5 — balanced",
        "params": {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}},
    },
    "claude-opus-4-8": {
        "label": "Claude Opus 4.8 — highest quality, highest cost",
        "params": {"thinking": {"type": "adaptive"}},
    },
}


def analysis_model() -> str:
    """The configured analysis model, or the default when unset/unknown."""
    model = (get_credential("ANALYSIS_MODEL") or "").strip()
    return model if model in ANALYSIS_MODELS else DEFAULT_ANALYSIS_MODEL

# ── IMAP helpers ──────────────────────────────────────────────────────────────

def fetch_folder_emails(imap: imaplib.IMAP4_SSL, folder: str, date_filter: str) -> List[dict]:
    status, _ = imap_call(lambda: imap.select(f'"{folder}"', readonly=True))
    if status != 'OK':
        log.warning("Could not select folder: %s", folder)
        return []

    status, messages = imap_call(lambda: imap.search(None, f'ON {date_filter}'))
    if status != 'OK':
        return []

    message_ids = messages[0].split()
    if not message_ids:
        return []

    log.info("'%s': %d messages on %s", folder, len(message_ids), date_filter)

    # One batched FETCH for all headers instead of a round trip per message.
    fetched = fetch_many(imap, message_ids, '(BODY[HEADER.FIELDS (FROM SUBJECT DATE)])')

    emails = []
    for msg_id in message_ids:
        header_bytes = fetched.get(msg_id, {}).get('header')
        if header_bytes is None:
            continue
        headers = parse_headers(header_bytes.decode(errors='replace'))
        emails.append({
            'msg_id': msg_id,
            'from_display': headers['from'],
            'from_addr': extract_email_address(headers['from']),
            'subject': headers['subject'] or '(no subject)',
            'date': headers['date'],
        })

    return emails


def fetch_inbox_with_body(imap: imaplib.IMAP4_SSL, date_filter: str) -> List[dict]:
    status, _ = imap_call(lambda: imap.select('"INBOX"', readonly=True))
    if status != 'OK':
        log.warning("Could not select INBOX")
        return []

    status, messages = imap_call(lambda: imap.search(None, f'ON {date_filter}'))
    if status != 'OK':
        return []

    message_ids = messages[0].split()
    if not message_ids:
        return []

    log.info("'INBOX': %d messages on %s", len(message_ids), date_filter)

    # A single combined FETCH per chunk gets headers and the body snippet
    # together instead of two round trips per message.
    fetched = fetch_many(
        imap, message_ids,
        '(BODY[HEADER.FIELDS (FROM SUBJECT DATE CONTENT-TYPE)] BODY[TEXT]<0.2000>)',
    )

    emails = []
    for msg_id in message_ids:
        entry = fetched.get(msg_id, {})
        header_bytes = entry.get('header')
        if header_bytes is None:
            continue
        headers = parse_headers(header_bytes.decode(errors='replace'))
        body_bytes = entry.get('text') or b''
        emails.append({
            'msg_id': msg_id,
            'from_display': headers['from'],
            'from_addr': extract_email_address(headers['from']),
            'subject': headers['subject'] or '(no subject)',
            'date': headers['date'],
            'body_snippet': extract_body_snippet(header_bytes, body_bytes) if body_bytes else '',
        })

    return emails


def deduplicate_inbox_emails(emails: List[dict]) -> List[dict]:
    """Collapse multiple emails from the same sender into one entry, adding a count."""
    seen: Dict[str, dict] = {}
    for em in emails:
        addr = em['from_addr']
        if addr not in seen:
            seen[addr] = {**em, 'count': 1}
        else:
            seen[addr]['count'] += 1
    return list(seen.values())


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyze_with_claude(inbox_emails: List[dict], available_labels: List[str],
                        caller: str = "summary") -> dict:
    if not inbox_emails:
        return {'action_required': [], 'filing_suggestions': []}

    client = anthropic.Anthropic(api_key=get_credential("ANTHROPIC_API_KEY"))

    emails_text = ''
    for i, em in enumerate(inbox_emails, 1):
        emails_text += f"\n{i}. From: {em['from_display']}"
        if em['from_addr'] and em['from_addr'] != em['from_display'].lower():
            emails_text += f" <{em['from_addr']}>"
        emails_text += f"\n   Subject: {em['subject']}"
        emails_text += f"\n   Date: {em['date']}"
        count = em.get('count', 1)
        if count > 1:
            emails_text += f"\n   (same sender sent {count} messages today)"
        if em.get('body_snippet'):
            emails_text += f"\n   Preview: {em['body_snippet'][:300]}"
        emails_text += "\n"

    labels_text = '\n'.join(f'- {label}' for label in available_labels)

    prompt = f"""Analyze these emails that were not matched by any automatic filing rule and are sitting in the INBOX.

## Unmatched Inbox Emails:
{emails_text}

## Available Filing Labels:
{labels_text if labels_text else '(none configured yet)'}

Respond ONLY with a JSON object — no other text before or after:
{{
  "action_required": [
    {{
      "index": 1,
      "from": "email@example.com",
      "subject": "Email subject",
      "reason": "Brief explanation of what action is needed"
    }}
  ],
  "filing_suggestions": [
    {{
      "index": 1,
      "from": "email@example.com",
      "subject": "Email subject",
      "suggested_label": "MailMatrixCategories/LabelName",
      "is_new_label": false,
      "reason": "Why this label fits"
    }}
  ]
}}

Rules:
- Only include in action_required emails that need a human response or decision (not newsletters, receipts, automated notifications)
- Include ALL emails in filing_suggestions
- Prefer existing labels; set is_new_label to false and use the exact label name from the list
- If no existing label fits well, suggest a descriptive new name under MailMatrixCategories/ and set is_new_label to true"""

    model = analysis_model()
    log.info("Calling %s to analyze %d inbox emails...", model, len(inbox_emails))

    try:
        with client.messages.stream(
            model=model,
            max_tokens=64000,
            messages=[{"role": "user", "content": prompt}],
            **ANALYSIS_MODELS[model]["params"],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError:
        log.error("Anthropic API key is invalid — check ANTHROPIC_API_KEY (Keychain or environment)")
        return {'action_required': [], 'filing_suggestions': [],
                '_error': 'Invalid API key — check ANTHROPIC_API_KEY (Keychain or environment)'}
    except anthropic.RateLimitError as exc:
        retry_after = exc.response.headers.get("retry-after") if exc.response is not None else None
        if retry_after:
            log.error("Anthropic API rate limited (429); retry after %ss: %s", retry_after, exc.message)
        else:
            log.error("Anthropic API rate limited (429): %s", exc.message)
        return {'action_required': [], 'filing_suggestions': [],
                '_error': 'Rate limited by Anthropic API — try again in a moment'}
    except anthropic.APIStatusError as exc:
        if exc.status_code == 402:
            log.error("Anthropic account has no remaining credits — skipping AI analysis")
            return {'action_required': [], 'filing_suggestions': [],
                    '_error': 'No credits remaining — add funds at console.anthropic.com'}
        log.error("Anthropic API error %d: %s", exc.status_code, exc.message)
        return {'action_required': [], 'filing_suggestions': [],
                '_error': f'Anthropic API error {exc.status_code}: {exc.message}'}
    except anthropic.APIConnectionError as exc:
        log.error("Could not connect to Anthropic API: %s", exc)
        return {'action_required': [], 'filing_suggestions': [],
                '_error': 'Could not connect to Anthropic API — check your internet connection'}

    log.info("Claude response received (stop_reason=%s, input_tokens=%d, output_tokens=%d)",
              response.stop_reason, response.usage.input_tokens, response.usage.output_tokens)
    log_token_usage(response, caller=caller, model=model,
                    email_count=len(inbox_emails))

    text = next((b.text for b in response.content if b.type == "text"), "")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError as exc:
            log.warning("Could not parse Claude JSON response: %s", exc)

    log.warning("Claude did not return valid JSON (stop_reason=%s). Raw response:\n%s",
                response.stop_reason, text[:500])
    return {'action_required': [], 'filing_suggestions': [],
            '_error': f'AI response was empty or malformed (stop_reason={response.stop_reason})'}


# ── Accept handler (IMAP move + rules update) ─────────────────────────────────

def accept_filing(
    body: dict,
    imap_server: str,
    imap_port: int,
    username: str,
    password: str,
    rules_path: str,
) -> dict:
    from_addr = body.get('from_addr', '').strip()
    label = body.get('label', '').strip()

    if not from_addr or not label:
        return {'ok': False, 'error': 'Missing from_addr or label'}
    if not validate_email_address(from_addr):  # H2: reject IMAP injection chars
        return {'ok': False, 'error': 'Invalid from_addr'}
    if not validate_label(label):              # H3: only allow MailMatrixCategories/*
        return {'ok': False, 'error': 'Invalid label'}

    # Move all INBOX messages from this sender to the label
    try:
        imap = connect_to_imap(imap_server, username, password, imap_port)
        try:
            imap_call(lambda: imap.select('INBOX'))
            # Accepting a suggestion for a brand-new category: COPY won't create
            # the mailbox, so create it first (no-op if it already exists).
            ensure_mailbox(imap, label)
            status, messages = imap_call(lambda: imap.search(None, f'FROM "{from_addr}"'))
            msg_ids = messages[0].split() if status == 'OK' else []

            moved = copy_failed = 0
            for mid in msg_ids:
                # Never delete the original unless the copy actually succeeded.
                st, _ = imap_call(lambda m=mid: imap.copy(m, f'"{label}"'))
                if st != 'OK':
                    copy_failed += 1
                    log.error("COPY to %s failed for message %s — leaving it in INBOX", label, mid)
                    continue
                imap_call(lambda m=mid: imap.store(m, '+FLAGS', '\\Deleted'))
                moved += 1

            if moved:
                imap_call(lambda: imap.expunge())

            log.info("Moved %d message(s) from %s to %s (%d copy failure(s))",
                     moved, from_addr, label, copy_failed)
        finally:
            imap.logout()
        if copy_failed and not moved:
            return {'ok': False, 'error': f'Could not copy messages to {label}',
                    'moved': 0, 'copy_failed': copy_failed}
    except Exception as exc:
        log.error("IMAP error during accept: %s", exc)
        return {'ok': False, 'error': str(exc)}

    # Update emailRules.json
    try:
        with rules_lock:
            with open(rules_path, 'r', encoding='utf-8') as f:
                rules = json.load(f)

            add_sender_to_label_rule(rules, from_addr, label)

            with open(rules_path, 'w', encoding='utf-8') as f:
                json.dump(rules, f, indent=2, ensure_ascii=False)

        log.info("emailRules.json updated: %s -> %s", from_addr, label)
    except Exception as exc:
        log.error("Failed to update %s: %s", rules_path, exc)
        return {'ok': False, 'error': f'Emails moved but rules update failed: {exc}'}

    return {'ok': True, 'moved': moved, 'copy_failed': copy_failed}


# ── Daily summary: live data, no saved files ─────────────────────────────────
#
# A summary is built on demand from the mailbox as it is now: collect_day()
# reads the day's filed + unmatched INBOX mail (IMAP only), analyze_senders()
# adds Claude's action/filing analysis. Nothing is written to disk; the only
# state is ANALYSIS_CACHE, so re-opening a summary doesn't pay for the same
# analysis twice.

class SummaryError(Exception):
    """Raised when a summary can't be built (e.g. credentials not configured)."""


def open_imap() -> imaplib.IMAP4_SSL:
    """Connect + log in with the configured credentials."""
    server = get_credential("IMAP_SERVER")
    port = int(get_credential("IMAP_PORT", "993"))
    username = get_credential("IMAP_USERNAME")
    password = get_credential("IMAP_PASSWORD")
    if not (server and username and password):
        raise SummaryError("IMAP credentials not configured — set them via the web UI Config page")
    return connect_to_imap(server, username, password, port)


def _without_msg_id(email_entry: dict) -> dict:
    # msg_id is a per-connection sequence number (bytes) — meaningless to a
    # caller and not JSON-serializable.
    return {k: v for k, v in email_entry.items() if k != 'msg_id'}


def collect_day(imap: imaplib.IMAP4_SSL, target_date: date) -> dict:
    """The day's mail straight from the mailbox — no AI.

    Returns {date, labels, filed: {label: [email]}, inbox: [one entry per
    sender, with count], counts: {filed, unfiled, senders}}.
    """
    date_filter = imap_date(target_date)
    labels = get_all_labels(imap, 'MailMatrixCategories')

    filed: Dict[str, List[dict]] = {}
    for label in labels:
        emails = fetch_folder_emails(imap, label, date_filter)
        if emails:
            filed[label] = [_without_msg_id(em) for em in emails]

    raw_inbox = fetch_inbox_with_body(imap, date_filter)
    inbox = [_without_msg_id(em) for em in deduplicate_inbox_emails(raw_inbox)]

    return {
        'date': target_date.isoformat(),
        'labels': labels,
        'filed': filed,
        'inbox': inbox,
        'counts': {
            'filed': sum(len(v) for v in filed.values()),
            'unfiled': len(raw_inbox),
            'senders': len(inbox),
        },
    }


def _count_on(imap: imaplib.IMAP4_SSL, folder: str, date_filter: str) -> int:
    status, _ = imap_call(lambda: imap.select(f'"{folder}"', readonly=True))
    if status != 'OK':
        return 0
    status, data = imap_call(lambda: imap.search(None, f'ON {date_filter}'))
    if status != 'OK' or not data or not data[0]:
        return 0
    return len(data[0].split())


def count_day(imap: imaplib.IMAP4_SSL, labels: List[str], target_date: date) -> dict:
    """Cheap per-day counts (SEARCH only — no FETCH, no AI)."""
    date_filter = imap_date(target_date)
    return {
        'filed': sum(_count_on(imap, label, date_filter) for label in labels),
        'unfiled': _count_on(imap, 'INBOX', date_filter),
    }


# ── Analysis cache ────────────────────────────────────────────────────────────

class AnalysisCache:
    """Per-sender analysis results, in memory only (lost on restart — by design,
    so the dynamic summary writes nothing to disk).

    The key covers everything the result depends on — model, label list, and
    the sender's message (address, subject, preview, count) — so a new message
    from a sender, a new label, or a model switch is re-analyzed automatically.
    """

    def __init__(self, ttl: float = 7 * 24 * 3600, max_entries: int = 5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(model: str, labels: List[str], email_entry: dict) -> str:
        material = json.dumps([
            model,
            sorted(labels),
            email_entry.get('from_addr', ''),
            email_entry.get('subject', ''),
            (email_entry.get('body_snippet') or '')[:300],
            email_entry.get('count', 1),
        ], ensure_ascii=False)
        return hashlib.sha256(material.encode('utf-8')).hexdigest()

    def get(self, key: str) -> Optional[tuple]:
        """(stored_at, result) if present and fresh, else None."""
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            if time.time() - hit[0] > self.ttl:
                del self._data[key]
                return None
            return hit

    def put(self, key: str, result: dict) -> None:
        with self._lock:
            self._data[key] = (time.time(), result)
            if len(self._data) > self.max_entries:
                oldest = sorted(self._data, key=lambda k: self._data[k][0])
                for k in oldest[:len(self._data) - self.max_entries]:
                    del self._data[k]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


ANALYSIS_CACHE = AnalysisCache()


def split_by_sender(analysis: dict, batch_len: int) -> List[dict]:
    """Turn one analyze_with_claude response (1-based indices local to the
    batch) into one {'action', 'suggestion'} entry per sender. Items with a
    missing or out-of-range index are dropped — Claude occasionally returns
    imperfect JSON, and one bad item shouldn't sink the batch.
    """
    per = [{'action': None, 'suggestion': None} for _ in range(batch_len)]
    for field, slot in (('action_required', 'action'), ('filing_suggestions', 'suggestion')):
        for item in analysis.get(field) or []:
            idx = item.get('index') if isinstance(item, dict) else None
            if not isinstance(idx, int) or isinstance(idx, bool) or not (1 <= idx <= batch_len):
                log.warning("Dropping analysis item with bad index: %r", item)
                continue
            per[idx - 1][slot] = {k: v for k, v in item.items() if k != 'index'}
    return per


def analyze_senders(
    emails: List[dict],
    labels: List[str],
    *,
    cache: Optional[AnalysisCache] = None,
    force: bool = False,
    caller: str = "summary",
    batch_size: int = 50,
    analyze_fn: Optional[Callable] = None,
    progress_cb: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
) -> dict:
    """Action/filing analysis for one-entry-per-sender `emails`, calling Claude
    only for senders not already in the cache (all of them when force=True), in
    batches of batch_size. Failed batches are reported, never cached.

    Returns {action_required, filing_suggestions (both with 1-based `index`
    into emails), error, model, analyzed (senders sent to Claude this call),
    cached (served from cache), analyzed_at (epoch of the oldest result shown,
    or None)} — or {'cancelled': True}.
    """
    cache = ANALYSIS_CACHE if cache is None else cache
    analyze_fn = analyze_fn or analyze_with_claude
    progress_cb = progress_cb or (lambda phase, current, total: None)
    cancel_event = cancel_event or threading.Event()
    model = analysis_model()

    keys = [cache.key(model, labels, em) for em in emails]
    results: List[Optional[dict]] = [None] * len(emails)
    stamps: List[Optional[float]] = [None] * len(emails)
    todo = []
    for i, key in enumerate(keys):
        hit = None if force else cache.get(key)
        if hit is None:
            todo.append(i)
        else:
            stamps[i], results[i] = hit

    error = None
    progress_cb("analyzing", 0, len(todo))
    for start in range(0, len(todo), batch_size):
        if cancel_event.is_set():
            return {'cancelled': True}
        idxs = todo[start:start + batch_size]
        analysis = analyze_fn([emails[i] for i in idxs], labels, caller=caller)
        if analysis.get('_error'):
            error = error or analysis['_error']
        else:
            now = time.time()
            for i, per_sender in zip(idxs, split_by_sender(analysis, len(idxs))):
                cache.put(keys[i], per_sender)
                results[i], stamps[i] = per_sender, now
        progress_cb("analyzing", start + len(idxs), len(todo))

    action_required, filing_suggestions = [], []
    for i, per_sender in enumerate(results, 1):
        if not per_sender:
            continue
        if per_sender['action']:
            action_required.append({**per_sender['action'], 'index': i})
        if per_sender['suggestion']:
            filing_suggestions.append({**per_sender['suggestion'], 'index': i})

    shown = [t for t in stamps if t is not None]
    return {
        'action_required': action_required,
        'filing_suggestions': filing_suggestions,
        'error': error,
        'model': model,
        'analyzed': len(todo),
        'cached': len(emails) - len(todo),
        'analyzed_at': min(shown) if shown else None,
    }


# ── CLI: print the summary as text ────────────────────────────────────────────

def format_summary_text(day: dict, analysis: dict) -> str:
    """Plain-text/markdown rendering of a summary (CLI output)."""
    counts = day['counts']
    lines = [
        f"# Email summary — {day['date']}",
        "",
        f"{counts['filed'] + counts['unfiled']} processed · "
        f"{len(analysis.get('action_required', []))} need attention · "
        f"{counts['unfiled']} unfiled ({counts['senders']} senders) · {counts['filed']} filed",
    ]
    if analysis.get('error'):
        lines += ["", f"AI analysis unavailable: {analysis['error']}"]

    lines += ["", "## Need attention"]
    items = analysis.get('action_required', [])
    lines += [f"- {it.get('subject', '(no subject)')} — {it.get('from', '')}: {it.get('reason', '')}"
              for it in items] or ["- (none)"]

    suggestions = {s['index']: s for s in analysis.get('filing_suggestions', [])}
    lines += ["", "## Unfiled"]
    for i, em in enumerate(day['inbox'], 1):
        count = f" ({em['count']}×)" if em.get('count', 1) > 1 else ""
        line = f"- {em.get('subject', '(no subject)')}{count} — {em.get('from_display') or em.get('from_addr', '')}"
        sug = suggestions.get(i)
        if sug and sug.get('suggested_label'):
            new = " (new label)" if sug.get('is_new_label') else ""
            line += f"\n  → {sug['suggested_label']}{new}: {sug.get('reason', '')}"
        lines.append(line)
    if not day['inbox']:
        lines.append("- (none)")

    lines += ["", "## Filed"]
    for label in sorted(day['filed']):
        lines.append(f"- {label.replace('MailMatrixCategories/', '')}: {len(day['filed'][label])}")
    if not day['filed']:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"


def main() -> None:
    setup_logging('email_summary.log')

    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if args:
        try:
            target_date = datetime.strptime(args[0], '%Y-%m-%d').date()
        except ValueError:
            print("Usage: python emailSummary.py [YYYY-MM-DD]", file=sys.stderr)
            sys.exit(1)
    else:
        target_date = date.today()

    try:
        imap = open_imap()
    except SummaryError as exc:
        log.error(str(exc))
        sys.exit(1)
    try:
        day = collect_day(imap, target_date)
    finally:
        imap.logout()

    analysis = analyze_senders(day['inbox'], day['labels'])
    print(format_summary_text(day, analysis), end="")


if __name__ == "__main__":
    main()
