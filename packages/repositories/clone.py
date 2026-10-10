"""Supervised bare clone: no worktree, credentials, hooks, submodules or unpinned egress."""

import os
import re
import shutil
import signal
import subprocess
import time

from packages.repositories.safety import RepositoryError, URLPolicy, canonicalize
from packages.repositories.storage import Storage, directory_bytes, safe_path
from packages.repositories.transport import Tunnel, connect_ip


class ExecutionStopped(Exception):
    pass


def terminate(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


class CloneExecutor:
    def __init__(self, settings, service, policy=None, connector=connect_ip, git=None, cafile=None):
        self.settings, self.service = settings, service
        self.storage = Storage(settings)
        self.policy = policy or URLPolicy()
        self.connector, self.git, self.cafile = connector, git or shutil.which("git"), cafile

    def environment(self, attempt, tunnel):
        home = attempt / "home"
        template = attempt / "empty-template"
        home.mkdir()
        template.mkdir()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
        }
        environment.update(
            HOME=str(home),
            XDG_CONFIG_HOME=str(home),
            TEMP=str(home),
            TMP=str(home),
            LANG="C.UTF-8",
            LC_ALL="C.UTF-8",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GCM_INTERACTIVE="Never",
            NO_PROXY="",
            no_proxy="",
        )
        config = {
            "protocol.allow": "never",
            "protocol.https.allow": "always",
            "http.proxy": f"http://127.0.0.1:{tunnel.port}",
            "http.followRedirects": "false",
            "http.sslVerify": "true",
            "http.version": "HTTP/1.1",
            "http.emptyAuth": "false",
            "credential.helper": "",
            "credential.interactive": "false",
            "core.hooksPath": str(template),
            "fetch.fsckObjects": "true",
            "transfer.fsckObjects": "true",
            "transfer.bundleURI": "false",
            "fetch.uriprotocols": "",
            "gc.auto": "0",
        }
        if self.cafile:
            config["http.sslCAInfo"] = str(self.cafile)
            if os.name == "nt":
                config["http.sslBackend"] = "openssl"
        environment["GIT_CONFIG_COUNT"] = str(len(config))
        for index, (key, value) in enumerate(config.items()):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        return environment, template

    def command(self, arguments, environment, attempt, tick, deadline, allow_failure=False):
        output, errors = attempt / "command.out", attempt / "command.err"
        process = None
        try:
            with output.open("wb") as stdout, errors.open("wb") as stderr:
                process = subprocess.Popen(
                    [self.git, *arguments],
                    env=environment,
                    cwd=attempt,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                while process.poll() is None:
                    tick()
                    if time.monotonic() >= deadline:
                        raise RepositoryError(422, "REPOSITORY_CLONE_TIMEOUT")
                    if output.stat().st_size + errors.stat().st_size > 65536:
                        raise RepositoryError(422, "REPOSITORY_OUTPUT_LIMIT")
                    time.sleep(0.5)
                tick()
                if time.monotonic() >= deadline:
                    raise RepositoryError(422, "REPOSITORY_CLONE_TIMEOUT")
                if output.stat().st_size + errors.stat().st_size > 65536:
                    raise RepositoryError(422, "REPOSITORY_OUTPUT_LIMIT")
            if process.returncode and not allow_failure:
                raise RepositoryError(422, "REPOSITORY_CLONE_FAILED")
            return process.returncode, output.read_bytes()
        finally:
            if process is not None:
                terminate(process)
            output.unlink(missing_ok=True)
            errors.unlink(missing_ok=True)

    def run(self, task_id, token, payload):
        attempt = None
        published = False
        publication_started = False
        tunnel = None
        try:
            if not self.git:
                raise RepositoryError(503, "REPOSITORY_GIT_UNAVAILABLE")
            target = canonicalize(payload["url"])
            self.storage.preflight()
            self.policy.addresses(target)
            attempt = self.storage.attempt(payload["repository_id"], token)
            bare = attempt / "repo.git"
            deadline = time.monotonic() + self.settings.repository_timeout_seconds
            last_heartbeat = 0

            def tick():
                nonlocal last_heartbeat
                self.storage.capacity()
                size = directory_bytes(bare) if bare.exists() else 0
                if size > self.settings.repository_max_bytes:
                    raise RepositoryError(422, "REPOSITORY_SIZE_LIMIT")
                if time.monotonic() - last_heartbeat >= min(
                    1, self.settings.task_heartbeat_seconds
                ):
                    if not self.service.tasks.checkpoint(
                        task_id, token, processed=size, stage="cloning"
                    ):
                        raise ExecutionStopped
                    last_heartbeat = time.monotonic()

            tick()
            with Tunnel(
                target, self.policy, 2 * self.settings.repository_max_bytes, self.connector
            ) as tunnel:
                environment, template = self.environment(attempt, tunnel)
                self.command(
                    [
                        "clone",
                        "--quiet",
                        "--bare",
                        "--no-local",
                        "--no-recurse-submodules",
                        "--template=" + str(template),
                        "--",
                        target.url,
                        str(bare),
                    ],
                    environment,
                    attempt,
                    tick,
                    deadline,
                )
                _, branch = self.command(
                    ["-C", str(bare), "symbolic-ref", "--short", "HEAD"],
                    environment,
                    attempt,
                    tick,
                    deadline,
                    allow_failure=True,
                )
                code, head = self.command(
                    ["-C", str(bare), "rev-parse", "--verify", "HEAD"],
                    environment,
                    attempt,
                    tick,
                    deadline,
                    allow_failure=True,
                )
                if code:
                    _, refs = self.command(
                        ["-C", str(bare), "for-each-ref", "--format=%(objectname)"],
                        environment,
                        attempt,
                        tick,
                        deadline,
                    )
                    if refs:
                        raise RepositoryError(422, "REPOSITORY_HEAD_UNAVAILABLE")
                    # Empty advertisements need not disclose a default branch;
                    # clone's local initial branch is not authoritative metadata.
                    branch = b""
                branch = branch.decode("utf-8", errors="replace").strip() or None
                head = head.decode("ascii", errors="replace").strip() if not code else None
                if (branch and len(branch) > 255) or (
                    head and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head)
                ):
                    raise RepositoryError(422, "REPOSITORY_INVALID_METADATA")
                safe_path(bare)
                size = directory_bytes(bare)
                self.storage.capacity()
                # An interrupted commit can have succeeded on the database server.
                # Retain this attempt if the publication outcome is unknown; deleting
                # it could destroy the repository referenced by a committed result.
                publication_started = True
                published = self.service.publish(
                    task_id,
                    token,
                    {"default_branch": branch, "head_sha": head, "size_bytes": size},
                    bare.relative_to(self.storage.root).as_posix(),
                )
                publication_started = False
                return published
        except ExecutionStopped:
            return False
        except RepositoryError as error:
            self.service.fail(
                task_id, token, tunnel.error if tunnel and tunnel.error else error.code
            )
            return False
        finally:
            if attempt is not None and not published and not publication_started:
                self.storage.remove(attempt)
