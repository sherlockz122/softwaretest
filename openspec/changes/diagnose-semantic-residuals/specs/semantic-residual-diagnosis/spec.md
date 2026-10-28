## ADDED Requirements
### Requirement: Fixed evidence-preserving residual diagnosis
The system SHALL declare all residual members and deterministic available-stratum controls before token-cost analysis, reproduce their existing contexts exactly, and preserve old labels, partitions, manifests, models and readiness.
#### Scenario: Valid declaration
- **WHEN** the canonical96 input has exactly seven residual records
- **THEN** all seven and SHA-first complete controls in available strata are sealed without using labels and with at most17 records
#### Scenario: Drift or unsupported source
- **WHEN** input/source/policy/identity changes, oldtest appears or exact reconstruction fails
- **THEN** diagnosis fails and evidence remains without qualification promotion
#### Scenario: Declaration changes during member read
- **WHEN** source hashes or the verified input manifest drift while declaration members are read
- **THEN** entry and pre-publication snapshots reject the declaration without rewriting old accepted artifacts
#### Scenario: Declaration changes during member read
- **WHEN** source hashes or the verified input manifest drift while declaration members are read
- **THEN** entry and pre-publication snapshots reject the declaration without rewriting old accepted artifacts
### Requirement: Honest bounded coverage explanations
The diagnostic SHALL report whole-line local token minima, candidate caps, checks, baseline floors and shared incidences separately, with finite same-policy replay and a complete-cover witness whose actual joined cost is measured.
#### Scenario: Feasible cover witness
- **WHEN** preserved floor plus deterministic whole-line cover costs no more than1008 joined tokens
- **THEN** feasibility for that role is proven without claiming global optimality, complete change semantics or population support
#### Scenario: Larger witness or exhausted checks
- **WHEN** witness cost exceeds1008 or measured checks reach a cap
- **THEN** the result retains uncertainty and does not infer global impossibility or a sole root cause from local/additive token estimates
### Requirement: Safe failure evidence before disposable cleanup
Fresh acceptance SHALL retain a bounded allowlisted task/root/type/status/error-code summary before removing its own volumes on success or failure, exclude secrets/raw error messages, and execute cleanup despite collector failure.
#### Scenario: Browser or collector failure
- **WHEN** browser acceptance fails or safe evidence cannot be collected
- **THEN** acceptance remains unsuccessful, a safe collection status is retained and only the unique disposable project is cleaned
#### Scenario: Malformed or oversized task data
- **WHEN** task identifiers/codes/schema/output limits are violated
- **THEN** evidence capture fails explicitly without writing untrusted fields or preventing cleanup
