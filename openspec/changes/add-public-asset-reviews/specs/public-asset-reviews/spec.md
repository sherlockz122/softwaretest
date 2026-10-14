## ADDED Requirements

### Requirement: A pinned public CSV asset preserves original and derived inputs
The offline system SHALL publish a bounded immutable ApacheJIT asset with original bytes, acquisition and license provenance, adapter version, a deterministic twelve-numeric derived view and independent capability deficits.

#### Scenario: A saved asset is loaded again
- **WHEN** an existing complete asset is loaded or the identical acquisition is imported again
- **THEN** the system verifies content-addressed identity and all file hashes and preserves original acquisition and first storage metadata
- **AND** unknown type/location, unverified per-row language and unavailable semantic/historical/product capabilities are not fabricated

#### Scenario: The source or stored artifact drifts
- **WHEN** input bytes, manifest, derived contents or inventory no longer match the expected asset identity
- **THEN** the system refuses reuse or publication with a safe diagnostic
- **AND** the prior complete asset and original source are not overwritten

### Requirement: Publication and reading obey explicit resource and path limits
The offline system SHALL enforce runtime-only paths without links or reparse points, limits on bytes/rows/fields, supervised deadlines/cancellation and capacity checks, and complete publication before an asset becomes loadable.

#### Scenario: Concurrent imports target the same asset
- **WHEN** two import attempts publish identical content
- **THEN** only a verified complete asset is reused and its first storage timestamp is preserved
- **AND** temporary files belonging to failed attempts do not replace an accepted asset

#### Scenario: A limit or cancellation interrupts processing
- **WHEN** an input exceeds limits or the operator cancels a supervised operation
- **THEN** no partial asset is accepted and existing assets remain intact

### Requirement: Review records bind evidence without certifying labels
The offline system SHALL preserve an immutable review revision chain bound to a verified source report, asset, packet member and evidence hashes, with actual review time and unresolved status.

#### Scenario: A review cannot establish label validity
- **WHEN** native objects, filtering or negative coverage evidence is missing or conflicting
- **THEN** the review retains pending or unresolved status and reasons
- **AND** it does not rewrite original labels, historical availability, type labels, gold status or existing readiness flags

#### Scenario: An evidence or review predecessor is altered
- **WHEN** evidence contents, packet identity, revision or predecessor digest differs from the bound chain
- **THEN** the system rejects the review update or reload rather than silently accepting a new interpretation

#### Scenario: A non-shallow repository does not certify a negative ancestry result
- **WHEN** both endpoint objects exist but merge-base returns 1 or 128 without independently verified graph completeness
- **THEN** the probe records unknown graph evidence rather than a certified absence of ancestry
- **AND** the evidence output cannot be written inside a preserved asset namespace
