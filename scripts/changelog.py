#!/usr/bin/env python3
"""Maintain changelog.json (the release history shown at /changelog).

Same file format as the shared version-bump tooling (schema_version 1, newest
release first). Releases here are cut by CI, so entries are drafted from the
commit subjects in each release instead of written by hand.

  changelog.py add --version X.Y.Z --previous X.Y.Z --bump patch|minor|major
      Prepend an entry for the commits in vPREVIOUS..HEAD (version-bump.yml).
  changelog.py backfill
      Rebuild the file from every existing vX.Y.Z tag.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG_PATH = ROOT / "changelog.json"
SCHEMA_VERSION = 1
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
CONVENTIONAL_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
# The release workflow's own commit carries no change of its own.
BUMP_COMMIT_RE = re.compile(r"^chore: bump version to \d+\.\d+\.\d+")
SECTIONS = [("breaking", "Breaking changes"), ("feat", "Features"), ("fix", "Fixes"),
            ("other", "Other changes")]
MAINTENANCE_TYPES = {"chore", "ci", "build", "test", "style", "docs", "refactor", "perf"}
_FIX_WORDS = re.compile(r"^(fix|fixes|fixed|correct|repair|handle|stop|prevent)\b", re.I)
_FEAT_WORDS = re.compile(r"^(add|adds|added|introduce|support|show|display|replace|default)\b", re.I)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def commits_between(start: Optional[str], end: str) -> List[dict]:
    rev = f"{start}..{end}" if start else end
    out = git("log", rev, "--no-merges", "--format=%h%x1f%s")
    commits = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\x1f")
        if subject and not BUMP_COMMIT_RE.match(subject):
            commits.append({"sha": sha, "subject": subject})
    return commits


def classify(subject: str) -> Tuple[str, str]:
    """(section key, display text) for one commit subject."""
    text = re.sub(r"\s*\[skip version\]", "", subject).strip()
    m = CONVENTIONAL_RE.match(text)
    if m:
        kind = m["type"].lower()
        desc = m["desc"][:1].upper() + m["desc"][1:]
        if m["scope"] and kind not in MAINTENANCE_TYPES:
            desc = f"{m['scope']}: {desc}"
        if m["bang"]:
            return "breaking", desc
        if kind in ("feat", "fix"):
            return kind, desc
        return "other", desc
    if _FIX_WORDS.match(text):
        return "fix", text
    if _FEAT_WORDS.match(text):
        return "feat", text
    return "other", text


def draft_summary(commits: List[dict]) -> str:
    grouped = {key: [] for key, _ in SECTIONS}
    for c in commits:
        key, text = classify(c["subject"])
        if text not in grouped[key]:
            grouped[key].append(text)
    parts = [f"### {title}\n" + "\n".join(f"- {t}" for t in grouped[key])
             for key, title in SECTIONS if grouped[key]]
    return "\n\n".join(parts) if parts else "- No notable changes."


def bump_level(previous: Optional[str], version: str) -> str:
    if not previous:
        return "custom"
    a = [int(x) for x in previous.split(".")]
    b = [int(x) for x in version.split(".")]
    if b[0] != a[0]:
        return "major"
    if b[1] != a[1]:
        return "minor"
    return "patch"


def make_entry(version: str, previous: Optional[str], bump: str, day: str,
               commits: List[dict]) -> dict:
    return {
        "version": version,
        "date": day,
        "bump": bump,
        "previous_version": previous,
        "summary": draft_summary(commits),
        "commits": commits,
    }


def load(path: Optional[Path] = None) -> dict:
    path = path or CHANGELOG_PATH
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "project": "MailMatrixAI", "releases": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save(data: dict, path: Optional[Path] = None) -> None:
    (path or CHANGELOG_PATH).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def release_tags() -> List[str]:
    tags = [t for t in git("tag", "--list", "v*").splitlines() if TAG_RE.match(t)]
    return sorted(tags, key=lambda t: tuple(int(x) for x in TAG_RE.match(t).groups()))


def cmd_add(version: str, previous: str, bump: str) -> None:
    data = load()
    if any(r["version"] == version for r in data["releases"]):
        raise SystemExit(f"changelog.json already has {version}")
    commits = commits_between(f"v{previous}", "HEAD")
    data["releases"].insert(0, make_entry(version, previous, bump, date.today().isoformat(), commits))
    save(data)
    print(f"changelog.json: added {version} ({len(commits)} commit(s))")


def cmd_backfill() -> None:
    releases = []
    prev_tag: Optional[str] = None
    for tag in release_tags():
        version, previous = tag[1:], (prev_tag[1:] if prev_tag else None)
        day = git("log", "-1", "--format=%cs", tag)
        releases.insert(0, make_entry(version, previous, bump_level(previous, version), day,
                                      commits_between(prev_tag, tag)))
        prev_tag = tag
    data = load()
    data["releases"] = releases
    save(data)
    print(f"changelog.json: wrote {len(releases)} release(s)")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("--version", required=True)
    add.add_argument("--previous", required=True)
    add.add_argument("--bump", required=True, choices=["patch", "minor", "major"])
    sub.add_parser("backfill")
    args = parser.parse_args(argv)
    if args.command == "add":
        cmd_add(args.version, args.previous, args.bump)
    else:
        cmd_backfill()


if __name__ == "__main__":
    main()
