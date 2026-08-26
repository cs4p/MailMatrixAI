import pytest

import resortEmail
from resortEmail import (
    DEFAULT_MAX_MESSAGES,
    build_index,
    plan_resort,
    report_from_plan,
    resort,
    resort_max_messages,
)

WORK = "MailMatrixCategories/Work"
SHOPPING = "MailMatrixCategories/Shopping"
VIP = "MailMatrixCategories/VIP"

EMAIL_RULES = {"boss@work.com": [WORK]}
DOMAIN_RULES = {"shop.example.com": [SHOPPING]}


# ── mock helpers ──────────────────────────────────────────────────────────────

def _list_response(labels):
    return ("OK", [b'(\\HasNoChildren) "/" "%s"' % lbl.encode() for lbl in labels])


def _uid_fetch_response(messages):
    """Emulate a batched UID FETCH: per message a header tuple carrying the UID
    in its prefix, then a bare b')'. `messages` is [(uid, from, msgid), ...]."""
    data = []
    for uid, from_addr, msgid in messages:
        payload = (f"From: {from_addr}\r\nSubject: Hello\r\n"
                   f"Date: Fri, 27 Jun 2026 10:00:00 +0000\r\n"
                   + (f"Message-ID: {msgid}\r\n" if msgid else "")).encode()
        prefix = b"1 (UID %s BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] {%d}" % (
            uid, len(payload))
        data.append((prefix, payload))
        data.append(b")")
    return ("OK", data)


def make_fake_imap(mock_imap):
    """Wire a MagicMock for the UID surface resort uses: a per-folder mailbox
    map drives LIST/SEARCH/FETCH/COPY/STORE/EXPUNGE, and every write is
    recorded. Shared with tests/test_app.py's end-to-end /api/resort test.

    Set `imap.mailboxes` to {folder: [(uid, from_addr, msgid), ...]} before the
    call under test.
    """
    mock_imap.mailboxes = {}
    mock_imap.selected = None
    mock_imap.copies = []       # (source_folder, uid, target_folder)
    mock_imap.deleted = []      # (folder, uid)
    mock_imap.expunged = []     # (folder, uid_set)
    mock_imap.searched_msgids = []

    def _select(name, readonly=False):
        folder = name.strip('"')
        mock_imap.selected = folder
        if folder not in mock_imap.mailboxes:
            return ("NO", [b"No such mailbox"])
        return ("OK", [str(len(mock_imap.mailboxes[folder])).encode()])

    def _list(*args, **kwargs):
        return _list_response(sorted(mock_imap.mailboxes))

    def _uid(command, *args):
        cmd = command.upper()
        folder = mock_imap.selected
        rows = mock_imap.mailboxes.get(folder, [])
        if cmd == 'SEARCH':
            if len(args) >= 3 and str(args[1]).upper() == 'HEADER':
                msgid = args[3].strip('"') if len(args) > 3 else ""
                mock_imap.searched_msgids.append((folder, msgid))
                hits = [uid for uid, _, mid in rows if mid == msgid]
                return ("OK", [b" ".join(hits)])
            return ("OK", [b" ".join(uid for uid, _, _ in rows)])
        if cmd == 'FETCH':
            wanted = args[0].encode().split(b",")
            return _uid_fetch_response([r for r in rows if r[0] in wanted])
        if cmd == 'COPY':
            uid, target = args[0].encode(), args[1].strip('"')
            row = next((r for r in rows if r[0] == uid), None)
            mock_imap.copies.append((folder, args[0], target))
            if row:
                mock_imap.mailboxes.setdefault(target, []).append(row)
            return ("OK", [b"COPY completed"])
        if cmd == 'STORE':
            mock_imap.deleted.append((folder, args[0]))
            return ("OK", [None])
        if cmd == 'EXPUNGE':
            uid_set = args[0].split(",")
            mock_imap.expunged.append((folder, args[0]))
            mock_imap.mailboxes[folder] = [
                r for r in rows if r[0].decode() not in uid_set]
            return ("OK", [None])
        raise AssertionError(f"unexpected UID command {command}")

    def _create(name):
        mock_imap.mailboxes.setdefault(name.strip('"'), [])
        return ("OK", [b"CREATE completed"])

    mock_imap.select.side_effect = _select
    mock_imap.list.side_effect = _list
    mock_imap.uid.side_effect = _uid
    mock_imap.create.side_effect = _create
    return mock_imap


@pytest.fixture
def imap(mock_imap):
    return make_fake_imap(mock_imap)


def _run(imap, apply=False, email_rules=None, domain_rules=None, limit=0):
    return resort(
        imap,
        EMAIL_RULES if email_rules is None else email_rules,
        DOMAIN_RULES if domain_rules is None else domain_rules,
        apply=apply,
        max_messages=limit,
    )


# ── resort_max_messages ───────────────────────────────────────────────────────

def test_resort_max_messages_defaults():
    assert resort_max_messages() == DEFAULT_MAX_MESSAGES


def test_resort_max_messages_reads_setting(monkeypatch):
    monkeypatch.setenv("RESORT_MAX_MESSAGES", "50")
    assert resort_max_messages() == 50


def test_resort_max_messages_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("RESORT_MAX_MESSAGES", "0")
    assert resort_max_messages() == 0


def test_resort_max_messages_ignores_junk(monkeypatch):
    monkeypatch.setenv("RESORT_MAX_MESSAGES", "lots")
    assert resort_max_messages() == DEFAULT_MAX_MESSAGES


def test_resort_max_messages_override_wins(monkeypatch):
    monkeypatch.setenv("RESORT_MAX_MESSAGES", "50")
    assert resort_max_messages(10) == 10


# ── index ─────────────────────────────────────────────────────────────────────

def test_build_index_groups_copies_by_message_id(imap):
    imap.mailboxes = {
        WORK: [(b"1", "boss@work.com", "<a@x>")],
        VIP: [(b"7", "boss@work.com", "<a@x>")],
    }
    messages, scanned, truncated, errors = build_index(imap, [WORK, VIP])

    assert scanned == 2
    assert not truncated and not errors
    assert list(messages) == ["<a@x>"]
    assert messages["<a@x>"]["copies"] == {WORK: ["1"], VIP: ["7"]}
    assert messages["<a@x>"]["from_addr"] == "boss@work.com"


def test_build_index_limit_keeps_newest_and_flags_truncation(imap):
    imap.mailboxes = {WORK: [(b"1", "boss@work.com", "<a@x>"),
                             (b"2", "boss@work.com", "<b@x>"),
                             (b"3", "boss@work.com", "<c@x>")]}
    messages, scanned, truncated, _ = build_index(imap, [WORK], max_messages=2)

    assert scanned == 2
    assert truncated is True
    assert set(messages) == {"<b@x>", "<c@x>"}   # highest UIDs = newest


def test_build_index_skips_unselectable_folder(imap):
    imap.mailboxes = {WORK: [(b"1", "boss@work.com", "<a@x>")]}
    messages, scanned, _, errors = build_index(imap, [WORK, "MailMatrixCategories/Gone"])

    assert scanned == 1
    assert any("Gone" in e for e in errors)


# ── planning ──────────────────────────────────────────────────────────────────

def test_correctly_filed_message_is_left_alone(imap):
    imap.mailboxes = {WORK: [(b"1", "boss@work.com", "<a@x>")]}
    result = _run(imap)

    assert result["totals"]["to_add"] == 0
    assert result["totals"]["to_remove"] == 0
    assert result["labels"] == {}


def test_domain_matched_sender_is_not_removed(imap):
    # Sender isn't in emailAddresses, but the domain rule puts it here.
    imap.mailboxes = {SHOPPING: [(b"1", "deals@shop.example.com", "<a@x>")]}
    result = _run(imap)

    assert result["totals"]["to_remove"] == 0
    assert result["totals"]["to_add"] == 0


def test_sender_with_no_rule_at_all_is_never_removed(imap):
    imap.mailboxes = {WORK: [(b"1", "stranger@nowhere.com", "<a@x>")]}
    result = _run(imap, apply=True)

    assert result["totals"]["to_remove"] == 0
    assert result["totals"]["unmatched"] == 1
    assert imap.deleted == []


def test_message_without_message_id_is_read_only(imap):
    imap.mailboxes = {SHOPPING: [(b"1", "boss@work.com", "")]}
    result = _run(imap, apply=True)

    assert result["totals"]["no_msgid"] == 1
    assert result["totals"]["to_remove"] == 0
    assert result["totals"]["to_add"] == 0
    assert imap.deleted == []
    assert imap.copies == []


def test_duplicate_message_id_from_two_senders_is_left_alone(imap):
    # Same Message-ID, different senders — one of them would be filed by the
    # other's rules if they were treated as copies of one message.
    imap.mailboxes = {
        WORK: [(b"1", "boss@work.com", "<dup@x>")],
        SHOPPING: [(b"4", "stranger@nowhere.com", "<dup@x>")],
    }
    result = _run(imap, apply=True)

    assert result["totals"]["conflicts"] == 1
    assert result["totals"]["to_remove"] == 0
    assert result["totals"]["to_add"] == 0
    assert imap.deleted == [] and imap.copies == []


def test_report_groups_by_label(imap):
    imap.mailboxes = {SHOPPING: [(b"4", "boss@work.com", "<a@x>")]}
    result = _run(imap)

    assert result["labels"][SHOPPING]["to_remove"][0]["from_addr"] == "boss@work.com"
    assert result["labels"][SHOPPING]["to_remove"][0]["keep"] == ["Work"]
    assert result["labels"][WORK]["to_add"][0]["from_addr"] == "boss@work.com"


def test_report_from_plan_drops_entry_references():
    messages = {"<a@x>": {"msgid": "<a@x>", "from_addr": "boss@work.com",
                          "subject": "s", "date": "d", "copies": {SHOPPING: ["4"]}}}
    report = report_from_plan(plan_resort(messages, EMAIL_RULES, {}))
    for changes in report.values():
        for item in changes["to_add"] + changes["to_remove"]:
            assert "entry" not in item


# ── apply: removals ───────────────────────────────────────────────────────────

def test_wrong_folder_copy_is_expunged_from_that_folder_only(imap):
    imap.mailboxes = {
        WORK: [(b"1", "boss@work.com", "<a@x>")],
        SHOPPING: [(b"4", "boss@work.com", "<a@x>")],
    }
    result = _run(imap, apply=True)

    assert imap.deleted == [(SHOPPING, "4")]
    assert imap.expunged == [(SHOPPING, "4")]
    assert imap.mailboxes[WORK] == [(b"1", "boss@work.com", "<a@x>")]
    assert result["totals"]["removed"] == 1


def test_removal_waits_for_the_replacement_copy(imap):
    # Only copy lives in the wrong folder: it must be copied into Work first,
    # and only then removed from Shopping.
    imap.mailboxes = {SHOPPING: [(b"4", "boss@work.com", "<a@x>")]}
    result = _run(imap, apply=True)

    assert imap.copies == [(SHOPPING, "4", WORK)]
    assert imap.deleted == [(SHOPPING, "4")]
    assert result["totals"] == {**result["totals"], "added": 1, "removed": 1}
    assert imap.mailboxes[WORK] and imap.mailboxes[SHOPPING] == []


def test_failed_copy_never_triggers_a_delete(imap):
    imap.mailboxes = {SHOPPING: [(b"4", "boss@work.com", "<a@x>")]}
    real_uid = imap.uid.side_effect

    def _uid(command, *args):
        if command.upper() == 'COPY':
            return ("NO", [b"[TRYCREATE] no such mailbox"])
        return real_uid(command, *args)

    imap.uid.side_effect = _uid
    result = _run(imap, apply=True)

    assert imap.deleted == []          # the only copy survives
    assert result["totals"]["removed"] == 0
    assert result["totals"]["added"] == 0
    assert any("no copy in a matching label" in e for e in result["errors"])


def test_expunge_falls_back_to_plain_expunge_without_uidplus(imap):
    import imaplib
    imap.mailboxes = {
        WORK: [(b"1", "boss@work.com", "<a@x>")],
        SHOPPING: [(b"4", "boss@work.com", "<a@x>")],
    }
    real_uid = imap.uid.side_effect

    def _uid(command, *args):
        if command.upper() == 'EXPUNGE':
            raise imaplib.IMAP4.error("UID EXPUNGE not supported")
        return real_uid(command, *args)

    imap.uid.side_effect = _uid
    result = _run(imap, apply=True)

    imap.expunge.assert_called_once()
    assert result["totals"]["removed"] == 1


# ── apply: additions ──────────────────────────────────────────────────────────

def test_missing_label_gets_a_copy(imap):
    imap.mailboxes = {WORK: [(b"1", "multi@work.com", "<a@x>")], VIP: []}
    result = _run(imap, apply=True, email_rules={"multi@work.com": [WORK, VIP]})

    assert imap.copies == [(WORK, "1", VIP)]
    assert result["totals"]["added"] == 1
    assert result["totals"]["removed"] == 0


def test_no_copy_when_the_message_is_already_present(imap):
    imap.mailboxes = {
        WORK: [(b"1", "multi@work.com", "<a@x>")],
        VIP: [(b"9", "multi@work.com", "<a@x>")],
    }
    result = _run(imap, apply=True, email_rules={"multi@work.com": [WORK, VIP]})

    assert imap.copies == []
    assert result["totals"]["to_add"] == 0


def test_copy_is_skipped_when_the_target_already_holds_the_message(imap):
    """The index said the target was empty (it was truncated / changed under
    us), but the pre-COPY SEARCH finds the message — copying would duplicate."""
    imap.mailboxes = {WORK: [(b"1", "multi@work.com", "<a@x>")], VIP: []}

    real_uid = imap.uid.side_effect
    state = {"planned": False}

    def _uid(command, *args):
        if command.upper() == 'SEARCH' and len(args) >= 2 and str(args[1]).upper() == 'HEADER':
            state["planned"] = True
            return ("OK", [b"9"])       # already there
        return real_uid(command, *args)

    imap.uid.side_effect = _uid
    result = _run(imap, apply=True, email_rules={"multi@work.com": [WORK, VIP]})

    assert state["planned"] is True
    assert imap.copies == []
    assert result["totals"]["added"] == 0
    assert result["totals"]["skipped"] == 1


def test_unquotable_message_id_is_never_copied_or_removed(imap):
    # A Message-ID that can't be safely quoted into an IMAP SEARCH can't be
    # verified, so the copy is skipped — and with no copy landing, the removal
    # is blocked too.
    bad = '<we"ird@x>'
    imap.mailboxes = {SHOPPING: [(b"4", "boss@work.com", bad)], WORK: []}
    result = _run(imap, apply=True)

    assert imap.copies == [] and imap.deleted == []
    assert result["totals"]["skipped"] == 2   # the add and the blocked removal
    assert any("Could not verify" in e for e in result["errors"])


def test_apply_creates_a_missing_target_folder(imap):
    imap.mailboxes = {WORK: [(b"1", "multi@work.com", "<a@x>")]}
    # VIP has no folder at all — ensure_mailbox must create it before the COPY.
    _run(imap, apply=True, email_rules={"multi@work.com": [WORK, VIP]})

    created = [c.args[0] for c in imap.create.call_args_list]
    assert f'"{VIP}"' in created


# ── dry run ───────────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(imap):
    imap.mailboxes = {
        SHOPPING: [(b"4", "boss@work.com", "<a@x>")],
        WORK: [(b"1", "other@work.com", "<b@x>")],
    }
    result = _run(imap, apply=False)

    assert imap.copies == [] and imap.deleted == [] and imap.expunged == []
    imap.expunge.assert_not_called()
    imap.create.assert_not_called()
    assert result["applied"] is False
    assert result["totals"]["to_remove"] == 1
    assert result["totals"]["to_add"] == 1
    assert result["totals"]["removed"] == 0


def test_non_category_folders_are_never_scanned(imap):
    imap.mailboxes = {
        WORK: [(b"1", "boss@work.com", "<a@x>")],
        "INBOX": [(b"1", "boss@work.com", "<z@x>")],
        "Archive": [(b"2", "boss@work.com", "<y@x>")],
    }
    result = _run(imap, apply=True)

    assert result["totals"]["labels"] == 1
    assert imap.mailboxes["INBOX"] and imap.mailboxes["Archive"]
    assert imap.copies == [] and imap.deleted == []


def test_limit_is_reported_in_the_result(imap):
    imap.mailboxes = {WORK: [(b"1", "boss@work.com", "<a@x>"),
                             (b"2", "boss@work.com", "<b@x>")]}
    result = _run(imap, limit=1)

    assert result["limit"] == 1
    assert result["truncated"] is True
    assert result["totals"]["scanned"] == 1


# ── main() ────────────────────────────────────────────────────────────────────

def test_main_exits_on_corrupt_rules_file(tmp_path, monkeypatch):
    corrupt = tmp_path / "emailRules.json"
    corrupt.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("RULES_PATH", str(corrupt))
    monkeypatch.setenv("IMAP_SERVER", "imap.test.com")
    monkeypatch.setenv("IMAP_USERNAME", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.setattr("sys.argv", ["resortEmail.py"])

    with pytest.raises(SystemExit) as exc_info:
        resortEmail.main()
    assert exc_info.value.code == 1


def test_main_defaults_to_dry_run(tmp_path, monkeypatch, imap):
    rules = tmp_path / "emailRules.json"
    rules.write_text('{"labels": []}', encoding="utf-8")
    monkeypatch.setenv("RULES_PATH", str(rules))
    monkeypatch.setenv("IMAP_SERVER", "imap.test.com")
    monkeypatch.setenv("IMAP_USERNAME", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.setattr("sys.argv", ["resortEmail.py"])
    monkeypatch.setattr(resortEmail, "connect_to_imap", lambda *a, **k: imap)

    captured = {}

    def _fake_resort(*args, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "applied": False, "truncated": False, "limit": 0,
                "labels": {}, "totals": {"to_add": 0, "to_remove": 0, "labels": 0,
                                         "scanned": 0}, "errors": []}

    monkeypatch.setattr(resortEmail, "resort", _fake_resort)
    resortEmail.main()

    assert captured["apply"] is False
