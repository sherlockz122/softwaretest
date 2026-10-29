"""Shared task-kind inventory for execution, filtering and safe evidence."""

TYPES = frozenset(
    {
        "diagnostic",
        "dataset.build",
        "model.train",
        "semantic.verify",
        "repository.clone",
        "repository.parse",
        "repository.sync",
        "repository.fix",
        "repository.szz",
        "repository.feature",
    }
)
