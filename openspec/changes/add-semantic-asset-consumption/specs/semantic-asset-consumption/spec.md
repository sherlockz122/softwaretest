## ADDED Requirements

### Requirement: Bounded versioned asset admission
The system SHALL admit only explicitly bound v5 fixed660 artifacts into a new immutable versioned read catalog, preserving old files and restrictions.

#### Scenario: Exact readback and independent restore
- **WHEN** an operator installs an identified valid v5 artifact twice and restores its catalog in an independent runtime root
- **THEN** receipt identity and all original payload bytes are unchanged, all660 identities/partitions/coverage/masks are exact, and restore requires no original paths, weights or Torch

#### Scenario: Invalid or drifting inputs
- **WHEN** paths/links, seals, schemas, duplicate keys, sizes, headers, dtype/order/shape/body length, nonfinite values, identities/partitions/coverage/masks or beginning/end bytes drift
- **THEN** admission fails within explicit bounds, no partial directory is published, existing data is preserved and errors do not disclose paths or source contents

#### Scenario: Bounded resources and interruption
- **WHEN** a declared length is excessive or the deadline/cancellation/capacity guard fires during read/copy/verification
- **THEN** the operation rejects before excessive allocation or publication, retains failure evidence and can restart with an independent attempt under unchanged limits

### Requirement: Authenticated restricted research browsing
The application SHALL expose A36/A37 and a usable paginated page that distinguishes readable assets from qualified training or published models.

#### Scenario: Read-only safe navigation
- **WHEN** an authenticated Viewer, Member or Admin lists assets, refreshes and opens bounded member pages
- **THEN** the page shows identity/version/provenance, fixed partition counts, readable restrictions and stable member/coverage details without vectors, paths, raw text or publication controls

#### Scenario: Full reload requires safe re-login
- **WHEN** a user fully reloads an authenticated semantic asset detail page
- **THEN** authentication returns to the login page and a successful re-login returns to the allowlisted original asset path, while external/lookalike/array return values fall back internally and tokens remain in memory

#### Scenario: Missing, unavailable and invalid requests
- **WHEN** an anonymous or disabled session, malformed ID/pagination, missing or corrupted asset is requested, or the catalog is empty
- **THEN** existing authentication and safe errors apply, no server path is accepted, and the UI provides empty/loading/error/retry states without stale records after ID/filter changes

#### Scenario: Qualification is not inferred
- **WHEN** a structurally valid asset is installed or browsed
- **THEN** training/candidate/historical/type/language eligibility and old readiness remain unpromoted, open source/license/expert/utility/independent-evaluation requirements remain visible and no classifier is fitted
