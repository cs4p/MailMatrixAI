# MailMatrix AI

An email management pipeline with a Flask web UI. Connects to any IMAP server to automatically file incoming mail into folders, and generates AI-powered daily summary reports using Claude.

## How it works

Three CLI scripts handle the core pipeline:

1. **`emailRulesInit.py`** — Crawls all `MailMatrixCategories/*` IMAP folders, extracts the sender address from every message, and writes `emailRules.json` (your filing rules).
2. **`sortEmail.py`** — Reads `emailRules.json` and moves matching INBOX messages into their folders.
3. **`emailSummary.py`** — Generates a daily HTML report: action-required items, unmatched INBOX emails with Claude-suggested folders, and a log of what was filed.

The web UI (`app.py`) wraps all three scripts and adds a rules browser with search, faceted filtering, and inline editing.

## Setup

**Prerequisites:** Python 3.9+, an IMAP-enabled mail account, and an [Anthropic API key](https://console.anthropic.com/settings/keys).

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
