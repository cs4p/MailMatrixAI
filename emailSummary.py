import imaplib
import json
import logging
import os
import re
import socket
import socketserver
import subprocess
import sys
import webbrowser
from datetime import date, datetime
from html import escape as _e
from http.server import BaseHTTPRequestHandler
from typing import Callable, Dict, List, Optional

import anthropic
from dotenv import load_dotenv

from commonFunctions import (
    connect_to_imap,
    decode_header_value,
    extract_email_address,
    get_all_labels,
    imap_call,
    imap_date,
    parse_headers,
    rules_lock,
    setup_logging,
    validate_email_address,
    validate_label,
)

log = logging.getLogger(__name__)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── CSS & JS as plain strings (no brace-doubling needed) ─────────────────────

_CSS = """\
*, *::before, *::after { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  max-width: 860px; margin: 0 auto; padding: 2rem 1.5rem;
  background: #f5f5f7; color: #1d1d1f; line-height: 1.5;
}
h1 { font-size: 1.75rem; font-weight: 700; margin: 0 0 1.5rem; }
.stat-bar { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
.stat {
  background: white; padding: 1rem 1.5rem; border-radius: 12px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08); min-width: 120px;
}
.stat-num { font-size: 2rem; font-weight: 700; line-height: 1; }
.stat-label { font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; color: #86868b; margin-top: .25rem; }
.red { color: #ff3b30; }
.orange { color: #ff9500; }
.green { color: #34c759; }
section {
  background: white; border-radius: 16px; padding: 1.5rem;
  margin: 1.25rem 0; box-shadow: 0 1px 4px rgba(0,0,0,.08);
}
h2 {
  font-size: .75rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .1em; color: #86868b; margin: 0 0 1rem;
  padding-bottom: .75rem; border-bottom: 1px solid #f0f0f0;
}
.section-desc { font-size: .8rem; color: #86868b; margin: -.25rem 0 1rem; }
.empty { color: #86868b; font-size: .875rem; font-style: italic; }
.email-card { border: 1px solid #e5e5ea; border-radius: 10px; padding: 1rem; margin: .75rem 0; }
.action-card { border-left: 3px solid #ff3b30; }
.email-subject { font-weight: 600; font-size: .95rem; }
.email-meta { font-size: .8rem; color: #86868b; margin-top: .2rem; }
.email-preview {
  font-size: .8rem; color: #6e6e73; margin: .6rem 0;
  padding: .6rem .75rem; background: #f9f9f9;
  border-radius: 6px; font-style: italic;
}
.action-reason { font-size: .85rem; color: #ff3b30; margin-top: .4rem; }
.suggestion { display: flex; align-items: center; gap: .6rem; margin-top: .75rem; flex-wrap: wrap; }
.label-tag {
  font-size: .8rem; font-family: 'SF Mono', 'Fira Code', monospace;
  padding: .25rem .6rem; border-radius: 6px; background: #e8f4fd; color: #0066cc;
}
.label-tag.new-label { background: #fff3e0; color: #e65100; }
.new-badge {
  font-size: .7rem; background: #fff3e0; color: #e65100;
  padding: .15rem .4rem; border-radius: 4px; border: 1px solid #ffcc80;
}
.label-select {
  font-size: .8rem; font-family: 'SF Mono', 'Fira Code', monospace;
  padding: .25rem .5rem; border-radius: 6px; border: 1px solid #c7d8ed;
  background: #e8f4fd; color: #0066cc; cursor: pointer;
  max-width: 220px;
}
.label-select:focus { outline: 2px solid #0066cc; outline-offset: 1px; }
.label-select:disabled { opacity: .6; cursor: default; }
.suggestion-reason { font-size: .8rem; color: #86868b; flex: 1; min-width: 0; }
.accept-btn {
  padding: .35rem .9rem; background: #34c759; color: white;
  border: none; border-radius: 8px; cursor: pointer;
  font-size: .8rem; font-weight: 600; white-space: nowrap;
  transition: background .15s, transform .1s;
}
.accept-btn:hover:not(:disabled) { background: #28a745; transform: translateY(-1px); }
.accept-btn:disabled { background: #a8d5b5; cursor: default; transform: none; }
.accepted-msg { font-size: .85rem; color: #34c759; font-weight: 600; }
.count-badge {
  display: inline-block; background: #86868b; color: white;
  font-size: .65rem; font-weight: 700; padding: .1rem .4rem;
  border-radius: 10px; margin-left: .3rem; vertical-align: middle;
}
.filed-label { margin: .75rem 0; }
.filed-label h3 { font-size: .875rem; color: #3c3c43; margin: 0 0 .4rem; }
.filed-item { font-size: .8rem; padding: .3rem 0; border-bottom: 1px solid #f0f0f0; color: #3c3c43; }
.filed-item:last-child { border-bottom: none; }
.error-note {
  background: #fff3cd; border: 1px solid #ffc107;
  padding: .75rem 1rem; border-radius: 8px; font-size: .875rem;
}
"""

_JS = """\
async function handleAccept(btn) {
  const fromAddr = btn.dataset.from;
  const sug = btn.closest('.suggestion');
  const select = sug.querySelector('.label-select');
  const label = select ? select.value : btn.dataset.label;
  btn.disabled = true;
  btn.textContent = 'Moving…';
  if (select) select.disabled = true;
  try {
    const res = await fetch('/accept', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({from_addr: fromAddr, label: label})
    });
    const data = await res.json();
    if (data.ok) {
      const shortLabel = label.replace('MailMatrixCategories/', '');
      sug.innerHTML = '<span class="accepted-msg">✓ Moved to ' + shortLabel + ' · emailRules.json updated</span>';
    } else {
      btn.disabled = false;
      btn.textContent = 'Accept';
      if (select) select.disabled = false;
      alert('Error: ' + (data.error || 'Unknown error'));
    }
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Accept';
    if (select) select.disabled = false;
    alert('Error: ' + err.message);
  }
}
"""

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
    emails = []

    for msg_id in message_ids:
        try:
            status, data = imap_call(
                lambda mid=msg_id: imap.fetch(mid, '(BODY[HEADER.FIELDS (FROM SUBJECT DATE)])')
            )
            if status != 'OK' or not data or not data[0]:
                continue

            raw = data[0][1].decode(errors='replace')
            headers = parse_headers(raw)

            emails.append({
                'msg_id': msg_id,
                'from_display': headers['from'],
                'from_addr': extract_email_address(headers['from']),
                'subject': headers['subject'] or '(no subject)',
                'date': headers['date'],
            })
        except Exception as exc:
            log.error("Error fetching message %s in '%s': %s", msg_id, folder, exc)

    return emails


def fetch_inbox_with_body(imap: imaplib.IMAP4_SSL, date_filter: str) -> List[dict]:
    emails = fetch_folder_emails(imap, 'INBOX', date_filter)

    for em in emails:
        try:
            status, data = imap_call(
                lambda mid=em['msg_id']: imap.fetch(mid, '(BODY[TEXT]<0.2000>)')
            )
            snippet = ''
            if status == 'OK' and data and data[0] and isinstance(data[0], tuple):
                raw_body = data[0][1]
                if raw_body:
                    text = raw_body.decode(errors='replace')
                    text = re.sub(r'<[^>]+>', ' ', text)
                    text = re.sub(r'\s+', ' ', text).strip()
                    snippet = text[:400]
            em['body_snippet'] = snippet
        except Exception as exc:
            log.debug("Could not fetch body for %s: %s", em['msg_id'], exc)
            em['body_snippet'] = ''

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

def analyze_with_claude(inbox_emails: List[dict], available_labels: List[str]) -> dict:
    if not inbox_emails:
        return {'action_required': [], 'filing_suggestions': []}

    client = anthropic.Anthropic()

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

    log.info("Calling Claude to analyze %d inbox emails...", len(inbox_emails))

    try:
        with client.messages.stream(
            model="claude-opus-4-8",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError:
        log.error("Anthropic API key is invalid — check ANTHROPIC_API_KEY in .env")
        return {'action_required': [], 'filing_suggestions': [],
                '_error': 'Invalid API key — check ANTHROPIC_API_KEY in .env'}
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

    log.warning("Claude did not return valid JSON. Raw response:\n%s", text[:500])
    return {'action_required': [], 'filing_suggestions': []}


# ── HTML report builder ───────────────────────────────────────────────────────

def build_html_report(
    target_date: date,
    inbox_emails: List[dict],
    filed_emails: Dict[str, List[dict]],
    analysis: dict,
    available_labels: Optional[List[str]] = None,
) -> str:
    total_filed = sum(len(v) for v in filed_emails.values())
    action_items = analysis.get('action_required', [])
    filing_suggestions = {
        s['index']: s
        for s in analysis.get('filing_suggestions', [])
        if 'index' in s
    }
    error_note = analysis.get('_error', '')

    # Action Required
    if error_note:
        action_html = f'<div class="error-note">AI analysis unavailable: {_e(error_note)}</div>'
    elif action_items:
        cards = []
        for item in action_items:
            cards.append(
                f'<div class="email-card action-card">'
                f'<div class="email-subject">{_e(item.get("subject", "(no subject)"))}</div>'
                f'<div class="email-meta">From: {_e(item.get("from", ""))}</div>'
                f'<div class="action-reason">{_e(item.get("reason", ""))}</div>'
                f'</div>'
            )
        action_html = '\n'.join(cards)
    else:
        action_html = '<p class="empty">No emails require immediate action.</p>'

    # Unmatched Emails
    if inbox_emails:
        cards = []
        for i, em in enumerate(inbox_emails, 1):
            sug = filing_suggestions.get(i, {})
            count = em.get('count', 1)
            count_badge = f'<span class="count-badge">{count}×</span>' if count > 1 else ''

            preview = ''
            if em.get('body_snippet'):
                preview = f'<div class="email-preview">{_e(em["body_snippet"][:200])}</div>'

            suggestion_html = ''
            if sug.get('suggested_label'):
                suggested = sug['suggested_label']
                is_new = sug.get('is_new_label', False)
                reason = f'<span class="suggestion-reason">{_e(sug.get("reason", ""))}</span>'

                # Build option list: all known labels + suggested (if new) pre-selected
                all_opts = list(available_labels or [])
                if suggested not in all_opts:
                    all_opts = [suggested] + all_opts
                options_html = ''.join(
                    f'<option value="{_e(lbl)}"'
                    f'{" selected" if lbl == suggested else ""}>'
                    f'{_e(lbl.replace("MailMatrixCategories/", ""))}'
                    f'</option>'
                    for lbl in all_opts
                )
                new_badge = ' <span class="new-badge">new label</span>' if is_new else ''
                select_html = f'<select class="label-select">{options_html}</select>{new_badge}'
                btn = (
                    f'<button class="accept-btn"'
                    f' data-from="{_e(em["from_addr"])}"'
                    f' onclick="handleAccept(this)">Accept</button>'
                )
                suggestion_html = (
                    f'<div class="suggestion">'
                    f'{select_html}'
                    f'{reason}{btn}'
                    f'</div>'
                )

            cards.append(
                f'<div class="email-card">'
                f'<div class="email-subject">{_e(em.get("subject", "(no subject)"))}{count_badge}</div>'
                f'<div class="email-meta">From: {_e(em.get("from_display", em.get("from_addr", "")))}</div>'
                f'<div class="email-meta">Date: {_e(em.get("date", ""))}</div>'
                f'{preview}{suggestion_html}'
                f'</div>'
            )
        unmatched_html = '\n'.join(cards)
    else:
        unmatched_html = '<p class="empty">No unmatched emails for this date.</p>'

    # Filed Emails
    if total_filed > 0:
        groups = []
        for label in sorted(filed_emails.keys()):
            emails = filed_emails[label]
            if not emails:
                continue
            short = _e(label.replace('MailMatrixCategories/', ''))
            items = ''.join(
                f'<div class="filed-item"><strong>{_e(em.get("subject", "(no subject)"))}</strong>'
                f' — {_e(em.get("from_display", em.get("from_addr", "")))}</div>'
                for em in emails
            )
            groups.append(
                f'<div class="filed-label">'
                f'<h3>{short} <span class="count-badge">{len(emails)}</span></h3>'
                f'{items}</div>'
            )
        filed_html = '\n'.join(groups)
    else:
        filed_html = '<p class="empty">No emails were filed on this date.</p>'

    date_heading = f"{target_date.strftime('%B')} {target_date.day}, {target_date.year}"

    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'  <meta charset="utf-8">\n'
        f'  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'  <title>Email Summary — {date_heading}</title>\n'
        f'  <style>{_CSS}</style>\n'
        f'</head>\n<body>\n'
        f'  <h1>Email Summary — {date_heading}</h1>\n'
        f'  <div class="stat-bar">\n'
        f'    <div class="stat"><div class="stat-num orange">{len(inbox_emails)}</div>'
        f'<div class="stat-label">Unmatched</div></div>\n'
        f'    <div class="stat"><div class="stat-num red">{len(action_items)}</div>'
        f'<div class="stat-label">Action Required</div></div>\n'
        f'    <div class="stat"><div class="stat-num green">{total_filed}</div>'
        f'<div class="stat-label">Filed</div></div>\n'
        f'  </div>\n'
        f'  <section>\n    <h2>Action Required</h2>\n    {action_html}\n  </section>\n'
        f'  <section>\n    <h2>Unmatched Emails</h2>\n'
        f'    <p class="section-desc">In INBOX with no matching filing rule. '
        f'Accept a suggestion to move the email and update emailRules.json.</p>\n'
        f'    {unmatched_html}\n  </section>\n'
        f'  <section>\n    <h2>Filed Emails</h2>\n'
        f'    <p class="section-desc">Automatically filed by sortEmail.py.</p>\n'
        f'    {filed_html}\n  </section>\n'
        f'  <script>{_JS}</script>\n'
        f'</body>\n</html>'
    )


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
            status, messages = imap_call(lambda: imap.search(None, f'FROM "{from_addr}"'))
            msg_ids = messages[0].split() if status == 'OK' else []

            for mid in msg_ids:
                imap_call(lambda m=mid: imap.copy(m, f'"{label}"'))
                imap_call(lambda m=mid: imap.store(m, '+FLAGS', '\\Deleted'))

            if msg_ids:
                imap_call(lambda: imap.expunge())

            log.info("Moved %d message(s) from %s to %s", len(msg_ids), from_addr, label)
        finally:
            imap.logout()
    except Exception as exc:
        log.error("IMAP error during accept: %s", exc)
        return {'ok': False, 'error': str(exc)}

    # Update emailRules.json
    try:
        with rules_lock:
            with open(rules_path, 'r', encoding='utf-8') as f:
                rules = json.load(f)

            for entry in rules.get('labels', []):
                if entry['labelName'] == label:
                    addrs = entry.setdefault('emailAddresses', [])
                    if from_addr not in addrs:
                        addrs.append(from_addr)
                        addrs.sort()
                    break
            else:
                rules.setdefault('labels', []).append({
                    'labelName': label,
                    'emailAddresses': [from_addr],
                    'emailDomains': [],
                })

            with open(rules_path, 'w', encoding='utf-8') as f:
                json.dump(rules, f, indent=2, ensure_ascii=False)

        log.info("emailRules.json updated: %s -> %s", from_addr, label)
    except Exception as exc:
        log.error("Failed to update %s: %s", rules_path, exc)
        return {'ok': False, 'error': f'Emails moved but rules update failed: {exc}'}

    return {'ok': True, 'moved': len(msg_ids)}


# ── Local report server ───────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def serve_report(html: str, accept_fn: Callable[[dict], dict]) -> None:
    port = _free_port()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/':
                body = html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == '/accept':
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length))
                result = accept_fn(data)
                payload = json.dumps(result).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass  # suppress HTTP access log noise

    class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with _Server(('127.0.0.1', port), Handler) as server:
        url = f'http://localhost:{port}'
        log.info("Report server at %s", url)
        print(f"\nReport ready at {url}")
        print("Press Ctrl+C to stop.\n")
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    setup_logging('email_summary.log')

    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    no_serve = '--no-serve' in sys.argv

    if args:
        try:
            target_date = datetime.strptime(args[0], '%Y-%m-%d').date()
        except ValueError:
            print("Usage: python emailSummary.py [YYYY-MM-DD] [--no-serve]", file=sys.stderr)
            sys.exit(1)
    else:
        target_date = date.today()

    log.info("Generating email summary for %s", target_date)

    # Sort inbox first so the summary reflects the post-filing state
    sort_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sortEmail.py")
    log.info("Running sortEmail.py before generating summary...")
    sort_result = subprocess.run(
        [sys.executable, sort_script],
        capture_output=True, text=True, timeout=180,
    )
    if sort_result.returncode == 0:
        log.info("Sort completed successfully")
    else:
        log.warning(
            "sortEmail.py exited with code %d — continuing anyway: %s",
            sort_result.returncode,
            sort_result.stderr[-500:],
        )

    imap_server = os.environ["IMAP_SERVER"]
    imap_port = int(os.environ.get("IMAP_PORT", "993"))
    username = os.environ["IMAP_USERNAME"]
    password = os.environ["IMAP_PASSWORD"]
    rules_path = os.environ.get("RULES_PATH", "emailRules.json")

    log.info("Connecting to %s:%d ...", imap_server, imap_port)
    imap = connect_to_imap(imap_server, username, password, imap_port)
    log.info("Authenticated as %s", username)

    date_filter = imap_date(target_date)

    try:
        labels = get_all_labels(imap, 'MailMatrixCategories')
        log.info("Found %d MailMatrixCategories labels", len(labels))

        filed_emails: Dict[str, List[dict]] = {}
        for label in labels:
            emails = fetch_folder_emails(imap, label, date_filter)
            if emails:
                filed_emails[label] = emails

        raw_inbox = fetch_inbox_with_body(imap, date_filter)

    finally:
        imap.logout()

    inbox_emails = deduplicate_inbox_emails(raw_inbox)
    log.info("INBOX: %d messages, %d unique senders", len(raw_inbox), len(inbox_emails))

    analysis = analyze_with_claude(inbox_emails, labels)

    report = build_html_report(target_date, inbox_emails, filed_emails, analysis, labels)

    # L5: anchor output to the script's directory, not CWD
    output_dir = os.path.join(_SCRIPT_DIR, "emailSummary")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"email_summary_{target_date.strftime('%Y-%m-%d')}.html")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(report)
    log.info("Report written to %s", output_file)

    total_filed = sum(len(v) for v in filed_emails.values())
    print(f"Summary written to {output_file}")
    print(
        f"  {len(inbox_emails)} unique unmatched senders, "
        f"{len(analysis.get('action_required', []))} action required, "
        f"{total_filed} filed"
    )

    if no_serve:
        return

    accept_fn = lambda body: accept_filing(
        body, imap_server, imap_port, username, password, rules_path
    )
    serve_report(report, accept_fn)


if __name__ == "__main__":
    main()
