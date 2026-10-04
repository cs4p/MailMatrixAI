import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

_spec = importlib.util.spec_from_file_location(
    "changelog_script", Path(__file__).resolve().parent.parent / "scripts" / "changelog.py")
changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog)


# ── classify / draft_summary ──────────────────────────────────────────────────

@pytest.mark.parametrize("subject,expected", [
    ("feat: add a page", ("feat", "Add a page")),
    ("feat(ui): add a page", ("feat", "ui: Add a page")),
    ("fix: crash on empty inbox", ("fix", "Crash on empty inbox")),
    ("feat!: drop python 3.9", ("breaking", "Drop python 3.9")),
    ("chore(deps): update electron to v44 [skip version] (#150)", ("other", "Update electron to v44 (#150)")),
    ("Fix sortEmail missing folded senders", ("fix", "Fix sortEmail missing folded senders")),
    ("Add token-usage log", ("feat", "Add token-usage log")),
    ("Bump anyio from 4.14.1 to 4.14.2", ("other", "Bump anyio from 4.14.1 to 4.14.2")),
])
def test_classify(subject, expected):
    assert changelog.classify(subject) == expected


def test_draft_summary_groups_in_section_order_and_dedupes():
    commits = [{"sha": "a", "subject": "Fix a bug"},
               {"sha": "b", "subject": "Add a feature"},
               {"sha": "c", "subject": "Fix a bug"},
               {"sha": "d", "subject": "Bump dep"}]
    assert changelog.draft_summary(commits) == (
        "### Features\n- Add a feature\n\n### Fixes\n- Fix a bug\n\n### Other changes\n- Bump dep")


def test_draft_summary_empty():
    assert changelog.draft_summary([]) == "- No notable changes."


@pytest.mark.parametrize("prev,new,level", [
    ("0.4.1", "0.4.2", "patch"), ("0.4.9", "0.5.0", "minor"),
    ("0.9.0", "1.0.0", "major"), (None, "0.3.0", "custom"),
])
def test_bump_level(prev, new, level):
    assert changelog.bump_level(prev, new) == level


# ── commits_between ───────────────────────────────────────────────────────────

def test_commits_between_drops_release_bump_commits():
    log = "aaa\x1fchore: bump version to 0.4.2 [skip version]\nbbb\x1fFix a bug"
    with patch.object(changelog, "git", return_value=log) as git:
        commits = changelog.commits_between("v0.4.1", "HEAD")
    assert commits == [{"sha": "bbb", "subject": "Fix a bug"}]
    assert git.call_args.args[1] == "v0.4.1..HEAD"
    assert "--no-merges" in git.call_args.args


# ── add / backfill ────────────────────────────────────────────────────────────

def test_add_prepends_entry(tmp_path):
    path = tmp_path / "changelog.json"
    path.write_text(json.dumps({"schema_version": 1, "project": "MailMatrixAI",
                                "releases": [{"version": "0.4.1"}]}))
    with patch.object(changelog, "CHANGELOG_PATH", path), \
         patch.object(changelog, "commits_between", return_value=[{"sha": "b", "subject": "Fix x"}]) as cb:
        changelog.main(["add", "--version", "0.4.2", "--previous", "0.4.1", "--bump", "patch"])
    cb.assert_called_once_with("v0.4.1", "HEAD")
    releases = json.loads(path.read_text())["releases"]
    assert [r["version"] for r in releases] == ["0.4.2", "0.4.1"]
    new = releases[0]
    assert new["bump"] == "patch" and new["previous_version"] == "0.4.1"
    assert new["summary"] == "### Fixes\n- Fix x"
    assert new["commits"] == [{"sha": "b", "subject": "Fix x"}]


def test_add_refuses_duplicate_version(tmp_path):
    path = tmp_path / "changelog.json"
    path.write_text(json.dumps({"schema_version": 1, "releases": [{"version": "0.4.2"}]}))
    with patch.object(changelog, "CHANGELOG_PATH", path), \
         patch.object(changelog, "commits_between", return_value=[]), \
         pytest.raises(SystemExit):
        changelog.main(["add", "--version", "0.4.2", "--previous", "0.4.1", "--bump", "patch"])
    assert len(json.loads(path.read_text())["releases"]) == 1


def test_backfill_builds_newest_first_from_tags(tmp_path):
    path = tmp_path / "changelog.json"

    def fake_git(*args):
        if args[:2] == ("tag", "--list"):
            return "v0.4.10\nv0.4.9\nnot-a-release\nv0.5.0"
        if args[:2] == ("log", "-1"):
            return "2026-10-04"
        raise AssertionError(args)

    with patch.object(changelog, "CHANGELOG_PATH", path), \
         patch.object(changelog, "git", side_effect=fake_git), \
         patch.object(changelog, "commits_between", return_value=[]) as cb:
        changelog.main(["backfill"])
    releases = json.loads(path.read_text())["releases"]
    assert [(r["version"], r["previous_version"], r["bump"]) for r in releases] == [
        ("0.5.0", "0.4.10", "minor"), ("0.4.10", "0.4.9", "patch"), ("0.4.9", None, "custom")]
    assert cb.call_args_list[0].args == (None, "v0.4.9")
    assert cb.call_args_list[1].args == ("v0.4.9", "v0.4.10")
