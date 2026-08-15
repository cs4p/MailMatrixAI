# MailMatrixAI — Roadmap

Detailed implementation plans for planned features. Each plan is written against
the current codebase; file/line references were accurate at authoring time —
re-verify anchors before executing.

**Architecture facts these plans rely on:**
- No provider abstraction exists — everything is hardcoded IMAP + SMTP with plain
  `login()` auth (`connect_to_imap` at `commonFunctions.py:260`, `send_smtp` at
  `commonFunctions.py:1267`). Fastmail is the reference provider.
- Credentials live in one macOS Keychain JSON blob via
  `get_credential`/`set_credential` (`commonFunctions.py:141`/`149`); the key
  whitelist is duplicated in `app.py:108` (`_CREDENTIAL_KEYS`) and
  `commonFunctions.py:33` (`_LEGACY_CREDENTIAL_KEYS`) and must stay in sync.
  `set_credential` mirrors each value into `os.environ`, which is how CLI
  subprocesses inherit creds.
- **Folders ARE labels.** This is plain IMAP, not Gmail's label model. A message
  "has" label `MailMatrixCategories/Work` iff a physical copy sits in that
  folder; multi-label = multiple independent copies. Filing = COPY into the
  folder + `\Deleted` + EXPUNGE from source.

---

## Make this work with gmail
- Add support for gmail's API.
- Add support for gmail's OAuth.

### Plan — OAuth over IMAP (XOAUTH2)

Decision: layer OAuth2 (XOAUTH2) onto the **existing IMAP/SMTP path** rather than
adopting the Gmail REST API. Gmail app passwords are disabled, so OAuth is
mandatory; but Gmail's IMAP (`imap.gmail.com:993`) and SMTP
(`smtp.gmail.com:465`) both support `AUTHENTICATE XOAUTH2` / `AUTH XOAUTH2`, so
the entire downstream pipeline (`sort_inbox`, `emailSummary`, the `/mail` client)
stays unchanged. Only the two auth chokepoints and credential storage change.

1. **Credential model.** Add keys to the whitelist in *both* `app.py:108` and the
   sync'd set in `commonFunctions.py:33`:
   - `MAIL_PROVIDER` (`fastmail` | `gmail` | `outlook`; default `fastmail`)
   - `AUTH_METHOD` (`password` | `oauth`; default `password`)
   - `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REFRESH_TOKEN`,
     `OAUTH_ACCESS_TOKEN`, `OAUTH_TOKEN_EXPIRY`

   Keep everything in the single JSON blob — no per-account namespacing for v1
   (single account). Update `.env.example`.

2. **New helper `oauth.py`** (or a section in `commonFunctions.py`):
   - `build_xoauth2_string(user, access_token) -> bytes` — the standard SASL
     string `user=<u>\x01auth=Bearer <tok>\x01\x01`.
   - `refresh_access_token(client_id, client_secret, refresh_token) -> (token, expiry)`
     — POST to Google's token endpoint (`https://oauth2.googleapis.com/token`);
     store the fresh token/expiry back via `set_credential`. Use `urllib`/an
     existing dep, or add `google-auth` to `pyproject.toml` if preferred.
   - `ensure_fresh_token()` — refresh when `OAUTH_TOKEN_EXPIRY` is past/near.

3. **Branch the auth chokepoints on `AUTH_METHOD`:**
   - `connect_to_imap()` (`commonFunctions.py:260`): when `oauth`, call
     `imap.authenticate('XOAUTH2', lambda _=None: build_xoauth2_string(user, token))`
     instead of `imap.login()`. Signature grows an optional `auth_method` /
     `access_token` param (default `password`, positional-compatible).
   - **Centralize connection setup.** ~9 call sites re-read `IMAP_*` inline
     (`app.py:188`/`374`/`525`/`777`, `sortEmail.py:133`, `emailRulesInit.py:150`,
     `emailSummary.py:669`/`879`). Add an `open_imap_from_credentials()` factory
     that reads provider + auth-method + creds and returns a connected
     `IMAP4_SSL`; migrate all call sites to it (the `_open_mail_imap` helper at
     `app.py:777` is the model).
   - `send_smtp()` (`commonFunctions.py:1267`): when `oauth`, replace
     `smtp.login()` with `smtp.auth('XOAUTH2', lambda: build_xoauth2_string(...))`.

4. **One-time OAuth consent flow** (to obtain the refresh token), reachable from
   `/config`:
   - `GET /api/oauth/gmail/start` → builds Google's auth URL (scope
     `https://mail.google.com/`) and redirects.
   - `GET /api/oauth/gmail/callback` → exchanges `code` for refresh/access
     tokens, stores them via `set_credential`, sets `MAIL_PROVIDER=gmail`,
     `AUTH_METHOD=oauth`. Use a loopback redirect URI
     (`http://127.0.0.1:PORT/...`) matching a desktop-app OAuth client. Document
     that the user creates a Google Cloud OAuth client and pastes client
     id/secret into `/config`.

5. **Config UI** (`templates/config.html`): add a provider `<select>`, an
   auth-method toggle that swaps password vs OAuth fields, and a "Connect Google
   account" button. Update the `saveConfig()` key list (`config.html:75`) and the
   Fastmail-specific hint text.

6. **Tests** (`tests/test_common.py`, `tests/test_app.py`):
   - `build_xoauth2_string` exact-bytes test.
   - `connect_to_imap` OAuth branch: patch `imaplib.IMAP4_SSL`, assert
     `imap.authenticate('XOAUTH2', ...)` is called, not `login` (mirror
     `test_common.py:247`).
   - Token refresh: mock the token endpoint, assert `set_credential` writes the
     new token/expiry (autouse fake keychain covers storage).
   - `/api/oauth/gmail/callback` happy path with a mocked token exchange.

**Gmail nuances:** Gmail exposes `[Gmail]/All Mail`, `[Gmail]/Sent Mail`, etc.;
a COPY into a folder adds a label (not a physical move), and `\Deleted`+EXPUNGE on
INBOX only removes the INBOX label. The existing `sort_inbox` COPY→delete flow
still yields the correct end state on Gmail — note this in code comments.
Special-use detection in `list_folders` (`commonFunctions.py:1025`) keys off
`\Sent`/`\Trash` flags that Gmail advertises, so `_append_to_sent` still finds
Sent.

---

## Add support for other email providers.
- Add support for outlook.com.

### Plan — Outlook.com (reuses the Gmail OAuth seam)

Depends on the Gmail item landing first. Outlook.com/Office365 uses IMAP
`outlook.office365.com:993` + SMTP `smtp.office365.com:587` (STARTTLS) and
**requires OAuth2 XOAUTH2** (Microsoft disabled basic auth for personal and most
tenant accounts). Only the OAuth endpoints and default hostnames differ from
Gmail.

1. **Provider defaults table** (e.g. new `providers.py`) mapping `MAIL_PROVIDER`
   → default IMAP/SMTP host+port + OAuth endpoints:
   - `fastmail`: `imap.fastmail.com:993` / `smtp.fastmail.com:465`, `password`.
   - `gmail`: `imap.gmail.com:993` / `smtp.gmail.com:465`, Google OAuth endpoints,
     scope `https://mail.google.com/`.
   - `outlook`: `outlook.office365.com:993` / `smtp.office365.com:587` (STARTTLS —
     `send_smtp` already picks STARTTLS when port != 465, so 587 works
     unchanged), Microsoft identity endpoints
     (`login.microsoftonline.com/common/oauth2/v2.0/{authorize,token}`), scope
     `https://outlook.office.com/IMAP.AccessAsUser.All offline_access
     https://outlook.office.com/SMTP.Send`.

   Selecting a provider in `/config` pre-fills host/port (user can override).

2. **Generalize the OAuth flow** to be provider-parameterized:
   `/api/oauth/<provider>/start` and `/callback` read endpoints/scope from the
   provider table; `refresh_access_token` takes the token endpoint as a param.
   Microsoft's XOAUTH2 SASL string format is identical to Google's.

3. **Tests:** provider-table lookups; Outlook STARTTLS path in `send_smtp`
   (assert `starttls()` for port 587 — extend the existing `send_smtp` tests);
   OAuth callback with Microsoft endpoints mocked; an Outlook case in any
   provider-selection test.

**Note:** No changes to `sort_inbox`, `emailRulesInit`, `emailSummary`, or the
mail client — all stay provider-agnostic IMAP once auth succeeds. The
`MailMatrixCategories/` folder convention works identically on Outlook.

---

## Full email client
- Add support to read and write emails.

### Plan — close the remaining gaps

The `/mail` client already implements read (list + full message + HTML/text +
attachment download), compose, reply, reply-all, forward, move, folder-create,
send (with threading + Sent-copy), unread counts, and mark-as-read. Prioritized
remaining work: outbound attachments + drafts, cross-folder search, and multiple
sending identities. (Delete/flags deferred — see below.)

**a. Outbound attachments + drafts**
- **Compose modal** (`templates/mail.html:33`): add a multi-file `<input>` and a
  "Save draft" button.
- **`POST /api/mail/send`** (`app.py:1031`): accept `multipart/form-data`
  (currently JSON). After `msg.set_content(text)` (`app.py:1084`), loop
  attachments and `msg.add_attachment(data, maintype, subtype, filename=...)`.
  Guard total size + sanitize filenames.
- **Forward re-attachment** (`mail.html:521`): when forwarding, re-fetch the
  source message's attachments via `get_attachment` and include them (currently
  only text is quoted).
- **New `POST /api/mail/draft`**: build the same `EmailMessage` and IMAP APPEND it
  to the special-use Drafts folder (mirror `_append_to_sent` at `app.py:1010`,
  keyed on the `\Drafts` special-use flag from `list_folders`). Best-effort, like
  Sent.
- **Tests** (`tests/test_app.py`): send-with-attachment builds a multipart
  message and calls `send_smtp` with the attachment present; forward re-attaches;
  draft APPENDs to Drafts. Reuse `_combined_fetch` and the existing send
  scaffolding.

**b. Cross-folder / in-folder search**
- **New `GET /api/mail/search?folder=&q=&scope=`**: run server-side
  `imap.uid('SEARCH', 'TEXT', q)` (or `OR SUBJECT ... FROM ...`), then feed the
  hit UIDs through the same `fetch_many(..., use_uid=True)` header path used by
  `/api/mail/messages` (`app.py:854`) so result rows are shape-identical.
  `scope=all` iterates selectable folders from the cached folder tree. Sanitize
  `q` against IMAP injection (reject quotes/CRLF, mirror `validate_folder`).
- **UI**: a search box above the message list; render results in the existing
  list component.
- **Tests**: mock `imap.uid('SEARCH', ...)` returning UID sets; assert the header
  fetch + JSON shape matches `/api/mail/messages`.

**c. Multiple sending identities**
- **Credential/config**: add `MAIL_IDENTITIES` (JSON list of `{name, address}`)
  to the whitelist and a `/config` editor. `From` is currently hardcoded to
  `IMAP_USERNAME` (`app.py:1069`).
- **`POST /api/mail/send`**: accept an optional `from` field; validate it is one
  of the configured identities (reject arbitrary values — spoofing / header
  injection). Default to the primary identity / `IMAP_USERNAME`.
- **Compose modal**: a `From` `<select>` populated from `MAIL_IDENTITIES`.
- **Tests**: send with a valid identity sets the right `From`; an unlisted
  identity is rejected.

**Deferred (record, don't build yet):** permanent delete / empty-trash endpoint,
flag/star, mark-unread, folder rename/delete, server-side conversation threading.

---

## resort function
- Add a resort function to the rules view that will go through all existing email and make sure everything is labeled correctly, removing any extra labels in the MailMatrix category but leaving other labels alone. This is intended as a once in a while cleanup routine to make sure the labels are correct.

### Plan — full reconcile against emailRules.json

Make every `MailMatrixCategories/*` folder exactly match the rules: **remove**
copies whose sender no longer matches that label, and **add** copies to every
MailMatrix label the sender now matches. Never touch INBOX or any non-MailMatrix
folder. Because folders are independent physical copies, "leave other labels
alone" is automatic — we only ever operate inside the `MailMatrixCategories/`
prefix.

New file `resortEmail.py` (sibling of `sortEmail.py`), reusing `load_rules` +
`find_matching_labels` (`sortEmail.py:23`/`40`).

**Algorithm:**
1. Load rules → `email_to_labels`, `domain_to_labels` (`sortEmail.load_rules`).
2. `labels = get_all_labels(imap, "MailMatrixCategories")`
   (`commonFunctions.py:283`).
3. **Build a cross-folder message index** (to dedupe the additive half by
   identity). For each label folder L:
   - `uids = uid_search_all(imap, L)` (`commonFunctions.py:1062`)
   - `fetch_many(imap, uids, '(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID)])', use_uid=True)`
     (`commonFunctions.py:428`)
   - Record per message: `msgid`, `from_addr` (`extract_email_address`), folder
     L, UID. Maintain `present[msgid] = {folders it lives in}` and
     `sender[msgid] = from_addr`.
4. **Removal pass:** for message m in folder L, if
   `L not in find_matching_labels(sender[m])` → remove that copy: select L
   writable, `UID STORE <uid> +FLAGS \Deleted`, `UID EXPUNGE` (pattern from
   `move_message_uid`, `commonFunctions.py:1253`). Only the wrong-folder copy is
   expunged; copies elsewhere are untouched.
5. **Additive pass:** for each unique message m, for each label in
   `find_matching_labels(sender[m])` not in `present[m]` → COPY m into that
   folder (source from any folder m currently lives in; `ensure_mailbox` first,
   `commonFunctions.py:1199`). Never COPY where it would duplicate (check
   `present[m]`).
6. **Domain-rule safety:** always match via `find_matching_labels` (address ∪
   domain), never the address dict alone — a message can legitimately belong to a
   label by domain even if its exact sender isn't listed. Prevents the removal
   pass from deleting domain-matched mail.

**Safety / UX:**
- **Dry-run first.** `resort_inbox(..., apply=False)` returns a report
  (`{label: {"to_remove": [...], "to_add": [...]}}`) with no writes; the `/rules`
  view shows it and the user confirms before an `apply=True` run (mirrors the
  report-then-act shape of `/api/inbox-analyze`).
- **Never delete unless certain** — same invariant as `sort_inbox`
  (`sortEmail.py:107`) and `move_message_uid`: a copy is expunged only when the
  sender definitively doesn't match that label.
- Confine every write to the `MailMatrixCategories/` prefix; assert each target
  passes `validate_label` (`commonFunctions.py:197`).

**Wiring:**
- **`POST /api/resort`** in `app.py`, mirroring `/api/sort` (`app.py:293`):
  `?dryrun=1` returns the report; without it, applies. Guard with a lock like the
  existing `_sort_lock`. Invalidate the inbox-count / folder caches after an
  apply.
- **`/rules` view**: a "Resort now" button that shows the dry-run report first,
  then an "Apply" confirmation.

**Tests** (`tests/test_resort.py`, patterned on `tests/test_sort.py`):
- Correctly-filed message → no removal, no add.
- Wrong-folder copy → `UID STORE +FLAGS \Deleted` + `UID EXPUNGE` on that folder
  only; other folders untouched.
- Missing label → `UID COPY` into the new folder; no COPY when already present
  (dedupe by Message-ID).
- Domain-matched sender not in address list → NOT removed.
- Failed COPY never triggers a delete (mirror `test_sort.py:167`).
- Dry-run mode performs zero `store`/`copy`/`expunge` calls, returns the report.
- Use the batched-fetch mock style from `test_sort.py:190` / `_combined_fetch`
  (`test_app.py:715`), extended to emit `Message-ID` headers.

---

## Summaries page — refresh controls
- Add a refresh icon to each summary card that regenerates that day's report.
- Add a page button to refresh **all** summaries that still have unfiled emails.

### Plan — reuse the existing per-date generation

Both features build on machinery that already exists: `/api/generate-summary`
(`app.py:311`) already accepts a `{"date": "YYYY-MM-DD"}` body and runs
`emailSummary.py --no-serve <date>`, overwriting `email_summary_<date>.html` (+
its `.json` sidecar). `summary_files()` (`commonFunctions.py:627`) already
returns per-summary `date`, `unfiled`, `processed`, `need_attention`, and
`filed` from the sidecar. So the per-card refresh needs **no new backend**, and
"refresh all unfiled" only needs a thin endpoint to enumerate the dates.

**a. Per-summary refresh icon**
- **Template** (`templates/summaries.html`): the card is currently a single
  `<a href="/summaries/{{ f.filename }}">`. Add a refresh control (↻) per card
  without breaking navigation — either move the `<a>` to wrap only the card body
  and place the button as a sibling, or keep the anchor and give the button
  `event.preventDefault(); event.stopPropagation()`. Render it with
  `data-date="{{ f.date }}"`.
- **JS** (inline `{% block %}` script in `summaries.html`, or `static/app.js`):
  on click, POST `{date}` to `/api/generate-summary` with the
  `X-Requested-With: XMLHttpRequest` header the app expects; show a spinner on
  the icon; on success update that card's stat counts in place and refresh the
  `generated_at` label. `/api/generate-summary` currently returns
  `{ok, filename}` (`app.py:330`) — extend it to also return the fresh sidecar
  meta (`processed`/`need_attention`/`unfiled`/`filed`/`generated_at`) so the
  card updates without a full page reload; fall back to `location.reload()`.

**b. "Refresh unfiled" button**
- **New `GET /api/summaries/unfiled-dates`** (`app.py`): call
  `summary_files(SUMMARY_DIR)`, return the `date`s where `unfiled` is truthy
  (newest first). Trivial, fully covered by the `client` fixture which already
  patches `SUMMARY_DIR` (`test_app.py:33`).
- **UI**: a "Refresh unfiled" button in the `.page-header`. On click, fetch the
  unfiled dates, then regenerate them **sequentially** (each
  `/api/generate-summary` call is a synchronous subprocess up to 300s and hits
  IMAP + Anthropic — never fan out in parallel), showing progress (`n of N`) and
  updating each card as it completes. Disable the button while running.
- **Scale note:** if the unfiled set is ever large enough that sequential
  regeneration risks a slow page, promote this to the existing background-job
  pattern — a daemon thread tracked in `_inbox_jobs` with a poll endpoint,
  exactly like `/api/inbox-analyze/*` (`_get_job`, `app.py`). Start simple
  (client-driven sequential loop); adopt the job pattern only if needed.

**Tests** (`tests/test_app.py`):
- `/api/summaries/unfiled-dates`: seed the tmp `SUMMARY_DIR` with a few
  `email_summary_<date>.json` sidecars (some `unfiled > 0`, some `0`, one with
  no sidecar) and assert only the unfiled dates come back, newest first.
- `/api/generate-summary` with a specific `date` already exercises the
  subprocess path (patch/stub `subprocess.run`); extend it to assert the
  response now echoes the refreshed sidecar meta.
- Summaries page renders a refresh control per card and the header button.
