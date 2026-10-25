"""Check documented links/API inventory and the generated bootstrap OpenAPI contract."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from apps.api.application import create_app
from packages.platform.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/contracts/bootstrap-openapi.json"
CHANGES = ROOT / "openspec/changes"

# 变更文档的文风检查。口径刻意收窄，只卡一眼能判的硬伤，
# 取舍理由见 docs/development/OpenSpec文档规格要求.md 第 2 节。
BANNED_PHRASES = (
    "保持活动状态",
    "具体状态见",
    "详见遗留问题表",
    "个人验收",
    "团队审批通过",
    "不代表算法",
    "不代表团队",
)
PHASE_HEADING = re.compile(r"^#{1,3}\s.*第[一二三四五六七八九十]+阶段", flags=re.M)
BOLD_SPAN = re.compile(r"\*\*(.+?)\*\*")
STRUCTURAL_BOLD = frozenset({"WHEN", "THEN", "AND", "SHALL", "MUST"})
BOLD_LIMIT = 10


def locate_line(content, needle):
    for number, line in enumerate(content.splitlines(), start=1):
        if needle in line:
            return number
    return 0


def check_change_doc_style():
    """Reject the writing tics that make change docs read as machine output."""
    checked = 0
    for path in sorted(CHANGES.rglob("*.md")):
        if "archive" in path.relative_to(CHANGES).parts:
            continue
        relative = path.relative_to(ROOT)
        content = path.read_text(encoding="utf-8")
        for phrase in BANNED_PHRASES:
            if phrase in content:
                line = locate_line(content, phrase)
                raise RuntimeError(f"Banned phrase in {relative}:{line}: {phrase}")
        heading = PHASE_HEADING.search(content)
        if heading:
            line = locate_line(content, heading.group(0).strip())
            raise RuntimeError(f"Phase number used as a heading in {relative}:{line}")
        bold = [span for span in BOLD_SPAN.findall(content) if span not in STRUCTURAL_BOLD]
        if len(bold) > BOLD_LIMIT:
            raise RuntimeError(
                f"Too much bold in {relative}: {len(bold)} spans, limit {BOLD_LIMIT}"
            )
        checked += 1
    return checked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    files = [
        ROOT / "README.md",
        *sorted((ROOT / "docs").rglob("*.md")),
        *sorted((ROOT / "openspec").rglob("*.md")),
    ]
    links = 0
    for path in files:
        content = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", content):
            target = target.strip().strip("<>")
            if target.startswith("#") or urlsplit(target).scheme:
                continue
            target = unquote(target.split("#")[0])
            if target:
                links += 1
                if not (path.parent / target).exists():
                    raise RuntimeError(f"Broken link in {path.relative_to(ROOT)}: {target}")
    styled = check_change_doc_style()
    api = (ROOT / "docs/04-API与数据库规格.md").read_text(encoding="utf-8")
    ids = re.findall(r"^\| (A\d+) \|", api, flags=re.M)
    # 计数刻意固定：新增或删减接口必须在这里改一次，避免文档清单悄悄变化。
    if len(ids) != 36 or len(set(ids)) != 36:
        raise RuntimeError("Documented API inventory must contain 36 unique IDs")
    application = create_app(load_settings(environment="test"))
    generated = (
        json.dumps(application.openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    if args.export:
        CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT.write_text(generated, encoding="utf-8")
    elif not CONTRACT.exists() or CONTRACT.read_text(encoding="utf-8") != generated:
        raise RuntimeError("OpenAPI drift: export and review the contract with --export")
    print(
        f"PASS {links} local links, {len(ids)} API IDs, bootstrap OpenAPI contract, "
        f"{styled} change docs match the writing style"
    )


if __name__ == "__main__":
    main()
