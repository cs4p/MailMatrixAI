# MailMatrix AI — Electron desktop app

A thin desktop wrapper around the Flask web UI. On launch it:

1. Picks a free localhost port.
2. Spawns the Python backend — `../.venv/bin/python ../app.py` — with
   `MAILMATRIX_PORT`/`MAILMATRIX_HOST` set.
3. Waits for the server to answer, then opens it in an app window.
4. Terminates the Python process when the window closes.

## Running

Prerequisites: Node 18+, and the repo's Python venv set up:

```bash
# from the repo root, once:
python3 -m venv .venv && .venv/bin/pip install -e .

# then:
cd electron
npm install
npm start
```

If the backend fails to start, the window shows the error and the tail of the
Python stderr; the full log is in `app.log` at the repo root.

## Notes

- **Keychain prompt** — credentials live in the macOS Keychain (via Python
  `keyring`). The first access from a new launch context may trigger the
  standard macOS Keychain permission prompt; allow it for `python`.
- **No network assets** — the UI is fully self-contained (no CDNs/fonts), so
  the window works offline. IMAP and the Anthropic API still need connectivity.

## Packaging (`npm run dist`)

`npm run dist` builds an unsigned `.app` into `electron/dist/` (mac `dir`
target — producing a signed dmg/zip requires an Apple signing identity).

The packaged app is still a wrapper, not a self-contained bundle: `app.py`
re-invokes `sys.executable` to run `sortEmail.py`/`emailSummary.py` as
subprocesses, so a real Python interpreter and the repo checkout are required
at runtime. Launch the packaged app with the repo location in its environment:

```bash
MAILMATRIX_REPO=/path/to/MailMatrixAI open "dist/mac/MailMatrix AI.app"
```

Bundling a frozen Python (PyInstaller-style) is intentionally out of scope for
now.
