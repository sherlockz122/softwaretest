## ADDED Requirements

### Requirement: Frozen SZZ input
The system SHALL bind each SZZ run to a completed Fix run, accepted snapshot, versioned policies, review revisions and observation cutoff.

#### Scenario: Concurrent later review
- **WHEN** a Fix assessment changes after SZZ creation
- **THEN** the existing frozen SZZ input remains unchanged and a new run is needed

#### Scenario: Historical cutoff
- **WHEN** evidence becomes available after the requested cutoff
- **THEN** it produces Unknown instead of a historical buggy label

### Requirement: Auditable baseline blame
The system SHALL trace deleted or replaced code lines in the sole parent with bounded Git blame and preserve source evidence and eligibility.

#### Scenario: Direct parent and rename
- **WHEN** blame points to the direct parent or follows a whole-file rename
- **THEN** that source is retained with original line and path evidence

#### Scenario: Unavailable content or history
- **WHEN** the fix is a merge/root or content/history is unavailable or incomplete
- **THEN** the system records explicit Unknown and never infers clean

### Requirement: Recoverable atomic execution
The system SHALL atomically publish bounded result batches with lease fencing and progress and retain completed batches through recovery.

#### Scenario: Worker termination
- **WHEN** a worker terminates after a committed partial batch
- **THEN** a successor resumes frozen inputs with unique links and rejects the old token

### Requirement: Authorized SZZ interface
The system SHALL expose Member/Admin startup and authenticated paginated queries with idempotency and safe DTOs.

#### Scenario: Viewer startup
- **WHEN** a Viewer attempts to start SZZ
- **THEN** startup is denied and no SZZ state is created

#### Scenario: Insufficient project drive
- **WHEN** D cannot support expected growth and emergency reserve
- **THEN** work stops with progress preserved and does not move to C
