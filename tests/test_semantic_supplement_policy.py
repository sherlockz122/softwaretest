"""Independent fixed-source trust and budget counterexamples, before implementation."""

import pytest

from packages.semantic import supplement_policy as policy
from packages.semantic.policy import SemanticError

COLLECTIONS = "5fc937c676f9c3dfe018db7cae9a2d57d33cae4e"


@pytest.mark.parametrize(
    "repository,head",
    [
        ("https://github.com/apache/commons-collections.git", COLLECTIONS),
        ("apache/commons-lang", COLLECTIONS),
        ("apache/commons-collections/../commons-codec", COLLECTIONS),
        ("APACHE/commons-collections", COLLECTIONS),
        ("apache/commons-collections", "0" * 40),
        ("apache/commons-collections", COLLECTIONS.upper()),
        ("apache/commons-collections", COLLECTIONS + " --upload-pack=evil"),
        ("apache/commons-codec", COLLECTIONS),
        (None, COLLECTIONS),
        ("apache/commons-collections", None),
    ],
)
def test_unknown_or_mismatched_source_cannot_authorize_itself(repository, head):
    with pytest.raises(SemanticError, match="SUPPLEMENT_SOURCE_NOT_APPROVED"):
        policy.approved_source(repository, head)


def test_returned_approval_is_detached_and_only_fixed_https_identity():
    first = policy.approved_source("apache/commons-collections", COLLECTIONS)
    assert first["url"] == "https://github.com/apache/commons-collections.git"
    first["head"] = "0" * 40
    assert policy.approved_source("apache/commons-collections", COLLECTIONS)["head"] == COLLECTIONS


def test_declared_acquisition_limits_are_finite_and_not_training_qualification():
    limits = policy.acquisition_limits()
    assert limits == {
        "git_bytes": 268435456,
        "transfer_bytes": 536870912,
        "seconds": 300,
        "rss_bytes": 1073741824,
        "log_bytes": 65536,
        "metadata_bytes": 4194304,
    }
    limits["seconds"] = 999999
    assert policy.acquisition_limits()["seconds"] == 300


def test_existing_historical_source_is_not_reapproved_as_new_source():
    with pytest.raises(SemanticError, match="SUPPLEMENT_SOURCE_NOT_APPROVED"):
        policy.approved_source("apache/activemq", "13def043d6f00d7adbfa14b9b083141afc82b82a")
