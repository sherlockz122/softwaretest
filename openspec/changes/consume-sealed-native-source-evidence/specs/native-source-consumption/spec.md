## ADDED Requirements

### Requirement: Explicit technical historical approval
The reader SHALL admit historical inputs only through its versioned trusted technical read catalog, binding the expected manifest, producer execution/source descriptors and applicable recorded safety policy. This approval SHALL grant historical data reading only.

#### Scenario: Approved original and Source6 input
- **WHEN** the caller selects the approved Stage32 v1 record or Source6 v3 record and supplies its exact closed data
- **THEN** the reader verifies every required payload and descriptor, and Source6 additionally requires the explicitly supplied approved original audit closure
- **AND** hashes alone do not certify actual execution, independent human review, Gold labels, training, historical prediction or candidate release

#### Scenario: Self-approved or unsupported evidence
- **WHEN** the caller supplies an unknown approval, unsupported producer, re-sealed alteration, mismatched producer/test/platform descriptor or the unapproved v2 result
- **THEN** historical admission is refused without a fallback, trusted-record modification, producer-code execution or qualification promotion

### Requirement: Full data semantics and producer-history preservation
The reader SHALL independently rederive and compare all supported observation, target, horizon and support results under the known producer semantics while preserving recorded history.

#### Scenario: Complete original and all-parent projection
- **WHEN** an approved full audit is read
- **THEN** all observation identities/order and metadata, original target roles/reservations/window, borrowed records, parent consensus/ambiguity/global and target gaps, outcomes and support are reverified
- **AND** missing evidence remains Unknown; acquisition/availability, producer Git/Python/source/test/platform values and original files are not rewritten

#### Scenario: Historical cached execution
- **WHEN** the audit embeds its approved safety and benchmark closure
- **THEN** all six ordered runs, starts, full original-eligibility unit payloads, hashes, cleanup/resources and the recorded decision are rederived
- **AND** JUnit is interpreted against the approved historical platform/test policy, without reading or running developer tests in the consumer image
- **AND** the recorded historical speed/cache decision confers no current-environment cache permission

### Requirement: Separate current verification and safe failure
The reader SHALL keep current verification distinct from historical admission and SHALL preserve the existing current-source, test and platform checks.

#### Scenario: Current proof mismatch
- **WHEN** an old or incompatible artifact is requested in current mode
- **THEN** the existing current verifier rejects the mismatch, publishes no success, and never silently retries in historical mode

#### Scenario: Explicit rollback
- **WHEN** historical consumption is unavailable or fails
- **THEN** prior artifacts, the installed660 consumer and existing supported uncached/current paths remain unchanged; no check is disabled to force adoption

### Requirement: Bounded untrusted data and read-only operation
The reader SHALL bound input inventory, bytes, record sizes, wall time and sampled memory, reject unsafe paths and malformed JSON/XML, and revalidate inputs before accepting a terminal result.

#### Scenario: Oversize malformed missing or linked input
- **WHEN** inputs contain missing/extra files, links/reparse points, invalid names/types/seals, duplicate JSON keys, non-finite values, XML entities, excessive records or declared/actual sizes
- **THEN** the reader refuses safely within the declared budget, reports only safe codes and leaves originals intact

#### Scenario: Cancellation resource refusal and concurrent drift
- **WHEN** supervision is revoked, the wall/memory/storage budget is unsafe or an input changes during reading
- **THEN** acceptance fails without successful publication, raw payload/path/credential disclosure, subprocess/network activity or modification of the approved input

### Requirement: Actual Linux and engineering delivery
The implementation SHALL be verified with the real approved40-payload nested closure in the bare Linux application image, with copied-root/repeat equality and actual failure cases.

#### Scenario: Bare application image
- **WHEN** the approved Windows-produced data are read inside the actual Linux application image with networking disabled and developer tests absent
- **THEN** the historical result agrees with independently checked outcomes/support and unchanged file hashes, while current-mode incompatibility remains explicit
- **AND** no producer code or developer test is copied into that image or executed from the data

#### Scenario: Complete bounded delivery
- **WHEN** full backend/CPU/frontend/new-volume, normal-data/model/review/user/A-B protection and independent restore, original ordinary storage checks and both own-SHA full CIs actually pass
- **THEN** a fresh bounded batch handoff may be recorded, without claiming whole-product or manual-demo completion
