## ADDED Requirements

### Requirement: Causal message-purpose feature version
The system SHALL use an independent fourteen-feature version with commit-message-only FIX purpose signals and SHALL remove later Fix review data before both numeric computation and hashing, retaining the existing production contract.

#### Scenario: Future confirmation changes
- **WHEN** a later Fix review changes but the fixed Git input and message remain unchanged
- **THEN** the independent causal feature values and content hash remain unchanged

#### Scenario: Missing or unrecognized purpose
- **WHEN** the message is undecodable or a real repair message does not match the heuristic
- **THEN** FIX is respectively null or 0 with explicit heuristic semantics, never an external defect outcome

#### Scenario: Merge parser masks message encoding
- **WHEN** a merge record cannot independently prove message decoding because its status is merge_skipped
- **THEN** FIX remains null with message_unverified, including readable-looking or keyword-containing text, while the existing parser contract remains unchanged

### Requirement: Explicit horizon replay knowledge
The system SHALL declare event clock, actual acquisition, horizon, assumed knowledge delay, label provenance and independently certified source coverage, retaining Unknown when the contract cannot prove a usable outcome.

#### Scenario: Incomplete negatives
- **WHEN** no positive link exists but complete compatible coverage of the horizon is unavailable
- **THEN** the label remains Unknown and cannot be inferred from a published clean flag

#### Scenario: Fixed endpoint and later repairs
- **WHEN** an observed repair occurs after the predeclared horizon
- **THEN** it does not create a positive within that horizon and any eligible negative is explicitly only a horizon endpoint

### Requirement: Immutable split and bounded acceptance
The system SHALL freeze original same-event groups and time split boundaries before purging late labels, use only eligible training rows for fitting and weights, bind source and policy hashes, and supervise resource limits without redirecting work to another drive.

#### Scenario: Late training label
- **WHEN** reconstructed label knowledge is at or after the first validation event
- **THEN** the row is purged from training, original membership is retained and no boundary moves to restore class balance

#### Scenario: Bounded real Git probe
- **WHEN** a controlled partial historical Git window is inspected
- **THEN** unsupported historical features remain missing, incomplete labels remain Unknown, the repository is read only and product training readiness is not claimed
