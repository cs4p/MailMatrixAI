# Test patterns

- **IMAP mocks**: every IMAP method must return a 2-tuple `("OK", data)` — the `imap_call` wrapper unpacks this. Use `MagicMock()` with explicit `return_value` assignments per method.
- **Batched FETCH mocks**: `imap.fetch` receives a *comma-joined ID set* (e.g. `b"1,2,3"`), not one call per message (`commonFunctions.fetch_many`). A side effect must emit a multi-message response: per message, a header tuple whose prefix starts with the sequence number (`b"1 (BODY[HEADER...] {n}"`), optionally a text continuation tuple (no sequence number; the server echoes `<0.2000>` back as `<0>`), then a bare `b")"`. See `_combined_fetch` in `test_app.py`.
- **Flask tests**: use `client` fixture from `test_app.py` which patches `RULES_PATH`, `SUMMARY_DIR`, and `ENV_PATH` to tmp paths so tests never touch real files (it also resets the module-level inbox-count cache).
- **Keychain**: an autouse fixture in `conftest.py` fakes `keyring` and invalidates the credential-blob cache around every test — never rely on real Keychain state.
- **Anthropic mock**: `client.messages.stream(...)` is a context manager — mock via `mock_cm.__enter__.return_value = mock_stream`.
