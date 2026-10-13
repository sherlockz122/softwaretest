# Index and compatibility

Build a per-path event index from frozen inputs in committer/SHA order. Each target
intersects events with its strict DAG ancestors. Reverse traversal preserves the
legacy commit/file ordering; rename follows old paths only before the matching
commit. Addition/copy ends a lifetime and deletion stops traversal. Unrelated
branches, future events and unsupported ancestors retain legacy exclusion/null rules.

Use legacy current-content calculation and recompute the six history fields with
indexed lineage. Keep stable fsum order and exact source canonicalization. Source
hash for v2 includes the new module; no stored quality/vector is rewritten. Preserve
1000 commits, 10000 files, 32 MiB frozen input and 30-second calculation bounds.

Acceptance compares whole FeatureVector tuples against the independent unchanged
legacy oracle across deterministic randomized DAG/lifecycle/time cases and real
Git fixtures. Count supervised work as well as recording actual timings; avoid a
flaky absolute-speed assertion. Real Worker recovery and fresh browser regressions
continue. Cancel/capacity exceptions must escape without partial publication.

First probe exposed dense-path regression. Final selection reuses a sorted prefix
only for a proved full linear chain and keeps the legacy vector scan for frequent
paths (maximum path event count above population/4). This frozen-input strategy
is validated against the same legacy oracle; it is a performance choice, not a
semantic or coverage change. General DAG/author/file worst-case costs remain.
