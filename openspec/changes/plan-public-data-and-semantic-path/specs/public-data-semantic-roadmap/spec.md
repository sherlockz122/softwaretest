## ADDED Requirements

### Requirement: Public source assets preserve original data and reload integrity
The future system SHALL preserve approved public data as immutable source assets with provenance, license scope, format/granularity, adapter version and hashes. Loading an asset SHALL NOT imply eligibility for every training task.

#### Scenario: A valid numerical benchmark has no semantic text
- **WHEN** an approved CSV benchmark is saved and reloaded with matching hashes
- **THEN** its original fields and acquisition metadata remain recoverable
- **AND** semantic or incompatible native-feature training is refused with an explicit capability deficit

#### Scenario: The restored asset differs from its manifest
- **WHEN** bytes, schema or provenance drift during reload
- **THEN** the future system refuses derived publication and retains the prior asset and diagnostic evidence

### Requirement: Label dimensions and domain capability remain distinct
The future system SHALL distinguish binary defect labels, type labels, location, granularity and language/domain evidence, with unknown values and versioned review provenance.

#### Scenario: Binary labels have no defect type evidence
- **WHEN** a public sample contains only buggy/clean information or SZZ candidate lines
- **THEN** its defect type remains unknown rather than an inferred gold category
- **AND** its type or localization prediction capability is not inherited from binary-label availability

#### Scenario: A new language or source uses a different sample unit
- **WHEN** an imported function/module source or unverified language is evaluated
- **THEN** it retains its original granularity/domain and separately reported quality and metrics
- **AND** it cannot silently become a compatible commit-level candidate model

### Requirement: Pretrained semantic and fusion experiments use fair versioned inputs
The future system SHALL evaluate a pinned pretrained semantic encoder and optional fusion with expert features against numerical baselines on the same eligible population and fixed partitions, with input availability, preprocessing, artifact integrity, domain and resource checks.

#### Scenario: Semantic text is recovered for a strict subset
- **WHEN** only some numerical benchmark samples have verified prediction-side text
- **THEN** comparative experiments disclose missing coverage and use a common eligible population
- **AND** future fix text, test-fitted transforms or unverified type labels cannot enter prediction inputs

#### Scenario: A pretrained experiment shows no acceptable benefit or cannot meet resources
- **WHEN** held-out quality, calibration, domain applicability or declared resource constraints are not met
- **THEN** the system retains the valid baseline and records the negative result or limitation
- **AND** it does not certify a model merely because the encoder is modern or more complex

### Requirement: Research engineering and product release use separate gates
The future system SHALL support bounded research engineering without requiring perfect historical labels, while enforcing independently evidenced historical and target-domain product release requirements.

#### Scenario: Published weak labels are available today but historical knowledge is missing
- **WHEN** a dataset passes declared research-purpose checks but cannot prove historical label availability
- **THEN** a research run can proceed under its explicit version and limitation
- **AND** existing false readiness flags remain unchanged and historical/candidate publication does not inherit research eligibility
