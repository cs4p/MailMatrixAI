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
- **Batched FETCH mocks**: `imap.fetch` receives a *comma-joined ID set* (e.g. `b"1,2,3"`), not one call per message (`commonFunctions.fetch_many`). A side effect must emit a multi-message response: per message, a header tuple whose prefix starts with the sequence number (`b"1 (BODY[HEADER...] {n}"`), optionally a text continuation tuple (no sequence number; the server echoes `<0.2000>` back as `<0>`), then a bare `b")"`. See `_combined_fetch` in `test_app.py`.
- **Flask tests**: use `client` fixture from `test_app.py` which patches `RULES_PATH`, `SUMMARY_DIR`, and `ENV_PATH` to tmp paths so tests never touch real files (it also resets the module-level inbox-count cache).
- **Keychain**: an autouse fixture in `conftest.py` fakes `keyring` and invalidates the credential-blob cache around every test — never rely on real Keychain state.
- **Anthropic mock**: `client.messages.stream(...)` is a context manager — mock via `mock_cm.__enter__.return_value = mock_stream`.
- New Flask routes → tests in `tests/test_app.py`. New script functions → tests in the matching `tests/test_<script>.py`.

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Dependencies are declared in `pyproject.toml` (`python-dotenv`, `anthropic`, `flask`, `keyring`). Python 3.10+ required.

## Running the Scripts

```bash
# Desktop app (Electron wrapper around the web UI)
cd electron && npm install && npm start

# Web UI
python app.py                         # start Flask dev server at http://localhost:5000

# CLI scripts
python emailRulesInit.py              # rebuild emailRules.json from mailbox history
python sortEmail.py                   # file today's INBOX messages
python emailSummary.py                # summary for today
python emailSummary.py 2026-06-27     # summary for a specific date
python emailSummary.py --no-serve     # generate report without launching browser server
```

`app.py` reads `MAILMATRIX_HOST` (default `127.0.0.1`), `MAILMATRIX_PORT` (default `5000`), and `MAILMATRIX_DEBUG` (default off) from the environment. The Electron wrapper (`electron/main.js`) spawns `.venv/bin/python app.py` on a free port using these.

## Configuration

Credentials live in the **macOS Keychain** as a single JSON blob (service `MailMatrixAI`, account `credentials`) accessed via `commonFunctions.get_credential()`/`set_credential()` (the decoded blob is cached in-process). `get_credential` falls back to `os.environ` for keys missing from the blob. A `.env` file (loaded via `python-dotenv`) still works as a seed: on `app.py` startup, `_migrate_env_to_keychain()` copies these keys into the Keychain once:

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
| `GET /config` | Credentials form (reads from Keychain via `get_credential`) |
| `GET /api/inbox-stats` | Returns `{inbox_count, connected}` (count cached 30s; invalidated by sort/accept) |
| `POST /api/sort` | Runs `sortEmail.py` as subprocess |
| `POST /api/generate-summary` | Runs `emailSummary.py --no-serve` as subprocess |
| `POST /accept` | Calls `accept_filing()` from `emailSummary.py` |
| `POST /api/config` | Writes fields to the Keychain via `set_credential` |
| `GET /api/test-connection` | Attempts IMAP connect/logout, returns `{ok, message/error}` |

POST bodies are read through `_json_body()` (never `request.get_json` directly) so malformed/non-object bodies degrade to `{}` and the handlers' own validation runs instead of a 500. Inbox-analysis jobs (`/api/inbox-analyze/*`) run in daemon threads tracked in `_inbox_jobs`; always look jobs up via `_get_job()` (takes the lock).

Templates in `templates/` extend `templates/base.html`. Static files in `static/` (`style.css`, `app.js`). Apple-inspired design: `#f5f5f7` bg, white cards, `border-radius: 16px`, system font stack, accent colors green/red/orange/blue.

### `commonFunctions.py` — shared utilities

All scripts import from here:
- `imap_call(fn)` — wraps any IMAP operation with exponential-backoff retry on rate-limit errors (`_RATE_LIMIT_PHRASES`)
- `fetch_many(imap, msg_ids, parts)` — batched FETCH over comma-joined ID sets (chunked by `FETCH_CHUNK_SIZE`); returns `{msg_id: {"header": bytes|None, "text": bytes|None}}`. All multi-message fetch paths go through this — never fetch in a per-message loop
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

`load_rules()` builds two lookup dicts (`email_to_labels`, `domain_to_labels`) from `emailRules.json`. `sort_inbox()` batch-fetches only `BODY[HEADER.FIELDS (FROM)]` via `fetch_many`, then `imap.copy()` to each matching label and `\Deleted` + `expunge` to remove from INBOX. **A message is only flagged `\Deleted` after every COPY returned OK** — a failed copy must never destroy the original (same rule in `accept_filing` and `move_imap_messages`). Uses the lambda default-arg pattern to capture loop variables: `lambda mid=msg_id: imap.copy(mid, ...)`.

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
