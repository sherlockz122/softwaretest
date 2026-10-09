"""Conservative, deterministic evidence rules; false means non-candidate, never clean."""

import hashlib
import json
import re

RULE_VERSION = "fix-evidence-v1"
KEYWORD = (
    r"\b(?:fix(?:es|ed|ing)?|bugfix|hotfix)\b|"
    r"\b(?:resolve[ds]?|correct(?:ed|s)?)\b.{0,40}\b(?:bug|defect|crash|error)\b"
)
NEGATION = (
    r"\b(?:not\s+(?:a\s+)?(?:bug\s+)?fix|no\s+(?:bug\s+)?fix|(?:do|does|did)\s+not\s+fix|"
    r"(?:doesn't|don't|didn't)\s+fix|non[- ]fix|"
    r"(?:do|does|did)\s+not\s+(?:close|resolve)|(?:don't|doesn't|didn't)\s+(?:close|resolve))\b"
)
REFERENCE = re.compile(
    r"(?<![\w/])(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<number>[1-9][0-9]{0,8})\b"
)
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s*:?[ \t]+$", re.I)
RULE_HASH = hashlib.sha256(
    json.dumps(
        {
            "version": RULE_VERSION,
            "keyword": KEYWORD,
            "negation": NEGATION,
            "reference": REFERENCE.pattern,
            "closing": CLOSING.pattern,
            "exclusions": (
                "revert,typo,spelling,docs-only;message-encoding;20-refs;github-exact-bug-label-v1"
            ),
        },
        sort_keys=True,
    ).encode()
).hexdigest()


def references(message, repository):
    refs = {}
    for match in REFERENCE.finditer(message):
        number = int(match["number"])
        scope = match["repo"]
        local = scope is None or scope.casefold() == repository.casefold()
        closing = bool(CLOSING.search(message[max(0, match.start() - 24) : match.start()]))
        key = (scope.casefold() if scope else repository.casefold(), number)
        previous = refs.get(key)
        refs[key] = {
            "number": number,
            "local": local,
            "closing": closing or bool(previous and previous["closing"]),
            "reference": match.group(),
        }
        if len(refs) >= 20:
            break
    return list(refs.values())


def evaluate(message, parse_status, files, refs, include_medium=True):
    evidence = []

    def add(kind, value, confidence, source=None):
        evidence.append(
            {"type": kind, "value": value[:300], "confidence": confidence, "source": source or {}}
        )

    lower = message.casefold()
    excluded = None
    if re.match(r"\s*revert\b", lower) or "this reverts commit" in lower:
        excluded = "revert"
    elif re.search(NEGATION, message, re.I):
        excluded = "negated_fix"
    elif re.search(r"\b(?:typo|spelling)\b", lower):
        excluded = "spelling"
    elif files and all(
        path.casefold().startswith(("docs/", "doc/"))
        or re.search(r"(?:\.md|\.rst|\.adoc)$|(?:^|/)readme(?:\.[^/]*)?$", path.casefold())
        for f in files
        for path in (f["old_path"], f["new_path"])
        if path is not None
    ):
        excluded = "documentation_only"
    elif re.search(r"\b(?:documentation|docs)\b", lower) and not files:
        excluded = "documentation_context"
    if excluded:
        add("exclusion", excluded, "low")
    elif parse_status == "message_encoding":
        add("unavailable", "message_encoding", "low")
    elif match := re.search(KEYWORD, message, re.I):
        add("keyword", match.group(), "medium", {"start": match.start(), "end": match.end()})
    for ref in refs:
        snapshot = ref["snapshot"]
        high = (
            ref["local"]
            and ref["closing"]
            and snapshot["status"] == "bug"
            and not excluded
            and parse_status != "message_encoding"
        )
        add(
            "bug_issue" if high else "issue_reference",
            ref["reference"],
            "high" if high else "low",
            {"relation": "closing" if ref["closing"] else "mention", **snapshot},
        )
    candidate = any(
        e["confidence"] == "high" or include_medium and e["confidence"] == "medium"
        for e in evidence
    )
    complete = parse_status == "parsed" and all(f["content_status"] == "parsed" for f in files)
    disposition = (
        "excluded"
        if excluded
        else "unknown"
        if parse_status == "message_encoding"
        else "candidate"
        if candidate
        else "not_candidate"
    )
    return {
        "rule_candidate": candidate,
        "disposition": disposition,
        "content_complete": complete,
        "evidence": evidence,
    }
