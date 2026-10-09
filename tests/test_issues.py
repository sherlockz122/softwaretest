import ssl

import pytest

from packages.repositories.safety import URLPolicy
from tests.issue_support import https_issues


@pytest.mark.parametrize(
    "status,body,content_type,expected",
    [
        (200, {"number": 18, "labels": [{"name": "Bug"}]}, "application/json", "bug"),
        (200, {"number": 18, "labels": [{"name": "enhancement"}]}, "application/json", "not_bug"),
        (
            200,
            {"number": 18, "labels": [{"name": "bug"}], "pull_request": {}},
            "application/json",
            "pull_request",
        ),
        (200, {"number": 19, "labels": [{"name": "bug"}]}, "application/json", "invalid_response"),
        (200, b"invalid-json", "application/json", "unavailable"),
        (200, "oversized", "application/json", "response_limit"),
        (200, {}, "text/html", "invalid_response"),
        (302, {}, "application/json", "redirect_rejected"),
        (403, {}, "application/json", "rate_limited"),
        (429, {}, "application/json", "rate_limited"),
        (404, {}, "application/json", "unavailable"),
    ],
)
def test_real_tls_public_issue_adapter_safety_and_classification(
    tmp_path, status, body, content_type, expected
):
    with https_issues(tmp_path / "tls") as (adapter, state):
        if body == "oversized":
            body = b"x" * (1024**2 + 1)
        state.update(status=status, body=body, content_type=content_type)
        result = adapter.lookup("https://github.com/team/demo.git", 18)
        assert result["status"] == expected
        assert "body" not in result and "labels" not in result
        assert (
            len(state["requests"]) == 1 and state["requests"][0][0] == "/repos/team/demo/issues/18"
        )
        headers = state["requests"][0][1]
        assert "Authorization" not in headers and "Cookie" not in headers
        assert (
            headers["Host"] == "api.github.com" and headers["X-GitHub-Api-Version"] == "2022-11-28"
        )


def test_adapter_rejects_untrusted_tls_private_dns_and_unsupported_provider(tmp_path):
    with https_issues(tmp_path / "tls") as (adapter, state):
        adapter.context_factory = ssl.create_default_context
        assert adapter.lookup("https://github.com/team/demo.git", 18)["status"] == "unavailable"
        adapter.policy = URLPolicy(lambda host: ["93.184.216.34", "127.0.0.1"])
        assert adapter.lookup("https://github.com/team/demo.git", 18)["status"] == "unavailable"
        assert not state["requests"]
        assert adapter.lookup("https://git.example/team/demo.git", 18)["status"] == "unsupported"
        assert not state["requests"]
