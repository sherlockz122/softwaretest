# Bounded native feature reads and atomic freeze measurement

Before expanding real source acquisition, measure supervised Git command counts,
parsed-file query counts and fenced transaction duration. Reduce repeated immutable
blob reads with an attempt-local bounded cache and load v2 FileChange projections
in bounded groups, preserving native comparison and original per-commit order.

Measure atomic input freezing before deciding whether bulk insertion is warranted.
Do not split the freeze into externally visible partial transactions or move Git
and CPU callbacks under task/repository locks. Preserve v1, accepted heads, all
current resource limits, source hashes, cancellation, stale leases and recovery.
No new endpoint/schema/dependency, source certification, dataset or training.
