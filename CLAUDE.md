# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MailMatrixAI is a Gmail management pipeline with a Flask web UI and three CLI scripts that operate via IMAP:

1. **`emailRulesInit.py`** — crawls all `MailMatrixCategories/*` Gmail labels, extracts sender addresses from every message, and writes `emailRules.json`
2. **`sortEmail.py`** — reads `emailRules.json` and files INBOX messages into their matching labels (then removes them from INBOX)
3. **`emailSummary.py`** — generates a daily markdown report: action-required messages, unmatched INBOX emails with Claude-suggested labels, and a list of what was filed where

## Testing

Always add tests when adding new code. Run the suite with:

```bash
python -m pytest
```

Tests live in `tests/`. Key patterns:
- **IMAP mocks**: every IMAP method must return a 2-tuple `("OK", data)` — the `imap_call` wrapper unpacks this. Use `MagicMock()` with explicit `return_value` assignments per method.
- **Flask tests**: use `client` fixture from `test_app.py` which patches `RULES_PATH`, `SUMMARY_DIR`, and `ENV_PATH` to tmp paths so tests never touch real files.
- **Anthropic mock**: `client.messages.stream(...)` is a context manager — mock via `mock_cm.__enter__.return_value = mock_stream`.
- New Flask routes → tests in `tests/test_app.py`. New script functions → tests in the matching `tests/test_<script>.py`.

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Dependencies are declared in `pyproject.toml` (`python-dotenv`, `anthropic`, `flask`). Python 3.9+ required.

## Running the Scripts

Copy `.env.example` to `.env` and fill in credentials before running anything.

```bash
# Web UI (recommended)
python app.py                         # start Flask dev server at http://localhost:5000

# CLI scripts
python emailRulesInit.py              # rebuild emailRules.json from mailbox history
python sortEmail.py                   # file today's INBOX messages
python emailSummary.py                # summary for today
python emailSummary.py 2026-06-27     # summary for a specific date
python emailSummary.py --no-serve     # generate report without launching browser server
```

## Configuration

All scripts load credentials from `.env` via `python-dotenv`:

```
IMAP_SERVER=imap.gmail.com
IMAP_PORT=993
IMAP_USERNAME=your_email@example.com
IMAP_PASSWORD=your_app_password
ANTHROPIC_API_KEY=sk-ant-...
```

`emailRules.json` and `.env` are gitignored.

## Architecture

### `app.py` — Flask web UI

Four pages served at `/`, `/summaries`, `/rules`, `/config`. API endpoints under `/api/`:

| Route | Purpose |
|---|---|
| `GET /` | Dashboard: stat cards (live inbox count via IMAP), action buttons |
| `GET /summaries` | List `.html` files from `emailSummary/` |
| `GET /summaries/<filename>` | Serve a saved summary HTML file |
| `GET /rules` | Faceted search over `emailRules.json` |
| `GET /config` | Credentials form (reads `.env`) |
| `GET /api/inbox-stats` | Returns `{inbox_count, connected}` |
| `POST /api/sort` | Runs `sortEmail.py` as subprocess |
| `POST /api/generate-summary` | Runs `emailSummary.py --no-serve` as subprocess |
| `POST /accept` | Calls `accept_filing()` from `emailSummary.py` |
| `POST /api/config` | Writes fields to `.env` via `dotenv.set_key` |
| `GET /api/test-connection` | Attempts IMAP connect/logout, returns `{ok, message/error}` |

Templates in `templates/` extend `templates/base.html`. Static files in `static/` (`style.css`, `app.js`). Apple-inspired design: `#f5f5f7` bg, white cards, `border-radius: 16px`, system font stack, accent colors green/red/orange/blue.

### `commonFunctions.py` — shared utilities

All scripts import from here:
- `imap_call(fn)` — wraps any IMAP operation with exponential-backoff retry on rate-limit errors (`_RATE_LIMIT_PHRASES`)
- `connect_to_imap()` — `IMAP4_SSL` + login
- `extract_email_address()` — pulls bare address from `"Name <addr>"` strings
- `get_all_labels(imap, parent_label=None)` — lists folders, optionally filtered to children of `parent_label`
- `decode_header_value()` / `parse_headers()` — RFC2047-safe header decoding and From/Subject/Date extraction
- `imap_date(d)` — formats a `date` as `D-Mon-YYYY` for IMAP SEARCH `ON`
- `setup_logging(log_file)` — configures `logging.basicConfig` with stdout + file handler; call at the top of each `main()`

### Gmail label conventions

Labels under `MailMatrixCategories/` are the only ones touched (e.g. `MailMatrixCategories/Work`). The `/` separator is the Gmail IMAP hierarchy delimiter. Folder names must be quoted in IMAP commands: `imap.select('"MailMatrixCategories/Work"')`.

### `emailRulesInit.py` pipeline

`get_all_labels(imap, parent_label="MailMatrixCategories")` → `crawl_emails_in_label()` (fetches full RFC822, extracts `From:`) → `build_email_rules()` → `write_to_json()` → `emailRules.json`

`emailDomains` in the schema is always written as `[]` — domain inference is not implemented.

### `sortEmail.py` pipeline

`load_rules()` builds two lookup dicts (`email_to_labels`, `domain_to_labels`) from `emailRules.json`. `sort_inbox()` fetches only `BODY[HEADER.FIELDS (FROM)]` for efficiency, then `imap.copy()` to each matching label and `\Deleted` + `expunge` to remove from INBOX. Uses the lambda default-arg pattern to capture loop variables: `lambda mid=msg_id: imap.fetch(mid, ...)`.

### `emailSummary.py` pipeline

1. Fetch `MailMatrixCategories/*` folders for the target date (`IMAP SEARCH ON DD-Mon-YYYY`) — headers only
2. Fetch INBOX for the target date with `BODY[TEXT]<0.2000>` body snippets
3. Deduplicate INBOX emails by sender (`deduplicate_inbox_emails`) — one card per unique `from_addr`
4. Call `claude-opus-4-8` (streaming, adaptive thinking) with a JSON-structured prompt asking for action classification and filing suggestions
5. Write `emailSummary/email_summary_YYYY-MM-DD.html` — an HTML report with three sections
6. Start a local HTTP server (`socketserver.ThreadingMixIn + TCPServer`) on a random port and open the browser

The HTML report has **Accept** buttons on unmatched email cards. Clicking one POSTs `{from_addr, label}` to `/accept` on the local server, which reconnects to IMAP, moves all INBOX messages from that sender to the label, and patches `emailRules.json`. The server runs until Ctrl+C.

Claude response is parsed as JSON (`action_required`, `filing_suggestions` arrays indexed by email position). Falls back gracefully if Claude doesn't return valid JSON.

### `emailRules.json` schema

Defined in `emailRules.schema.json` (JSON Schema draft-07):
```json
{
  "labels": [
    {
      "labelName": "MailMatrixCategories/Work",
      "emailAddresses": ["alice@example.com"],
      "emailDomains": []
    }
  ]
}
```
