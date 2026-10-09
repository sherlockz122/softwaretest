"""Bounded baseline blame; unknown is never a negative label."""

import hashlib
import io
import re
import tokenize
from pathlib import Path

from packages.repositories.parser import sha_valid

ALGORITHM_VERSION = "baseline-szz-v1"
BLAME_LIMIT = 8 * 1024**2
LINE_LIMIT = 10000
ALGORITHM_HASH = hashlib.sha256(
    (
        Path(__file__).read_text(encoding="utf-8")
        + Path(__file__).with_name("szz.py").read_text(encoding="utf-8")
    ).encode()
).hexdigest()


def java_comment_lines(text):
    """Lexical Java comments, preserving literals/text blocks; ambiguous input is unknown."""
    if re.search(r"\\u+[0-9a-fA-F]{4}", text):
        raise ValueError("Java Unicode preprocessing requires a different filter version")
    state, line, index = "code", 1, 0
    comments, code, literals = set(), set(), set()
    while index < len(text):
        char = text[index]
        if state == "code":
            if text.startswith("//", index):
                state = "line"
                comments.add(line)
                index += 2
                continue
            if text.startswith("/*", index):
                state = "block"
                comments.add(line)
                index += 2
                continue
            if text.startswith('"""', index):
                state = "text"
                code.add(line)
                literals.add(line)
                index += 3
                continue
            if char in {"'", '"'}:
                state = char
                literals.add(line)
            if not char.isspace():
                code.add(line)
        elif state == "line":
            comments.add(line)
            if char == "\n":
                state = "code"
        elif state == "block":
            comments.add(line)
            if text.startswith("*/", index):
                state = "code"
                index += 2
                continue
        else:
            code.add(line)
            literals.add(line)
            if char == "\\":
                if index + 1 < len(text) and text[index + 1] == "\n":
                    if state != "text":
                        raise ValueError("Invalid Java literal")
                    line += 1
                index += 2
                continue
            if state == "text" and text.startswith('"""', index):
                state = "code"
                index += 3
                continue
            if state in {"'", '"'}:
                if char == state:
                    state = "code"
                elif char == "\n":
                    raise ValueError("Invalid Java literal")
        if char == "\n":
            line += 1
        index += 1
    if state not in {"code", "line"}:
        raise ValueError("Unclosed Java lexical construct")
    return comments - code, literals


def deleted_code(content, path, numbers):
    """Preserve indentation and strings; filter blank and Python standalone comments only."""
    text = content.decode("utf-8", "strict")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    comments, literals = set(), set()
    if path.endswith((".py", ".java")) and re.search(r"\r(?!\n)", text):
        raise ValueError("Unsupported line mapping")
    if path.endswith(".py"):
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.ERRORTOKEN and token.string.strip():
                raise ValueError("Invalid lexical content")
            if token.type == tokenize.COMMENT:
                line, col = token.start
                if not lines[line - 1][:col].strip():
                    comments.add(line)
            elif token.type == tokenize.STRING:
                literals.update(range(token.start[0], token.end[0] + 1))
    elif path.endswith(".java"):
        comments, literals = java_comment_lines(text)
    if any(type(n) is not int or not 1 <= n <= len(lines) for n in numbers):
        raise ValueError("Invalid deleted lines")
    return [
        n
        for n in sorted(set(numbers))
        if (lines[n - 1].strip() or n in literals) and n not in comments
    ]


def porcelain(data):
    rows, current = {}, None
    for line in data.split(b"\n"):
        if match := re.fullmatch(
            rb"([0-9a-f]{40}|[0-9a-f]{64}) ([0-9]+) ([0-9]+)(?: [0-9]+)?", line
        ):
            current = {
                "blamed_sha": sha_valid(match[1]),
                "blamed_line": int(match[2]),
                "fixed_line": int(match[3]),
            }
        elif line.startswith(b"filename ") and current is not None:
            current["origin_path"] = line[9:].decode("utf-8", "strict")
        elif line.startswith(b"\t"):
            if current is None or "origin_path" not in current or current["fixed_line"] in rows:
                raise ValueError("Invalid blame result")
            current["line_hash"] = hashlib.sha256(line[1:]).hexdigest()
            rows[current["fixed_line"]] = current
            current = None
    if current is not None:
        raise ValueError("Incomplete blame result")
    return rows


def trace(reader, source, commit, files, eligible):
    """eligible maps accepted, parsed SHA to its event time; paths/bytes are bounded upstream."""
    result = {"status": "unknown", "reasons": [], "files": [], "links": []}
    if not source["visible"]:
        result["reasons"] = ["evidence_after_cutoff"]
        return result
    if not source["candidate"]:
        result.update(status="not_candidate", reasons=["frozen_non_candidate"])
        return result
    if len(commit["parents"]) != 1:
        result["reasons"] = ["merge_parent" if commit["parents"] else "root_parent"]
        return result
    if commit["parse_status"] != "parsed":
        result["reasons"] = [commit["parse_status"]]
        return result
    parent = commit["parents"][0]
    sha_valid(parent.encode("ascii"))
    used, ancestor_cache = 0, {}
    for file in files:
        reader.tick()
        path = file["old_path"]
        state = {"ordinal": file["ordinal"], "path": path or file["new_path"], "status": "unknown"}
        result["files"].append(state)
        if file["content_status"] != "parsed" or file["is_binary"]:
            state["reason"] = file["content_status"]
            continue
        if path is None:
            state.update(status="no_traceable_lines", reason="new_file")
            continue
        if path.casefold().startswith(("docs/", "doc/")) or re.search(
            r"\.(md|rst|adoc)$|(?:^|/)readme(?:\.[^/]*)?$", path, re.I
        ):
            state.update(status="excluded", reason="documentation")
            continue
        metadata = reader.command(["ls-tree", "-z", parent, "--", path])
        entry, _, actual_path = metadata.rstrip(b"\0").partition(b"\t")
        values = entry.split()
        if len(values) != 3 or values[0] not in {b"100644", b"100755"}:
            state["reason"] = "unsupported_mode"
            continue
        if actual_path.decode("utf-8", "strict") != path or values[2].decode() != file["old_blob"]:
            state["reason"] = "parent_blob_conflict"
            continue
        content = reader.blob(file["old_blob"], values[0].decode())
        if content is None:
            state["reason"] = "blob_limit"
            continue
        try:
            numbers = deleted_code(content, path, (file["line_numbers"] or {}).get("deleted", []))
        except (UnicodeError, ValueError, tokenize.TokenError, SyntaxError):
            state["reason"] = "filter_unavailable"
            continue
        if used + len(numbers) > LINE_LIMIT:
            state["reason"] = "line_limit"
            continue
        used += len(numbers)
        if not numbers:
            state.update(status="no_traceable_lines", reason="no_deleted_code")
            continue
        output = reader.command(
            ["blame", "--root", "--line-porcelain", "--no-textconv", parent, "--", path],
            BLAME_LIMIT,
            allow_limit=True,
        )
        if output is None:
            state["reason"] = "blame_output_limit"
            continue
        try:
            blamed = porcelain(output)
            old_lines = content.split(b"\n")
            if any(
                n not in blamed
                or blamed[n]["line_hash"] != hashlib.sha256(old_lines[n - 1]).hexdigest()
                for n in numbers
            ):
                raise ValueError("Blame content mismatch")
        except (ValueError, UnicodeError):
            state["reason"] = "blame_invalid"
            continue
        file_links = []
        for number in numbers:
            reader.tick()
            row = blamed[number]
            introduced = row["blamed_sha"]
            reason = None
            if introduced not in eligible:
                reason = "outside_parsed_history"
            elif eligible[introduced] > commit["committer_time"]:
                reason = "event_time_inversion"
            else:
                if introduced not in ancestor_cache:
                    ancestor_cache[introduced] = reader.ancestor(introduced, parent)
                if not ancestor_cache[introduced]:
                    reason = "not_parent_ancestor"
            file_links.append(
                {
                    **row,
                    "fix_sha": commit["sha"],
                    "parent_sha": parent,
                    "path": path,
                    "old_blob": file["old_blob"],
                    "eligible": reason is None,
                    "label": "candidate_buggy" if reason is None else "unknown",
                    "reason": reason,
                    "label_available_at": source["available_at"],
                }
            )
        result["links"].extend(file_links)
        state.update(status="traced", links=len(file_links))
    unknown = any(f["status"] == "unknown" for f in result["files"]) or any(
        not link["eligible"] for link in result["links"]
    )
    result["status"] = (
        "partial"
        if result["links"] and unknown
        else "unknown"
        if unknown
        else "traced"
        if result["links"]
        else "no_traceable_lines"
    )
    if unknown:
        result["reasons"] = sorted(
            {f.get("reason", "unavailable") for f in result["files"] if f["status"] == "unknown"}
            | {link["reason"] for link in result["links"] if link["reason"]}
        )
    return result
