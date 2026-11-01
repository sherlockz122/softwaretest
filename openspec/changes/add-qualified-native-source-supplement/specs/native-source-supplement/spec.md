# Native Source Supplement

## ADDED Requirements

### Requirement: Fixed bounded native source acquisition
The system SHALL perform online acquisition only for the controlled source/head pairs using public-address-pinned HTTPS with TLS verification, no credentials/redirects/upstream execution and bounded owned process supervision. It SHALL publish an immutable checked source receipt with real acquisition time, fixed identity, safe object inventory and original root license evidence; self-sealing SHALL NOT authorize arbitrary sources. User-supplied offline material SHALL follow the distinct offline content admission requirement and SHALL NOT be represented as successful online acquisition.

#### Scenario: Actual fixed source acquired
- **GIVEN** a predeclared approved pair and enough measured D capacity
- **WHEN** safe acquisition completes
- **THEN** the source is bare, non-shallow, without alternates/promisor/grafts/replacement refs, fixed head/object/license hashes match and no checkout or upstream code executes

#### Scenario: Network or resource safety fails
- **WHEN** DNS is non-public, TLS/authority/redirect/identity changes, byte/RSS/wall/output budget fails, cancellation occurs or cleanup cannot be confirmed
- **THEN** acquisition refuses with safe error, retains bounded failure evidence and does not publish success, alter system settings or reuse an unverified source

### Requirement: Independently verified offline fixed-source admission
The system SHALL accept user-supplied offline Git material only for the existing preregistered source/head pairs after bounded safe extraction, complete inventory/hash verification and independent native object/parent/license verification without network, checkout or supplied-tool execution. It SHALL preserve the original archive, external acquisition claims and actual verification time in a distinct immutable provenance record. Offline content verification SHALL NOT certify the external network route, measured transfer budget, original process supervision, training readiness or product completion.

#### Scenario: User supplies complete fixed-object history
- **GIVEN** an explicitly supplied archive containing the existing approved fixed pairs
- **WHEN** isolated clean-config complete-object verification and copied-root replay both pass
- **THEN** downstream source development may use the verified content through a separately tested offline admission path without repeating its network download, while online failures and external provenance differences remain recorded

#### Scenario: An archive or source identity is incomplete or altered
- **WHEN** extraction safety, complete hash coverage, approved identity, object closure, executable binding, license equality, resource supervision or replay fails
- **THEN** offline admission refuses, preserves the original material and bounded failure evidence, and does not execute supplied tools, silently fetch missing objects or substitute self-reported seals for independent checks

#### Scenario: Production offline import and independent copied-root consumption
- **GIVEN** the code-approved original ZIP identity and existing two allowed repository/head pairs
- **WHEN** production import safely streams all checksum-covered members and independently verifies clean-config object copies and copied-root native facts
- **THEN** it atomically publishes a distinct offline receipt with archive/member/object/license/code/Git identity and external/local time bindings, leaves original material unchanged and keeps training/candidate readiness false

#### Scenario: Reuse, drift or interrupted publication
- **WHEN** an import target already exists, input/program/native executable changes, cancellation or a resource/cleanup failure occurs, or a published directory is tampered with
- **THEN** no success is overwritten or partially adopted, bounded failure evidence remains, and explicit consumption rechecks the original approved archive and full native facts instead of authorizing a caller-resealed receipt

### Requirement: Label-blind complete population declaration
The system SHALL freeze complete members, exclusions, roles, clocks and observation window before label/score inspection, separately bind each new source and preserve prior populations. Unsupported scale SHALL be refused; it SHALL NOT silently select members or monkeypatch old fixed-source validation.

#### Scenario: Metadata feasibility before labels
- **WHEN** reachable identities and year counts are inspected
- **THEN** no Fix/SZZ/label/model score selects the source/frame, and any resulting declaration has a complete independently verifiable inventory and explicit limits

### Requirement: Evidence-qualified compatible data
The system SHALL retain all-parent native facts, gaps/Unknown, actual knowledge and mature temporal boundaries and SHALL independently verify complete outcomes. Dataset/training readiness SHALL require compatible actual features, allowed source/use/license, sufficient role support and independent evaluation; new data or technical integrity alone SHALL NOT grant readiness.

#### Scenario: Source support or native14 compatibility is insufficient
- **WHEN** role support, maturity/purge, native14 history/scale/merge policy or another required gate is missing
- **THEN** the original complete population and explicit refusal/worklist remain, no LT or label is invented, no roles are moved and corresponding fitting/promotion is refused pending a versioned verified solution

#### Scenario: Actual source evidence is used downstream
- **GIVEN** applicable source, temporal, feature, support and use gates have all been independently verified
- **WHEN** compatible immutable data is produced
- **THEN** every member/feature/label/exclusion/role and qualification is traceable to the fixed source and executable binding, and old source/current-cache/review/product qualifications remain separate
