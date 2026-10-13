## ADDED Requirements

### Requirement: Bounded supervised immutable blob reuse
Online v2 feature verification SHALL reuse immutable blob results only within an
attempt, bounded by 8 MiB payload and 512 entries, while preserving native record
comparison, mode proof, all current limits and the unchanged v1 parser.

#### Scenario: Recurring blobs match uncached verification
- **WHEN** real Git edits/reverts share blobs across different commits
- **THEN** complete records, native digests and frozen inputs match the uncached
  reader exactly and measured command counts show actual reuse

#### Scenario: Cache hit cancellation and cleanup
- **WHEN** cancellation, lease loss, deadline or capacity failure occurs on a hit
- **THEN** supervision interrupts before returning cached data and the context
  restores the original reader without retaining the attempt's blobs

#### Scenario: Modes and oversized blobs
- **WHEN** null/submodule modes, oversized blobs or cache eviction are encountered
- **THEN** original content semantics are preserved and both cache limits hold

### Requirement: Bounded parsed projections and fenced atomic freezing
Online v2 feature planning SHALL load parsed-file projections in groups of at most
100 eligible commits, preserve ordinal order, release locks before Git/CPU work,
and publish the complete frozen input population in one fenced transaction.

#### Scenario: Query and lock evidence
- **WHEN** an isolated real MySQL/Git feature run is measured
- **THEN** query counts and fenced transaction durations are recorded and another
  connection can immediately lock task/repository rows during native reading

#### Scenario: Freeze interrupted before commit
- **WHEN** a write fails or a lease expires while freezing inputs
- **THEN** all inputs and input/source digests roll back together and retry can
  freeze a complete population without duplicates or publishing partial quality

#### Scenario: Existing sources and rules remain fenced
- **WHEN** stored Git projections, frozen inputs or implementation rules drift
- **THEN** verification rejects publication and finished older runs are not rewritten
