# Design

Only online v2 verification installs an attempt-local immutable blob cache around
the existing NativeReader.record calls. Keys are object SHA, with null blobs and
submodule modes bypassed; bytes/oversize sentinels are retained under both byte and
entry limits. Every hit invokes supervision and checks deadline. Context cleanup
restores the original reader and clears retained bytes on success and exceptions.
No commit metadata, patch command, mode verification or DB comparison is skipped.

v2 reads parsed files in groups of at most 100 eligible commit IDs, preserves file
ordinal order, counts files once and rejects more than 10000 before verification.
The new helper source participates in the v2 rule hash; finished old runs remain
immutable, unfinished old rules reject drift. Legacy parser/v1 hashes stay intact.

Real Git fixtures compare entire records, native digests and frozen inputs against
uncached NativeReader. Isolated MySQL probes record query shape and actual fenced
transactions; a second connection proves task/repository locks can be acquired
during native reads. Fault tests preserve atomic freeze rollback and retry. Timings
are observations, not fixed acceptance thresholds or full-scale capacity claims.
