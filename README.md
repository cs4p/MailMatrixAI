# MailMatrix AI

An email management pipeline with a Flask web UI. Connects to any IMAP server to automatically file incoming mail into folders, and generates AI-powered daily summary reports using Claude.

## How it works

Three CLI scripts handle the core pipeline:

1. **`emailRulesInit.py`** — Crawls all `MailMatrixCategories/*` IMAP folders, extracts the sender address from every message, and writes `emailRules.json` (your filing rules).
2. **`sortEmail.py`** — Reads `emailRules.json` and moves matching INBOX messages into their folders.
3. **`emailSummary.py`** — Generates a daily HTML report: action-required items, unmatched INBOX emails with Claude-suggested folders, and a log of what was filed.

The web UI (`app.py`) wraps all three scripts and adds a rules browser with search, faceted filtering, and inline editing, plus a full **Mail** client (`/mail`) — browse every folder, read messages (with sanitized HTML rendering and attachment downloads), compose/reply/forward over SMTP, create labels, and drag-and-drop a message onto a `MailMatrixCategories/*` label to move it and auto-create a filing rule.

> **Provider note:** the mail client is developed and tested against **Fastmail** (app-specific password with the SMTP scope enabled). It uses standard IMAP/SMTP, so other providers may work, but Gmail — which requires OAuth rather than app passwords — is not yet supported.

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
IMAP_SERVER=imap.fastmail.com
IMAP_PORT=993
IMAP_USERNAME=your@email.com
IMAP_PASSWORD=your_app_password
SMTP_SERVER=smtp.fastmail.com
SMTP_PORT=465
ANTHROPIC_API_KEY=sk-ant-...
```

`SMTP_SERVER`/`SMTP_PORT` are only needed to send mail from the Mail client
(they default to `smtp.fastmail.com:465` when unset); sending reuses the IMAP
username and app password, so the Fastmail app password must have the **SMTP**
scope enabled.

Common server addresses:

| Provider | IMAP server | SMTP server |
|---|---|---|
| Fastmail | `imap.fastmail.com` | `smtp.fastmail.com` |
| Outlook / Hotmail | `outlook.office365.com` | `smtp.office365.com` |
| Apple iCloud | `imap.mail.me.com` | `smtp.mail.me.com` |
| Yahoo | `imap.mail.yahoo.com` | `smtp.mail.yahoo.com` |

Gmail is intentionally omitted — it requires OAuth, which this app does not yet support.

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
| Mail | `/mail` | Full mail client: browse folders, read/compose/reply/forward, drag-and-drop filing |
| AI Inbox | `/inbox` | Claude-analyzed inbox recommendations |
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

### Docker / Kubernetes

A production container image is built and published to GHCR
(`ghcr.io/cs4p/mailmatrixai`) by [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml).
In a container there is no macOS Keychain, so credentials are read from the
environment instead (`IMAP_*`, `SMTP_*`, `ANTHROPIC_API_KEY`), and persistent
state lives in `MAILMATRIX_DATA_DIR` (`/data`).

Images are tagged with semantic versions (`0.3.0`, `0.3`) from `vX.Y.Z` git
tags, so deployments pin an immutable version and Renovate can bump it:

```bash
docker run --rm -p 5000:5000 --env-file .env ghcr.io/cs4p/mailmatrixai:0.4.0
```

Kubernetes manifests and step-by-step deployment instructions are in
[`k8s/`](k8s/README.md).

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

## Versioning

Every merge to `main` is versioned automatically by
[`.github/workflows/version-bump.yml`](.github/workflows/version-bump.yml): it
bumps the version in `pyproject.toml` and `electron/package.json` (kept in sync)
and pushes a matching `vX.Y.Z` tag. The bump level comes from the merge commit
message:

- default → **patch** (`0.4.0` → `0.4.1`)
- contains `#minor` → **minor** (`0.4.0` → `0.5.0`)
- contains `#major` → **major** (`0.4.0` → `1.0.0`)

The bump commit is made with the built-in `GITHUB_TOKEN`, whose pushes don't
retrigger workflows, so it can't loop. Because of that same rule, the tag it
pushes does **not** trigger the container build in `docker-publish.yml`; the
merge that preceded the bump already built and pushed the `latest`/`main`/`sha`
images. To also auto-build semver-tagged images (`0.5.0`, `0.5`) from the bot
tag, run the bump workflow's checkout/push with a Personal Access Token secret
instead of `GITHUB_TOKEN`.

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
