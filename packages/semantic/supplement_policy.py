"""Separate controlled inputs for new source evidence; no model qualification."""

from packages.semantic.policy import SemanticError

VERSION = "native-source-supplement-v1"
_SOURCES = (
    ("apache/commons-collections", "5fc937c676f9c3dfe018db7cae9a2d57d33cae4e"),
    ("apache/commons-codec", "315fd72b289a0c47adef636acb637517399a2dfe"),
)


def approved_source(repository, head):
    for name, expected in _SOURCES:
        if type(repository) is str and type(head) is str and (repository, head) == (name, expected):
            return {"repository": name, "head": expected, "url": f"https://github.com/{name}.git"}
    raise SemanticError("SUPPLEMENT_SOURCE_NOT_APPROVED")


def acquisition_limits():
    return {
        "git_bytes": 256 * 1024**2,
        "transfer_bytes": 512 * 1024**2,
        "seconds": 300,
        "rss_bytes": 1024**3,
        "log_bytes": 65536,
        "metadata_bytes": 4 * 1024**2,
    }
