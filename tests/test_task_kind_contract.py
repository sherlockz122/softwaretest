"""Cross-boundary inventory regression for the observed evidence-collection gap."""

import re
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.platform import acceptance_evidence as evidence
from packages.tasks.kinds import TYPES


def test_ui_kind_filter_and_safe_collection_cover_all_executable_kinds():
    source = (assets.ROOT / "apps/web/src/task-tools.js").read_text(encoding="utf-8")
    block = source.split("export const taskTypes = {", 1)[1].split("};", 1)[0]
    names = re.findall(r'^\s*(?:"([a-z.]+)"|([a-z]+)):\s*"', block, flags=re.M)
    assert {quoted or bare for quoted, bare in names} == TYPES
    for kind in TYPES:
        identity = str(uuid4())
        envelope = {
            "version": evidence.VERSION,
            "truncated": False,
            "tasks": [
                {
                    "id": identity,
                    "root_task_id": identity,
                    "type": kind,
                    "status": "succeeded",
                    "error_code": None,
                }
            ],
        }
        assert evidence.validate(envelope) == envelope
