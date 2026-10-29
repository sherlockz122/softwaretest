## ADDED Requirements

### Requirement: Persisted bound integrity verification
The system SHALL accept idempotent Member or Admin verification requests for an installed semantic asset and reuse the existing durable task framework without promoting its qualification.

#### Scenario: Active authorization, replay and safe inputs
- **WHEN** an active Member or Admin submits an existing asset ID with an idempotency key, or replays a lost response
- **THEN** one task with fixed asset/receipt/rule/executable binding is queued through the outbox and replayed, changed input conflicts, and anonymous/disabled/Viewer, malformed IDs, arbitrary paths and extra body input are rejected without asset writes

#### Scenario: Snapshot verification and privacy
- **WHEN** the Worker completes bounded full payload validation under the original binding
- **THEN** the valid token transaction records dimensions, partitions, hashes, timestamp and restricted capabilities plus an audit, without text, vectors, paths, secrets, training or publishing eligibility

#### Scenario: Drift, cancellation and recovery
- **WHEN** files, receipt, version or executables drift, cancellation wins, a token/lease expires, publication rolls back, or a Worker is killed
- **THEN** no invalid or partial success is visible, safe errors or existing lease recovery apply, old tokens cannot publish, and a bounded successor re-verifies the same immutable input without modifying old data

### Requirement: Safe publication lock lifecycle
The operator installer SHALL arbitrate publication with a persistent OS-held lock and preserve existing assets during contention and process failure.

#### Scenario: Independent contender and killed holder
- **WHEN** separate processes compete for the publication lock and the holder is forcibly terminated
- **THEN** the contender is safely rejected while held, the OS releases the lock after exit, a new process can acquire it, and no lock-path unlink or asset modification is required

#### Scenario: Unknown legacy or unsafe lock
- **WHEN** a legacy lock, link, oversized or otherwise unsafe lock path is present
- **THEN** publication fails closed and preserves evidence and assets, without automatic age/PID-based deletion, system permission changes or mixed-version writer compatibility claims

### Requirement: Usable verification workflow
The authenticated application SHALL connect semantic assets and durable task controls while preserving safe navigation and limited use.

#### Scenario: Member task navigation and refresh
- **WHEN** a Member starts verification, navigates through task type/status filters, refreshes and safely re-authenticates, or cancels/retries an owned task
- **THEN** existing task progress/results/relationships remain readable, successors and the original asset are reachable, and operation keys prevent lost-response duplicates

#### Scenario: Viewer, failure and narrow layout
- **WHEN** a Viewer browses, an operation fails or a route changes during loading, or the viewport is narrow
- **THEN** no unauthorized create control or stale result appears, safe errors have actionable context, and page overflow is contained without hiding valid task/asset information
