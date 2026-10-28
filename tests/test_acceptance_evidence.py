"""Bounded failure evidence, secret rejection and real PowerShell finally ordering."""

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from packages.mining import public_assets as assets
from packages.platform import acceptance_evidence as e
from scripts import collect_fresh_task_evidence as driver


@pytest.fixture
def work():
    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root, prefix="safe-evidence-") as value:
        yield Path(value)


def safe():
    identity = str(uuid4())
    return {
        "version": e.VERSION,
        "truncated": False,
        "tasks": [
            {
                "id": identity,
                "root_task_id": identity,
                "type": "repository.clone",
                "status": "failed",
                "error_code": "REPOSITORY_UNSAFE_ADDRESS",
            }
        ],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", {"password": "private-password"}),
        ("error_message", "private-message"),
        ("actor_id", "private-actor"),
        ("id", "../private-file"),
        ("root_task_id", "private-root"),
        ("type", "private-secret"),
        ("status", "private-status"),
        ("error_code", "password=private-password"),
        ("error_code", "A" * 65),
    ],
)
def test_untrusted_fields_never_written(work, field, value):
    data = safe()
    data["tasks"][0][field] = value
    target = work / "summary.json"
    assert not driver.capture([], target, runner=lambda _: data)
    result = target.read_text()
    assert "private" not in result and "payload" not in result and "error_message" not in result
    assert json.loads(result)["collection_status"] == "failed"


def test_success_preserves_safe_failure_and_root(work):
    data = safe()
    target = work / "summary.json"
    assert driver.capture([], target, runner=lambda _: data)
    result = json.loads(target.read_text())
    assert result["tasks"] == data["tasks"] and result["collection_status"] == "succeeded"
    with pytest.raises(ValueError, match="OUTPUT_EXISTS"):
        driver.capture([], target, runner=lambda _: data)


@pytest.mark.parametrize("alter", ["duplicate", "overflow", "version", "truncated", "extra"])
def test_bad_envelope_is_explicit(work, alter):
    data = safe()
    if alter == "duplicate":
        data["tasks"] *= 2
    elif alter == "overflow":
        data["tasks"] *= 1001
    elif alter == "version":
        data["version"] = "another-protocol"
    elif alter == "truncated":
        data["truncated"] = 1
    else:
        data["secret"] = "private"
    assert not driver.capture([], work / "summary.json", runner=lambda _: data)


def test_decode_rejects_duplicate_json_keys():
    with pytest.raises(ValueError, match="DUPLICATE_KEY"):
        e.decode(b'{"version":"private","version":"fresh-task-evidence-v1"}')


def test_real_child_success_output_cap_timeout_and_stderr(work):
    data = safe()
    command = [sys.executable, "-c", "import sys;sys.stdout.write(" + repr(json.dumps(data)) + ")"]
    assert driver.bounded(command) == data
    with pytest.raises(ValueError, match="OUTPUT_LIMIT"):
        driver.bounded([sys.executable, "-c", f"print('x'*{e.MAX_BYTES + 1})"])
    with pytest.raises(ValueError, match="TIMEOUT"):
        driver.bounded([sys.executable, "-c", "import time;time.sleep(20)"], timeout=0.1)
    command = [sys.executable, "-c", "import sys;sys.stderr.write('private-password');sys.exit(3)"]
    assert not driver.capture(command, work / "failure.json")
    assert "private" not in (work / "failure.json").read_text()


def test_database_projection_excludes_secret_columns(monkeypatch):
    import sqlalchemy.orm

    from packages.platform import config, connections

    selected, closed = [], []
    data = safe()["tasks"]

    class FakeConnections:
        engine = object()

        def __init__(self, _):
            pass

        def close(self):
            closed.append(True)

    class FakeSession:
        def __init__(self, _):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query):
            selected.extend(c.key for c in query.selected_columns)
            self.query = query
            return self

        def mappings(self):
            return self

        def all(self):
            return copy.deepcopy(data)

    monkeypatch.setattr(connections, "Connections", FakeConnections)
    monkeypatch.setattr(config, "load_settings", lambda: None)
    monkeypatch.setattr(sqlalchemy.orm, "Session", FakeSession)
    assert e.collect_database()["tasks"] == data
    assert set(selected) == {"id", "root_task_id", "type", "status", "error_code"}
    assert closed == [True]


@pytest.mark.parametrize(
    "scenario", ["success", "browser-failed", "collector-failed", "logs-failed", "no-browser"]
)
def test_actual_powershell_finally_always_captures_before_cleanup(work, scenario):
    pwsh = shutil.which("pwsh")
    assert pwsh, "PowerShell required by the existing cross-platform acceptance workflow"
    source = assets.ROOT / "scripts/Test-FreshEnvironment.ps1"
    (work / "Invoke-ProjectCommand.ps1").write_text(
        "param($ExpectedGrowthGiB,$FilePath,$ArgumentList)\n"
        "Add-Content -LiteralPath $global:calls -Value 'collector'\n"
        "if ($global:scenario -eq 'collector-failed') { throw 'private-password' }\n",
        encoding="utf-8",
    )
    runner = work / "runner.ps1"
    runner.write_text(
        r"""
param($source,$folder,$mode)
$ErrorActionPreference='Stop'
$global:scenario=$mode
$global:calls=Join-Path $folder 'calls.txt'
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$null,[ref]$null)
$target=$ast.Find({param($n)
    $n -is [Management.Automation.Language.TryStatementAst] -and $n.Finally -and
    $n.Finally.Extent.Text.Contains("'task-summary.json'")
},$true)
if (-not $target) { throw 'Required finally not found' }
$block=$target.Finally.Extent.Text
$block=$block.Substring(1,$block.Length-2)
$EvidenceDirectory=$folder; $projectPython='unused'; $DockerPath='Fake-Docker'
$project='dg-fresh-012345abcdef'; $config='unused'; $override='unused'; $base=@()
$Browser=$mode -ne 'no-browser'
$acceptanceFailed=$mode -eq 'browser-failed'
function Fake-Docker {
    Add-Content -LiteralPath $global:calls -Value 'logs'
    if ($global:scenario -eq 'logs-failed') { throw 'private-password' }
    $global:LASTEXITCODE=0
}
function Invoke-FreshCompose {
    param($Arguments)
    if ($Arguments -notcontains 'down') { throw 'Unexpected mutation' }
    Add-Content -LiteralPath $global:calls -Value 'down'
}
$previous=(Get-Location).Path
Push-Location $folder
. ([scriptblock]::Create('$PSScriptRoot=$folder' + "`n" + $block))
if ((Get-Location).Path -ne $previous) { throw 'Location not restored' }
$result=@{failed=$acceptanceFailed;calls=@(Get-Content -LiteralPath $global:calls)}
Write-Output ('RESULT:'+($result|ConvertTo-Json -Compress))
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(runner), str(source), str(work), scenario],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "PowerShell cleanup regression failed"
    lines = result.stdout.decode("utf-8-sig").splitlines()
    data = json.loads(next(line[7:] for line in lines if line.startswith("RESULT:")))
    assert data["calls"] == (
        ["collector", "down"] if scenario == "no-browser" else ["collector", "logs", "down"]
    )
    assert data["failed"] == (scenario in {"browser-failed", "collector-failed", "logs-failed"})
    assert b"private-password" not in result.stdout + result.stderr
