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
    api = (ROOT / "docs/04-API与数据库规格.md").read_text(encoding="utf-8")
    ids = re.findall(r"^\| (A\d+) \|", api, flags=re.M)
    if len(ids) != 35 or len(set(ids)) != 35:
        raise RuntimeError("Documented API inventory must contain 35 unique IDs")
    application = create_app(load_settings(environment="test"))
    generated = (
        json.dumps(application.openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    if args.export:
        CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT.write_text(generated, encoding="utf-8")
    elif not CONTRACT.exists() or CONTRACT.read_text(encoding="utf-8") != generated:
        raise RuntimeError("OpenAPI drift: export and review the contract with --export")
    print(f"PASS {links} local links, 35 API IDs, bootstrap OpenAPI contract")


if __name__ == "__main__":
    main()
