import json
from unittest.mock import patch

import pytest

from cleanupRules import (
    find_domain_collapsible,
    find_duplicate_addresses,
    load_rules,
    remove_address,
    save_rules,
    handle_domain_collapses,
    handle_duplicate_addresses,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def rules_with_domain():
    return {
        "labels": [
            {
                "labelName": "MailMatrixCategories/Work",
                "emailAddresses": ["alice@work.com", "bob@work.com"],
                "emailDomains": ["work.com"],
            },
            {
                "labelName": "MailMatrixCategories/Other",
                "emailAddresses": ["charlie@work.com"],
                "emailDomains": [],
            },
        ]
    }


@pytest.fixture
def rules_with_duplicates():
    return {
        "labels": [
            {
                "labelName": "MailMatrixCategories/Work",
                "emailAddresses": ["shared@example.com", "unique@work.com"],
                "emailDomains": [],
            },
            {
                "labelName": "MailMatrixCategories/VIP",
                "emailAddresses": ["shared@example.com"],
                "emailDomains": [],
            },
        ]
    }


# ── load_rules / save_rules ───────────────────────────────────────────────────

def test_load_rules_reads_json(tmp_path):
    data = {"labels": [{"labelName": "MailMatrixCategories/Work", "emailAddresses": [], "emailDomains": []}]}
    f = tmp_path / "rules.json"
    f.write_text(json.dumps(data))
    assert load_rules(str(f)) == data


def test_save_rules_writes_json(tmp_path):
    data = {"labels": []}
    f = tmp_path / "rules.json"
    save_rules(data, str(f))
    assert json.loads(f.read_text()) == data


def test_save_rules_pretty_prints(tmp_path):
    f = tmp_path / "rules.json"
    save_rules({"labels": []}, str(f))
    assert "\n" in f.read_text()


# ── find_domain_collapsible ───────────────────────────────────────────────────

def test_finds_same_label_collapse(rules_with_domain):
    result = find_domain_collapsible(rules_with_domain)
    assert len(result) == 1
    domains = {r["domain"] for r in result}
    assert "work.com" in domains

    item = result[0]
    addrs = {c["address"] for c in item["collapsible"]}
    assert "alice@work.com" in addrs
    assert "bob@work.com" in addrs


def test_finds_cross_label_collapse(rules_with_domain):
    result = find_domain_collapsible(rules_with_domain)
    item = result[0]
    addrs = {c["address"] for c in item["collapsible"]}
    # charlie@work.com is in Other but work.com rule is in Work — still collapsible
    assert "charlie@work.com" in addrs


def test_cross_label_flagged_differently(rules_with_domain):
    result = find_domain_collapsible(rules_with_domain)
    item = result[0]
    charlie = next(c for c in item["collapsible"] if c["address"] == "charlie@work.com")
    assert charlie["label"] == "MailMatrixCategories/Other"
    assert charlie["label"] not in item["rule_labels"]


def test_no_collapsible_when_no_domain_rules():
    rules = {
        "labels": [{
            "labelName": "MailMatrixCategories/Work",
            "emailAddresses": ["alice@work.com"],
            "emailDomains": [],
        }]
    }
    assert find_domain_collapsible(rules) == []


def test_no_collapsible_when_no_matching_addresses():
    rules = {
        "labels": [{
            "labelName": "MailMatrixCategories/Work",
            "emailAddresses": ["alice@other.com"],
            "emailDomains": ["work.com"],
        }]
    }
    assert find_domain_collapsible(rules) == []


def test_collapsible_results_sorted_by_address(rules_with_domain):
    result = find_domain_collapsible(rules_with_domain)
    addrs = [c["address"] for c in result[0]["collapsible"]]
    assert addrs == sorted(addrs)


# ── find_duplicate_addresses ──────────────────────────────────────────────────

def test_finds_address_in_multiple_labels(rules_with_duplicates):
    result = find_duplicate_addresses(rules_with_duplicates)
    assert len(result) == 1
    assert result[0]["address"] == "shared@example.com"
    assert set(result[0]["labels"]) == {
        "MailMatrixCategories/Work",
        "MailMatrixCategories/VIP",
    }


def test_unique_addresses_not_reported(rules_with_duplicates):
    result = find_duplicate_addresses(rules_with_duplicates)
    addrs = {r["address"] for r in result}
    assert "unique@work.com" not in addrs


def test_no_duplicates_returns_empty():
    rules = {
        "labels": [
            {"labelName": "MailMatrixCategories/Work", "emailAddresses": ["a@b.com"], "emailDomains": []},
            {"labelName": "MailMatrixCategories/VIP", "emailAddresses": ["c@d.com"], "emailDomains": []},
        ]
    }
    assert find_duplicate_addresses(rules) == []


def test_duplicate_results_sorted_by_address():
    rules = {
        "labels": [
            {"labelName": "MailMatrixCategories/A", "emailAddresses": ["z@x.com", "a@x.com"], "emailDomains": []},
            {"labelName": "MailMatrixCategories/B", "emailAddresses": ["z@x.com", "a@x.com"], "emailDomains": []},
        ]
    }
    result = find_duplicate_addresses(rules)
    addrs = [r["address"] for r in result]
    assert addrs == sorted(addrs)


# ── remove_address ────────────────────────────────────────────────────────────

def test_remove_address_success(rules_with_duplicates):
    assert remove_address(rules_with_duplicates, "shared@example.com", "MailMatrixCategories/Work")
    work = next(e for e in rules_with_duplicates["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    assert "shared@example.com" not in work["emailAddresses"]


def test_remove_address_leaves_other_labels_intact(rules_with_duplicates):
    remove_address(rules_with_duplicates, "shared@example.com", "MailMatrixCategories/Work")
    vip = next(e for e in rules_with_duplicates["labels"] if e["labelName"] == "MailMatrixCategories/VIP")
    assert "shared@example.com" in vip["emailAddresses"]


def test_remove_address_returns_false_when_not_found(rules_with_duplicates):
    assert not remove_address(rules_with_duplicates, "nobody@example.com", "MailMatrixCategories/Work")


def test_remove_address_returns_false_wrong_label(rules_with_duplicates):
    assert not remove_address(rules_with_duplicates, "shared@example.com", "MailMatrixCategories/NonExistent")


# ── handle_domain_collapses ───────────────────────────────────────────────────

def test_handle_domain_collapses_removes_on_yes(rules_with_domain):
    items = find_domain_collapsible(rules_with_domain)
    with patch("builtins.input", return_value="y"):
        changes = handle_domain_collapses(rules_with_domain, items)

    assert changes == 3  # alice, bob, charlie
    work = next(e for e in rules_with_domain["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    assert "alice@work.com" not in work["emailAddresses"]
    assert "bob@work.com" not in work["emailAddresses"]
    other = next(e for e in rules_with_domain["labels"] if e["labelName"] == "MailMatrixCategories/Other")
    assert "charlie@work.com" not in other["emailAddresses"]


def test_handle_domain_collapses_skips_on_no(rules_with_domain):
    items = find_domain_collapsible(rules_with_domain)
    with patch("builtins.input", return_value="n"):
        changes = handle_domain_collapses(rules_with_domain, items)

    assert changes == 0
    work = next(e for e in rules_with_domain["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    assert "alice@work.com" in work["emailAddresses"]


# ── handle_duplicate_addresses ────────────────────────────────────────────────

def test_handle_duplicates_keeps_chosen_label(rules_with_duplicates):
    items = find_duplicate_addresses(rules_with_duplicates)
    # Labels are: [Work, VIP] — pick "1" to keep Work
    with patch("builtins.input", return_value="1"):
        changes = handle_duplicate_addresses(rules_with_duplicates, items)

    assert changes == 1
    work = next(e for e in rules_with_duplicates["labels"] if e["labelName"] == "MailMatrixCategories/Work")
    vip = next(e for e in rules_with_duplicates["labels"] if e["labelName"] == "MailMatrixCategories/VIP")
    assert "shared@example.com" in work["emailAddresses"]
    assert "shared@example.com" not in vip["emailAddresses"]


def test_handle_duplicates_skip_leaves_rules_unchanged(rules_with_duplicates):
    items = find_duplicate_addresses(rules_with_duplicates)
    with patch("builtins.input", return_value="s"):
        changes = handle_duplicate_addresses(rules_with_duplicates, items)

    assert changes == 0
    for entry in rules_with_duplicates["labels"]:
        assert "shared@example.com" in entry["emailAddresses"]
