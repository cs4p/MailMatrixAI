import json
from unittest.mock import MagicMock

import pytest


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
