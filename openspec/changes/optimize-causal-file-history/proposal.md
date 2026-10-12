# Bounded v2 file-history indexing

Repeated scans of unrelated ancestor files obstruct larger source certification.
Index file events for v2 calculation while preserving the legacy numeric values,
missing reasons and base source digest exactly. Keep v1 implementation untouched.
Include the optimization source in the v2 rule hash; existing runs remain immutable
and stale unfinished runs reject rule drift rather than mixing implementations.

This batch does not raise limits or implement label certification, A17–19 or training.
Ancestry/author decay remain quadratic in the worst case. Native Git reads and DB
transaction duration require separate measurement before larger collection.

First probe exposed dense-path regression. Final selection reuses a sorted prefix
only for a proved full linear chain and keeps the legacy vector scan for frequent
paths (maximum path event count above population/4). This frozen-input strategy
is validated against the same legacy oracle; it is a performance choice, not a
semantic or coverage change. General DAG/author/file worst-case costs remain.
