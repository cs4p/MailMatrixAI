# MailMatrix AI

An email management pipeline with a Flask web UI. Connects to any IMAP server to automatically file incoming mail into folders, and generates AI-powered daily summary reports using Claude.

## How it works

Three CLI scripts handle the core pipeline:

1. **`emailRulesInit.py`** — Crawls all `MailMatrixCategories/*` IMAP folders, extracts the sender address from every message, and writes `emailRules.json` (your filing rules).
2. **`sortEmail.py`** — Reads `emailRules.json` and moves matching INBOX messages into their folders.
3. **`emailSummary.py`** — Generates a daily HTML report: action-required items, unmatched INBOX emails with Claude-suggested folders, and a log of what was filed.

The web UI (`app.py`) wraps all three scripts and adds a rules browser with search, faceted filtering, and inline editing.

## Setup

**Prerequisites:** Python 3.10+, an IMAP-enabled mail account, and an [Anthropic API key](https://console.anthropic.com/settings/keys).

```bash
git clone <repo>
cd MailMatrixAI

python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Credentials

Credentials are stored in the macOS Keychain (one consolidated item, service
`MailMatrixAI`) and managed from the web UI's **Config** page — no config file
needed. Alternatively, create a `.env` (see `.env.example`); its values are
migrated into the Keychain the first time `app.py` starts:

```
IMAP_SERVER=imap.your-provider.com
IMAP_PORT=993
IMAP_USERNAME=your@email.com
IMAP_PASSWORD=your_password
ANTHROPIC_API_KEY=sk-ant-...
```

Common server addresses:

| Provider | IMAP server |
|---|---|
| Gmail | `imap.gmail.com` (use an [App Password](https://support.google.com/accounts/answer/185833)) |
| Fastmail | `imap.fastmail.com` |
| Outlook / Hotmail | `outlook.office365.com` |
| Apple iCloud | `imap.mail.me.com` |
| Yahoo | `imap.mail.yahoo.com` |

## Usage

### Desktop app (Electron)

```bash
cd electron
npm install
npm start
```

Opens the web UI in its own window, running the Flask backend on a free local
port and shutting it down when the window closes. See `electron/README.md`.

### Web UI

```bash
python app.py
# Open http://localhost:5000
```

The server is configurable via environment variables: `MAILMATRIX_HOST`
(default `127.0.0.1`), `MAILMATRIX_PORT` (default `5000`), and
`MAILMATRIX_DEBUG=1` to enable Flask debug mode (off by default).

| Page | Path | What it does |
|---|---|---|
| Dashboard | `/` | Inbox count, sort button, generate summaries for any date |
| Summaries | `/summaries` | Browse and view saved HTML reports |
| Rules | `/rules` | Search and edit filing rules by sender or domain |
| Config | `/config` | Update credentials, test IMAP connection |

### CLI

```bash
python emailRulesInit.py              # rebuild emailRules.json from mailbox history
python sortEmail.py                   # file today's INBOX messages
python emailSummary.py                # summary for today
python emailSummary.py 2026-06-27     # summary for a specific date
python emailSummary.py --no-serve     # generate report without opening a browser
```

## Folder conventions

Filing targets must live under a `MailMatrixCategories/` parent folder (e.g. `MailMatrixCategories/Work`, `MailMatrixCategories/Newsletters`). Create these folders in your mail client before running `emailRulesInit.py`. The `/` character is the IMAP hierarchy delimiter — most providers support nested folders this way.

## Filing rules

Rules are stored in `emailRules.json` (gitignored). Each entry maps a label to a list of exact sender addresses and/or domains:

```json
{
  "labels": [
    {
      "labelName": "MailMatrixCategories/Work",
      "emailAddresses": ["alice@corp.com", "bob@corp.com"],
      "emailDomains": ["corp.com"]
    }
  ]
}
```

Domain rules match any sender at that domain. The Rules page in the web UI lets you convert individual sender rules into domain rules with one click.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest
```

256 tests. IMAP, the Anthropic API, and the macOS Keychain are fully stubbed — no network or Keychain access during tests.

## Project structure

```
app.py                 Flask web UI and API routes
commonFunctions.py     Shared IMAP utilities, retry logic, header parsing
emailRulesInit.py      Crawl labels → emailRules.json
sortEmail.py           Sort INBOX using emailRules.json
emailSummary.py        Generate daily HTML report with Claude analysis
cleanupRules.py        Interactive rules optimizer (also backs the /cleanup page)
electron/              Electron desktop wrapper (npm start)
templates/             Jinja2 page templates
static/                CSS and JS
tests/                 pytest suite
emailRules.schema.json JSON Schema for emailRules.json
```
