"""
cleanupRules.py — interactive optimizer for emailRules.json.

Two optimizations:
  1. Domain collapse — email addresses that can be removed because an existing
     domain rule already covers them (even if they currently route to a different label).
  2. Duplicate addresses — email addresses filed under more than one label;
     prompts the user to pick the canonical one and removes the rest.
"""

import json
import logging
import os
import sys
from collections import defaultdict
from typing import Dict, List

from dotenv import load_dotenv

from commonFunctions import setup_logging

load_dotenv()

log = logging.getLogger(__name__)

RULES_PATH = os.environ.get("RULES_PATH", "emailRules.json")
_W = 66


# ── Display helpers ───────────────────────────────────────────────────────────

def _hr(char: str = "─") -> None:
    print(char * _W)


def _header(title: str) -> None:
    print()
    _hr("═")
    print(f"  {title}")
    _hr("═")


def _short(label: str) -> str:
    return label.replace("MailMatrixCategories/", "")


def _prompt(question: str, choices: List[str]) -> str:
    hint = "/".join(choices)
    valid = {c.lower() for c in choices}
    while True:
        try:
            ans = input(f"\n  {question} [{hint}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Interrupted — no further changes made.")
            sys.exit(0)
        if ans in valid:
            return ans
        print(f"  Please enter one of: {hint}")


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_rules(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_rules(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Saved %s", path)


# ── Analysis ──────────────────────────────────────────────────────────────────

def find_domain_collapsible(rules: dict) -> List[dict]:
    """
    For each domain rule, find all individual sender addresses across every
    label whose domain matches — these are redundant and can be removed.
    """
    domain_to_rule_labels: Dict[str, List[str]] = defaultdict(list)
    for entry in rules.get("labels", []):
        for domain in entry.get("emailDomains", []):
            domain_to_rule_labels[domain].append(entry["labelName"])

    results = []
    for domain in sorted(domain_to_rule_labels):
        rule_labels = domain_to_rule_labels[domain]
        collapsible = []
        for entry in rules.get("labels", []):
            for addr in entry.get("emailAddresses", []):
                if addr.lower().endswith(f"@{domain}"):
                    collapsible.append({"address": addr, "label": entry["labelName"]})
        if collapsible:
            results.append({
                "domain": domain,
                "rule_labels": rule_labels,
                "collapsible": sorted(collapsible, key=lambda x: x["address"]),
            })
    return results


def find_duplicate_addresses(rules: dict) -> List[dict]:
    """Return addresses that appear under more than one label."""
    addr_to_labels: Dict[str, List[str]] = defaultdict(list)
    for entry in rules.get("labels", []):
        for addr in entry.get("emailAddresses", []):
            addr_to_labels[addr].append(entry["labelName"])
    return sorted(
        [{"address": a, "labels": ls} for a, ls in addr_to_labels.items() if len(ls) > 1],
        key=lambda x: x["address"],
    )


# ── Mutations ─────────────────────────────────────────────────────────────────

def remove_address(rules: dict, address: str, label: str) -> bool:
    for entry in rules.get("labels", []):
        if entry["labelName"] == label:
            addrs = entry.get("emailAddresses", [])
            if address in addrs:
                addrs.remove(address)
                return True
    return False


# ── Interactive handlers ──────────────────────────────────────────────────────

def handle_domain_collapses(rules: dict, items: List[dict]) -> int:
    changes = 0
    for item in items:
        domain = item["domain"]
        rule_labels = item["rule_labels"]
        collapsible = item["collapsible"]

        print()
        _hr()
        print(f"  Domain rule:  *@{domain}")
        for rl in rule_labels:
            print(f"  Routes to:    {_short(rl)}  ({rl})")
        print()
        print(f"  {len(collapsible)} individual sender rule(s) can be collapsed:")

        cross = []
        for i, c in enumerate(collapsible, 1):
            same = c["label"] in rule_labels
            flag = "" if same else "  ← routes to a DIFFERENT label"
            print(f"    {i:2}. {c['address']:<42} [{_short(c['label'])}]{flag}")
            if not same:
                cross.append(c)

        if cross:
            print()
            print(f"  WARNING: {len(cross)} address(es) currently route to a different label.")
            print(f"  Removing them will redirect that mail to the domain rule's label instead.")

        ans = _prompt("Remove these individual sender rules?", ["y", "n"])
        if ans == "y":
            for c in collapsible:
                if remove_address(rules, c["address"], c["label"]):
                    print(f"    ✓ Removed  {c['address']}  from  {_short(c['label'])}")
                    changes += 1
    return changes


def handle_duplicate_addresses(rules: dict, items: List[dict]) -> int:
    changes = 0
    for item in items:
        addr = item["address"]
        labels = item["labels"]

        print()
        _hr()
        print(f"  Address: {addr}")
        print(f"  Filed under {len(labels)} label(s):")
        for i, lbl in enumerate(labels, 1):
            print(f"    {i}. {_short(lbl)}  ({lbl})")

        choices = [str(i) for i in range(1, len(labels) + 1)] + ["s"]
        ans = _prompt("Keep which label? (s = skip)", choices)
        if ans == "s":
            continue

        keep_idx = int(ans) - 1
        keep_label = labels[keep_idx]
        for i, lbl in enumerate(labels):
            if i == keep_idx:
                continue
            if remove_address(rules, addr, lbl):
                print(f"    ✓ Removed  {addr}  from  {_short(lbl)}")
                changes += 1

    return changes


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging("cleanup_rules.log")

    if not os.path.exists(RULES_PATH):
        print(f"Error: {RULES_PATH} not found. Run emailRulesInit.py first.")
        sys.exit(1)

    rules = load_rules(RULES_PATH)
    collapses = find_domain_collapsible(rules)
    duplicates = find_duplicate_addresses(rules)

    _header("emailRules.json — Optimization Report")

    if not collapses and not duplicates:
        print("\n  No optimizations found. Your rules look clean!")
        return

    if collapses:
        total_addrs = sum(len(c["collapsible"]) for c in collapses)
        print(f"\n  Domain collapse opportunities : {len(collapses)} domain(s), {total_addrs} redundant address(es)")
    if duplicates:
        print(f"  Duplicate address rules       : {len(duplicates)} address(es) in multiple labels")

    total_changes = 0

    if collapses:
        _header(f"1 of 2 — Domain Collapse  ({len(collapses)} domain(s))")
        print("  Individual sender rules that are already covered by a domain rule.")
        total_changes += handle_domain_collapses(rules, collapses)

    if duplicates:
        _header(f"2 of 2 — Duplicate Address Rules  ({len(duplicates)} address(es))")
        print("  Addresses filed under more than one label — pick the one to keep.")
        total_changes += handle_duplicate_addresses(rules, duplicates)

    print()
    _hr("═")
    if total_changes > 0:
        save_rules(rules, RULES_PATH)
        print(f"\n  {total_changes} change(s) saved to {RULES_PATH}")
    else:
        print("\n  No changes made.")
    _hr("═")
    print()


if __name__ == "__main__":
    main()
