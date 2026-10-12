## ADDED Requirements

### Requirement: Explicit compatible online versions
The system SHALL support v1 and v2 feature runs, preserve the default v1 API, and explicitly select v2 by default in the page without mixing frozen results or Fix semantics.

#### Scenario: Later Fix review
- **WHEN** historical and candidate v2 runs are calculated before or after a later human Fix review
- **THEN** their message-purpose feature inputs remain identical, no fix_run_id is accepted and zero indicates only a heuristic non-match

### Requirement: Accepted native Git binding
The system SHALL certify all consumed parsed DB projections against the accepted private native Git snapshot before freezing v2 inputs and SHALL distinguish feature population coverage from independently certified label knowledge.

#### Scenario: Parsed record drift or full gap
- **WHEN** a consumed DB message, count, clock or identity differs from native Git, or a full snapshot lacks current parsed records
- **THEN** the run fails with a stable source conflict and no final quality is published

#### Scenario: Label evidence unavailable
- **WHEN** native feature inputs are certified even for a full feature population
- **THEN** label_coverage_complete and product_training_ready remain false and the page states the missing label certification

### Requirement: Frozen reliable recovery
The system SHALL freeze minimal numeric and message proof inputs, bind code/version/snapshot hashes, supervise capacity, preserve existing fencing and publish quality only atomically after all batches.

#### Scenario: Actual Worker interruption
- **WHEN** a Worker is killed after a durable partial batch and a later lease resumes after Fix review changes
- **THEN** the old token cannot publish and the new Worker completes using the same frozen input/source hash without later Fix leakage

#### Scenario: Version incompatible dataset
- **WHEN** the legacy v1 production dataset adapter is asked to consume a v2 feature run
- **THEN** it rejects the source rather than silently granting training eligibility
