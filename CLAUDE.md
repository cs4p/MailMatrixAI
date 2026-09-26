# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MailMatrixAI is an email management pipeline with a Flask web UI and four CLI scripts that operate via IMAP. It targets **Fastmail** (Cyrus IMAP) with an app-specific password — historical references to "Gmail" predate that. Gmail support would require OAuth (app passwords are disabled there) and is future work; do not assume Gmail-specific behavior (`[Gmail]/` namespace, All Mail semantics, SMTP auto-saving Sent).

1. **`emailRulesInit.py`** — crawls all `MailMatrixCategories/*` labels, extracts sender addresses from every message, and writes `emailRules.json`
2. **`sortEmail.py`** — reads `emailRules.json` and files INBOX messages into their matching labels (then removes them from INBOX)
3. **`emailSummary.py`** — generates a daily markdown report: action-required messages, unmatched INBOX emails with Claude-suggested labels, and a list of what was filed where
4. **`resortEmail.py`** — occasional cleanup: reconciles every `MailMatrixCategories/*` folder against `emailRules.json` (adds missing copies, removes ones the sender no longer matches). Dry run unless `--apply`

On top of these, `app.py` serves a full **Mail client** at `/mail` (browse folders, read/compose/reply/forward, drag-and-drop filing) via the UID-based `/api/mail/*` endpoints — see below.

## Testing

Always add tests when adding new code. Run the suite with:

```bash
python -m pytest
```

Tests live in `tests/`; mocking patterns for IMAP, batched FETCH, Flask, Keychain and Anthropic are in `tests/CLAUDE.md`.
- New Flask routes → tests in `tests/test_app.py`. New script functions → tests in the matching `tests/test_<script>.py`.

## Configuration

Credentials live in the **macOS Keychain** as a single JSON blob (service `MailMatrixAI`, account `credentials`) accessed via `commonFunctions.get_credential()`/`set_credential()` (the decoded blob is cached in-process). `get_credential` falls back to `os.environ` for keys missing from the blob. A `.env` file (loaded via `python-dotenv`) still works as a seed: on `app.py` startup, `_migrate_env_to_keychain()` copies these keys into the Keychain once:

```
IMAP_SERVER=imap.fastmail.com
IMAP_PORT=993
IMAP_USERNAME=your_email@example.com
IMAP_PASSWORD=your_app_password
SMTP_SERVER=smtp.fastmail.com
SMTP_PORT=465
ANTHROPIC_API_KEY=sk-ant-...
```

`RESORT_MAX_MESSAGES` (default `resortEmail.DEFAULT_MAX_MESSAGES` = 2000, `0` = unlimited) rides in the same blob — a setting, not a secret, but it goes through `set_credential` so the CLI inherits it via `os.environ` like everything else.

`emailRules.json` and `.env` are gitignored.

## Container images

**Every published image must carry a semantic version tag** — never ship a
build that is only reachable as `latest` or `sha-…`. Automated update tools
(Renovate) can only detect a new release from an immutable `X.Y.Z` tag.

- Release flow is automatic: every push to `main` runs `version-bump.yml`, which
  bumps `pyproject.toml` + `electron/package.json` (patch; `#minor`/`#major` in
  the head commit message), commits with `[skip version]`, and pushes a
  `vX.Y.Z` tag → `docker-publish.yml` publishes `X.Y.Z` and `X.Y` to
  `ghcr.io/cs4p/mailmatrixai` (`docker/metadata-action` strips the leading `v`).
  Put `[skip version]` in a commit message to push to `main` without a release.
- `k8s/deployment.yaml` pins the exact `X.Y.Z` tag, not `latest`.
  `renovate.json` points Renovate's `kubernetes` manager at `k8s/**/*.yaml` —
  that manager has no default file matching, so removing the config silently
  disables bump PRs. Renovate auto-merges those pin bumps.
- **Never remove the `k8s/**` `paths-ignore` from `version-bump.yml` /
  `docker-publish.yml`.** Without it, each auto-merged pin bump cuts a new
  release, which Renovate pins again — an endless release loop.
- Version-bump commits touch `pyproject.toml` and `electron/package.json` only.
  The pin advances **after** CI publishes the image (via the Renovate PR) —
  bumping it in the same commit would point at a tag that does not exist yet.
- **The live lab deployment is not `k8s/` here.** Argo CD deploys it from
  `cs4p/homelab` (`argocd/manifests/mailmatrixai/`, pinned
  `X.Y.Z@sha256:<index digest>`, `selfHeal` on — `kubectl apply` from this repo
  gets reverted). Deploying a release = bumping that pin in homelab.

## Architecture

### `app.py` — Flask web UI

**Mail-client conventions:** the `/api/mail/*` surface is **UID-based** (`imap.uid('SEARCH'|'FETCH'|'COPY'|'STORE'|'EXPUNGE', …)`) so message references survive expunges — do not mix in sequence-number operations there. Folder params are gated by `validate_folder` (relaxed `validate_label` that still rejects quotes/CRLF/traversal but allows any folder, not just `MailMatrixCategories/*`); `uid`/`part` must match `^\d+$`. The client passes back the **raw wire folder name** verbatim (`list_folders` returns `name` = raw, `display` = `decode_modified_utf7(name)`); there is no encode path, so folder *creation* is ASCII-only. HTML bodies render in a `<iframe sandbox>` with a CSP that blocks images until the user opts in — never render server-fetched HTML same-origin. Since this is Fastmail (not Gmail), SMTP-sent mail is **not** auto-saved: `_append_to_sent()` does an IMAP APPEND to the `\Sent` folder (best-effort; a failed APPEND is a warning, never a send failure).

POST bodies are read through `_json_body()` (never `request.get_json` directly) so malformed/non-object bodies degrade to `{}` and the handlers' own validation runs instead of a 500. Inbox-analysis jobs (`/api/inbox-analyze/*`) run in daemon threads tracked in `_inbox_jobs`; always look jobs up via `_get_job()` (takes the lock).

### `commonFunctions.py` — shared utilities

All scripts import from here. All multi-message fetch paths go through `fetch_many` — never fetch in a per-message loop. Call `setup_logging` at the top of each `main()`.

### Label conventions

The sorting pipeline only touches labels under `MailMatrixCategories/` (e.g. `MailMatrixCategories/Work`); the Mail client browses the whole folder tree. `/` is the IMAP hierarchy delimiter (from each folder's LIST response). Folder names must be quoted in IMAP commands: `imap.select('"MailMatrixCategories/Work"')`.

### `emailRulesInit.py` pipeline

`emailDomains` in the schema is always written as `[]` — domain inference is not implemented.

### `sortEmail.py` pipeline

`load_rules()` builds two lookup dicts (`email_to_labels`, `domain_to_labels`) from `emailRules.json`. `sort_inbox()` batch-fetches only `BODY[HEADER.FIELDS (FROM)]` via `fetch_many`, then `imap.copy()` to each matching label and `\Deleted` + `expunge` to remove from INBOX. **A message is only flagged `\Deleted` after every COPY returned OK** — a failed copy must never destroy the original (same rule in `accept_filing` and `move_imap_messages`). Uses the lambda default-arg pattern to capture loop variables: `lambda mid=msg_id: imap.copy(mid, ...)`.

### `resortEmail.py` pipeline

Reconciles the folders against the rules, in one direction each way: **remove**
copies whose sender no longer matches that folder, **add** copies to every
MailMatrix label the sender now matches. `build_index()` (UID SEARCH +
`fetch_many(..., use_uid=True)` for `FROM/SUBJECT/DATE/MESSAGE-ID`) →
`plan_resort()` → `report_from_plan()` → optionally `apply_plan()`.

Unlike `/api/sort`, this runs **in-process** (the caller needs the report back),
so it is bounded by `resort_max_messages()` and serialized by `_resort_lock`.

Non-negotiable safety rules (tests in `tests/test_resort.py` pin each one):
- Additions run **before** removals, and a copy is expunged only if the message
  is still present in a matching label afterwards — a failed COPY can never
  leave the message deleted everywhere.
- A sender matching **no** rule is skipped entirely: deleting a rule must never
  delete mail.
- Messages with no `Message-ID` are read-only (they can't be deduped across
  folders, so neither pass is safe).
- Every add re-checks the target with `UID SEARCH HEADER Message-ID` before
  COPY, so a truncated index can't create duplicates.
- Only `MailMatrixCategories/*` folders are ever read or written; every target
  is re-checked with `validate_label` at write time.

### `emailSummary.py` pipeline

The HTML report has **Accept** buttons on unmatched email cards. Clicking one POSTs `{from_addr, label}` to `/accept` on the local server, which reconnects to IMAP, moves all INBOX messages from that sender to the label, and patches `emailRules.json`. The server runs until Ctrl+C.

Claude response is parsed as JSON (`action_required`, `filing_suggestions` arrays indexed by email position). Falls back gracefully if Claude doesn't return valid JSON.
