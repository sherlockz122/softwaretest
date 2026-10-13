"""Read-only, bounded public GitHub Issue adapter; no redirects, credentials or proxy env."""

import http.client
import json
import ssl
import time
from datetime import UTC, datetime

from packages.repositories.safety import RepositoryError, Target, URLPolicy, canonicalize
from packages.repositories.transport import Tunnel, connect_ip

ADAPTER_VERSION = "github-public-issue-v1"


def repository_name(url):
    target = canonicalize(url)
    return target.path[1:-4] if target.host == "github.com" else ""


def observation(status, number, **values):
    return {
        "provider": ADAPTER_VERSION,
        "number": number,
        "status": status,
        "observed_at": datetime.now(UTC).isoformat(),
        **values,
    }


class GitHubIssues:
    def __init__(
        self, policy=None, connector=connect_ip, context_factory=ssl.create_default_context
    ):
        self.policy, self.connector, self.context_factory = (
            policy or URLPolicy(),
            connector,
            context_factory,
        )

    def lookup(self, canonical_url, number):
        repo = repository_name(canonical_url)
        if not repo:
            return observation("unsupported", number)
        target = Target(
            "https://api.github.com", "api.github.com", f"/repos/{repo}/issues/{number}"
        )
        connection = None
        try:
            with Tunnel(target, self.policy, 2 * 1024**2, self.connector) as tunnel:
                connection = http.client.HTTPSConnection(
                    "127.0.0.1", tunnel.port, timeout=3, context=self.context_factory()
                )
                connection.set_tunnel(target.host, 443)
                connection.request(
                    "GET",
                    target.path,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "DefectGuard-Issue-Evidence",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                if response.status != 200:
                    return observation(
                        "rate_limited"
                        if response.status in {403, 429}
                        else "redirect_rejected"
                        if 300 <= response.status < 400
                        else "unavailable",
                        number,
                    )
                if response.getheader("Content-Type", "").split(";")[0] != "application/json":
                    return observation("invalid_response", number)
                data, deadline = bytearray(), time.monotonic() + 4
                while time.monotonic() < deadline:
                    chunk = response.read1(min(65536, 1024**2 + 1 - len(data)))
                    data.extend(chunk)
                    if len(data) > 1024**2:
                        return observation("response_limit", number)
                    if not chunk:
                        break
                else:
                    return observation("timeout", number)
                obj = json.loads(data)
                if (
                    not isinstance(obj, dict)
                    or obj.get("number") != number
                    or not isinstance(obj.get("labels"), list)
                ):
                    return observation("invalid_response", number)
                if "pull_request" in obj:
                    return observation("pull_request", number)
                bug = any(
                    isinstance(label, dict) and label.get("name", "").casefold() == "bug"
                    for label in obj["labels"]
                )
                return observation("bug" if bug else "not_bug", number, bug_label=bug)
        except (
            RepositoryError,
            OSError,
            http.client.HTTPException,
            ValueError,
            TypeError,
            AttributeError,
        ):
            return observation("unavailable", number)
        finally:
            if connection is not None:
                connection.close()
