import pytest

from packages.mining.fix_rules import evaluate, references
from packages.mining.issues import observation


@pytest.mark.parametrize(
    "message,candidate,disposition",
    [
        ("FIX crash", True, "candidate"),
        ("Fixes race", True, "candidate"),
        ("Bugfix overflow", True, "candidate"),
        ("hotfix: handle error", True, "candidate"),
        ("Resolved crash", True, "candidate"),
        ("prefix suffix fixture", False, "not_candidate"),
        ("add bug reporting", False, "not_candidate"),
        ("", False, "not_candidate"),
        ("Refs #18", False, "not_candidate"),
        ('Revert "fix crash"', False, "excluded"),
        ("this reverts commit abc; fix crash", False, "excluded"),
        ("not a fix", False, "excluded"),
        ("does not fix crash", False, "excluded"),
        ("doesn't fix crash", False, "excluded"),
        ("no bug fix intended", False, "excluded"),
        ("fix typo", False, "excluded"),
        ("fix spelling", False, "excluded"),
        ("Fix documentation", False, "excluded"),
        ("non-fix change", False, "excluded"),
    ],
)
def test_rule_golden_boundaries_and_exclusions(message, candidate, disposition):
    result = evaluate(message, "parsed", [], [], True)
    assert result["rule_candidate"] == candidate and result["disposition"] == disposition


def test_doc_only_mixed_rename_encoding_and_skipped_content():
    docs = {"old_path": None, "new_path": "docs/guide.md", "content_status": "parsed"}
    code = {"old_path": "src.py", "new_path": "docs/guide.md", "content_status": "binary"}
    assert evaluate("Fix crash", "parsed", [docs], [])["disposition"] == "excluded"
    result = evaluate("Fix crash", "parsed", [docs, code], [])
    assert result["rule_candidate"] and not result["content_complete"]
    assert evaluate("Fix crash", "message_encoding", [], [])["disposition"] == "unknown"
    assert not evaluate("fix crash", "parsed", [], [], False)["rule_candidate"]


@pytest.mark.parametrize(
    "message,bug,expected",
    [
        ("Closes #18", "bug", True),
        ("Refs #18", "bug", False),
        ("Closes #18", "not_bug", False),
        ("Closes #18", "pull_request", False),
        ("Closes #18", "unavailable", False),
        ("Closes other/repo#18", "bug", False),
        ("does not close #18", "bug", False),
        ('Revert "Closes #18"', "bug", False),
        ("Closes team/demo#18", "bug", True),
    ],
)
def test_issue_confirmation_requires_local_closing_relation(message, bug, expected):
    refs = references(message, "team/demo")
    for ref in refs:
        ref["snapshot"] = observation(bug, ref["number"])
    result = evaluate(message, "parsed", [], refs, False)
    assert result["rule_candidate"] == expected
    assert any(e["confidence"] == "high" for e in result["evidence"]) == expected


def test_reference_dedup_and_bounded_input():
    refs = references(
        "Refs #18 then closes #18 and " + " ".join("#" + str(i) for i in range(1, 40)), "team/demo"
    )
    assert len(refs) == 20 and refs[0]["closing"] and refs[0]["number"] == 18
    assert references("abc#12 #0 #1234567890 #2suffix", "team/demo") == []
