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

## resort function — ✅ shipped (2026-08-25)

Implemented as `resortEmail.py` + `POST /api/resort` + the **Resort Now…**
button on `/rules`. See CLAUDE.md ("`resortEmail.py` pipeline") for the design
and `tests/test_resort.py` (29 tests) / the `/api/resort` block in
`tests/test_app.py` for the safety invariants each change must keep.

Delivered:
- `resortEmail.py` — `build_index()` → `plan_resort()` → `report_from_plan()` →
  `apply_plan()`; CLI is dry-run by default (`--apply`, `--limit N`).
- Additions run before removals; a copy is only expunged once the message is
  confirmed present in a matching label. A sender with **no** matching rule is
  left alone entirely, and messages without a `Message-ID` are read-only.
- Every add re-checks the target with `UID SEARCH HEADER Message-ID` before
  COPY, so a truncated scan can never duplicate a message.
- `POST /api/resort` (`?dryrun=1` → report, else apply), serialized by
  `_resort_lock`, invalidating the inbox-count and folder caches after an apply.
- `/rules` "Resort Now…" → preview modal → explicit **Apply Changes**.
- `RESORT_MAX_MESSAGES` setting (Config page, default 2000, `0` = unlimited)
  caps the messages examined per run, newest first.

Deliberately not built: a background-job/progress variant of the endpoint (the
message cap keeps a run short enough to be synchronous), and de-duplicating
two copies of the same message *within* one folder.

**General Ideas**
- Include a link to open the full email in a model dialog whenever displaying an email summary
- Set a custom sort order for mailmatrix categories to be used when displaying filled messages in summaries
- ~~Add a "Resort now" button to the `/rules` view that shows the dry-run report first, then an "Apply" confirmation~~ — done (2026-08-25)
- Add a setting to specify the time zone for sorting messages
- ~~Add a setting to specify the maximum number of messages to process in a single resort operation~~ — done (2026-08-25), `RESORT_MAX_MESSAGES` on `/config`
- Add a setting to schedule summaries to run at regular intervals
- add a view that shows all email from the past 24 hours sorted by category using the custom order
- add a view that shows all email from the past 7 days sorted by category using the custom order
- add a view that shows all email from the past 30 days sorted by category using the custom order