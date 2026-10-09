import hashlib
import tokenize

import pytest

from packages.mining import szz_engine
from packages.mining.szz_engine import deleted_code, porcelain, trace
from tests.szz_support import event, reader_for, szz_history


@pytest.mark.parametrize(
    "scenario,expected,links",
    [
        ("modify", "traced", 1),
        ("parent", "traced", 1),
        ("rename", "traced", 1),
        ("delete", "traced", 1),
        ("add", "no_traceable_lines", 0),
        ("comment", "no_traceable_lines", 0),
        ("blank", "no_traceable_lines", 0),
        ("string", "traced", 1),
        ("string_blank", "traced", 1),
        ("binary", "unknown", 0),
        ("encoding", "unknown", 0),
    ],
)
def test_actual_git_szz_golden(tmp_path, scenario, expected, links):
    bare, introduced, fixed, shas = szz_history(tmp_path / "source", scenario)
    reader = reader_for(bare, tmp_path / "attempt")
    record = reader.record(fixed)
    result = trace(
        reader,
        {"candidate": True, "visible": True, "available_at": "2026-10-10T00:00:00Z"},
        record["commit"],
        record["files"],
        {sha: event(i) for i, sha in enumerate(shas)},
    )
    assert result["status"] == expected and len(result["links"]) == links
    if links:
        link = result["links"][0]
        assert link["blamed_sha"] == introduced and link["eligible"]
        assert (
            link["blamed_line"] == link["fixed_line"] == (2 if scenario.startswith("string") else 1)
        )
        assert len(link["line_hash"]) == 64 and "author" not in str(result)
        assert link["parent_sha"] == record["commit"]["parents"][0]
        if scenario == "parent":
            assert link["blamed_sha"] == link["parent_sha"]
        if scenario == "rename":
            assert link["path"] == "重命名 code.py" and link["origin_path"] == "code.py"


@pytest.mark.parametrize(
    "reason",
    [
        "root",
        "merge",
        "hidden",
        "non_candidate",
        "outside",
        "time_inversion",
        "output_limit",
        "line_limit",
        "parent_blob",
        "symlink",
        "docs",
    ],
)
def test_unknown_and_filter_boundaries_real_git(tmp_path, monkeypatch, reason):
    bare, _, fixed, shas = szz_history(tmp_path / "source")
    reader = reader_for(bare, tmp_path / "attempt")
    record = reader.record(fixed)
    commit, files = record["commit"], record["files"]
    source = {
        "candidate": reason != "non_candidate",
        "visible": reason != "hidden",
        "available_at": "2026-10-10T00:00:00Z",
    }
    eligible = {sha: event(i) for i, sha in enumerate(shas)}
    if reason == "root":
        commit["parents"] = []
    if reason == "merge":
        commit["parents"] *= 2
    if reason == "outside":
        eligible.clear()
    if reason == "time_inversion":
        eligible[shas[0]] = event(99)
    if reason == "line_limit":
        monkeypatch.setattr(szz_engine, "LINE_LIMIT", 0)
    if reason == "output_limit":
        monkeypatch.setattr(szz_engine, "BLAME_LIMIT", 64)
    if reason == "parent_blob":
        files[0]["old_blob"] = "f" * 40
    if reason == "symlink":
        command = reader.command
        monkeypatch.setattr(
            reader,
            "command",
            lambda args, *a, **kw: (
                b"120000 blob " + files[0]["old_blob"].encode() + b"\tcode.py\0"
                if args[0] == "ls-tree"
                else command(args, *a, **kw)
            ),
        )
    if reason == "docs":
        files[0]["old_path"] = "docs/code.py"
    result = trace(reader, source, commit, files, eligible)
    assert not any(row["eligible"] for row in result["links"])
    assert result["status"] in {"unknown", "partial", "not_candidate", "no_traceable_lines"}
    assert "clean" not in str(result)


def test_python_filter_keeps_indentation_and_multiline_strings():
    assert deleted_code(
        b'  value=1\ntext="""x\n# string\ny"""\n# comment\n \n', "code.py", [1, 3, 5, 6]
    ) == [1, 3]
    assert deleted_code(b"// comment\n# value\n \n", "code.java", [1, 2, 3]) == [2]
    with pytest.raises((tokenize.TokenError, SyntaxError, ValueError)):
        deleted_code(b'text="""bad\n', "code.py", [1])
    with pytest.raises(ValueError):
        deleted_code(b"x\n", "code.py", [2])


def test_porcelain_rejects_incomplete_or_duplicate_lines():
    header = b"a" * 40 + b" 3 1 1\nfilename old.py\n\tvalue=1\n"
    assert porcelain(header)[1]["line_hash"] == hashlib.sha256(b"value=1").hexdigest()
    with pytest.raises(ValueError):
        porcelain(header + header)
    with pytest.raises(ValueError):
        porcelain(header.replace(b"filename old.py\n", b""))


@pytest.mark.parametrize(
    "content,expected",
    [
        (b"/* old\n comment */\nint x=1; // keep code\n", [3]),
        (b'String s="// not comment";\n// comment\n', [1]),
        (b'String s="""\n// string\n/* string */\n""";\n', [1, 2, 3, 4]),
        (b"int x=1; /* comment */ int y=2;\n/* only */\n", [1]),
    ],
)
def test_java_comment_lexer_preserves_literals_and_code(content, expected):
    assert deleted_code(content, "Code.java", list(range(1, content.count(b"\n") + 1))) == expected


@pytest.mark.parametrize("content", [b"/* unclosed", b'String s="unclosed', b'String s="\\u002f";'])
def test_java_ambiguous_filter_is_unknown(content):
    with pytest.raises(ValueError):
        deleted_code(content, "Code.java", [1])


@pytest.mark.parametrize(
    "path,content",
    [("code.py", b'text="""first\n  \nlast"""\n'), ("Code.java", b'String s="""\n  \nlast""";\n')],
)
def test_blank_lines_inside_literals_remain_traceable(path, content):
    assert deleted_code(content, path, [2]) == [2]
    with pytest.raises(ValueError):
        deleted_code(content.replace(b"\n", b"\r"), path, [1])
