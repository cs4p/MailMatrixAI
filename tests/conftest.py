import json
from unittest.mock import MagicMock

import keyring.errors
import pytest

import commonFunctions


@pytest.fixture(autouse=True)
def _fake_keychain(monkeypatch):
    """Never let a test touch the real macOS Keychain.

    Incident (2026-07-16): only keyring.get_password was mocked here before
    (always returning None) while set_password/delete_password hit the real
    Keychain. Under the old one-item-per-key credential scheme that only
    clobbered whichever single key a test posted; under the consolidated
    single-JSON-blob scheme (commonFunctions.get_credential/set_credential),
    every write does a read-modify-write of the *whole* blob — since the
    mocked read always saw "nothing", every test write wiped out the real
    stored credentials entirely. This in-memory fake keychain makes
    get/set/delete behave consistently against a per-test store instead,
    so no test can reach real system Keychain storage, ever.
    """
    store: dict[tuple[str, str], str] = {}

    def _get(service, username):
        return store.get((service, username))

    def _set(service, username, password):
        store[(service, username)] = password

    def _delete(service, username):
        if (service, username) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(service, username)]

    monkeypatch.setattr("keyring.get_password", _get)
    monkeypatch.setattr("keyring.set_password", _set)
    monkeypatch.setattr("keyring.delete_password", _delete)

    # The credential blob is cached at module level — reset it around each
    # test so one test's fake-keychain contents can't leak into the next.
    commonFunctions._invalidate_credentials_cache()
    yield
    commonFunctions._invalidate_credentials_cache()


@pytest.fixture
def mock_imap():
    """Pre-configured IMAP mock with sensible defaults for all method calls."""
    imap = MagicMock()
    imap.login.return_value = ("OK", [b"LOGIN OK"])
    imap.select.return_value = ("OK", [b"5"])
    imap.list.return_value = ("OK", [])
    imap.search.return_value = ("OK", [b""])
    imap.fetch.return_value = ("OK", [(b"", b""), b")"])
    imap.copy.return_value = ("OK", None)
    imap.store.return_value = ("OK", [None])
    imap.expunge.return_value = ("OK", None)
    imap.logout.return_value = ("BYE", [b"Logging out"])
    return imap


@pytest.fixture
def rules_data():
    return {
        "labels": [
            {
                "labelName": "MailMatrixCategories/Work",
                "emailAddresses": ["boss@work.com", "alice@work.com"],
                "emailDomains": [],
            },
            {
                "labelName": "MailMatrixCategories/Shopping",
                "emailAddresses": ["orders@amazon.com"],
                "emailDomains": ["shop.example.com"],
            },
        ]
    }


@pytest.fixture
def rules_file(tmp_path, rules_data):
    f = tmp_path / "emailRules.json"
    f.write_text(json.dumps(rules_data))
    return f
