## ADDED Requirements

### Requirement: Pinned acquisition SHALL preserve source identity
The offline acquisition SHALL verify exact fixed revision, sizes, Git blob and SHA256, store a timezone-aware actual bundle acquisition time, reject unsafe paths and preserve an existing valid manifest. Upstream code SHALL never execute.

#### Scenario: Reuse or tamper
- **WHEN** a completed source bundle is reused or its bytes or acquisition metadata change
- **THEN** valid reuse preserves its acquisition time and tampering is rejected without silently replacing it

### Requirement: Audit SHALL distinguish candidate membership from certified labels
The independent audit SHALL retain cohort aliases, missing raw sources, duplicate and inverted clocks, raw-linked published clean rows, missing positive provenance and non-earliest selected fixes. Historical knowledge times SHALL remain unknown without evidence.

#### Scenario: Filtered clean overlaps raw SZZ
- **WHEN** a published clean row appears in the raw bug candidates
- **THEN** it enters an explicit review stratum without automatic relabeling or negative coverage certification

#### Scenario: Selected repair differs from first raw link
- **WHEN** the published repair matches a later raw link
- **THEN** the selected match and earliest difference are both retained and neither becomes a historical available_at

### Requirement: Evidence publication SHALL be bounded and immutable
The audit SHALL supervise byte/row/deadline limits and cancellation, reject input mutation and malformed schemas, return independent deterministic reports with hashes and stratified review packets, and never overwrite existing inputs or reports.

#### Scenario: Interrupted or mutated source
- **WHEN** a source changes, a supervisor cancels, or a resource limit is exceeded
- **THEN** no audit report is published and existing evidence is preserved

#### Scenario: Research provenance is not product readiness
- **WHEN** all matching checks pass but native ancestry, filter replay, complete negative coverage, gold review or compatible fourteen features are missing
- **THEN** those deficits remain explicit and product training and historical replay readiness are false

### Requirement: Imported legacy account entry SHALL refuse unsupported provisioning
The imported retired provision entry SHALL be importable without account or database side effects, exit with code 2 and point to the current explicit bootstrap command. Existing bootstrap semantics SHALL remain unchanged.

#### Scenario: Invoke retired seed
- **WHEN** the old packages.persistence.seed entry is invoked
- **THEN** it rejects without silently translating configuration, resetting credentials, changing roles or creating an account
