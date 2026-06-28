# MailMatrix AI

A Gmail management pipeline with a Flask web UI. Connects to Gmail via IMAP to automatically file incoming mail into labeled folders, and generates AI-powered daily summary reports using Claude.

## How it works

Three CLI scripts handle the core pipeline:

1. **`emailRulesInit.py`** — Crawls all `MailMatrixCategories/*` Gmail labels, extracts the sender address from every message, and writes `emailRules.json` (your filing rules).
2. **`sortEmail.py`** — Reads `emailRules.json` and moves matching INBOX messages into their labels.
3. **`emailSummary.py`** — Generates a daily HTML report: action-required items, unmatched INBOX emails with Claude-suggested labels, and a log of what was filed.

The web UI (`app.py`) wraps all three scripts and adds a rules browser with search, faceted filtering, and inline editing.

## Setup

**Prerequisites:** Python 3.9+, a Gmail account with IMAP enabled, a Gmail [App Password](https://support.google.com/accounts/answer/185833), and an [Anthropic API key](https://console.anthropic.com/settings/keys).

```bash
git clone <repo>
cd MailMatrixAI

python -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env and fill in your credentials
```

### `.env` configuration

```
IMAP_SERVER=imap.gmail.com
IMAP_PORT=993
IMAP_USERNAME=your_email@gmail.com
IMAP_PASSWORD=your_app_password
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

### Web UI (recommended)

```bash
python app.py
# Open http://localhost:5000
```

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

## Gmail label conventions

Filing targets must live under a `MailMatrixCategories/` parent label in Gmail (e.g. `MailMatrixCategories/Work`, `MailMatrixCategories/Newsletters`). Create these labels in Gmail before running `emailRulesInit.py`.

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

106 tests, 85% coverage. IMAP and Anthropic API are fully stubbed — no network calls during tests.

## Project structure

```
app.py                 Flask web UI and API routes
commonFunctions.py     Shared IMAP utilities, retry logic, header parsing
emailRulesInit.py      Crawl labels → emailRules.json
sortEmail.py           Sort INBOX using emailRules.json
emailSummary.py        Generate daily HTML report with Claude analysis
templates/             Jinja2 page templates
static/                CSS and JS
tests/                 pytest suite
emailRules.schema.json JSON Schema for emailRules.json
```
