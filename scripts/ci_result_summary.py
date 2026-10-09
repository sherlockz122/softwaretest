"""Print test identities and counts from JUnit without assertion payloads or logs."""

import re
import sys
from pathlib import Path
from xml.etree import ElementTree


def identifier(value):
    return re.sub(r"[^A-Za-z0-9_.\[\]-]", "_", value or "unknown")[:200]


def main():
    path = Path(sys.argv[1])
    if not path.is_file():
        print("JUnit result unavailable; test process failed before producing results")
        return
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failed = 0
    skipped = 0
    for case in cases:
        if case.find("skipped") is not None:
            skipped += 1
        for outcome in ("failure", "error"):
            if case.find(outcome) is not None:
                failed += 1
                print(
                    f"{outcome.upper()}: {identifier(case.get('classname'))}"
                    f".{identifier(case.get('name'))}"
                )
                # Source locations are useful without serializing assertion values.
                body = case.find(outcome).text or ""
                for file, line in re.findall(r"(?m)^([A-Za-z0-9_/.-]+\.py):(\d+):", body):
                    print(f"  location: {identifier(file)}:{line}")
                break
    print(f"JUnit: {len(cases)} cases, {failed} failed/error, {skipped} skipped")


if __name__ == "__main__":
    main()
