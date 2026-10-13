## ADDED Requirements

### Requirement: Indexed calculation SHALL preserve causal semantics
The v2 kernel SHALL use only frozen native-bound inputs and SHALL preserve all
legacy numeric values, missing reasons and base source digest under its limits.

#### Scenario: Branch and lifecycle equivalence
- **WHEN** inputs contain fork/merge, rename/copy, deletion/re-addition, timestamp ties or inversion
- **THEN** indexed output exactly equals legacy historical numeric calculation before the v2 message FIX replacement

#### Scenario: Unsupported and bounded input
- **WHEN** history is a recent window or contains unsupported content, or the caller cancels
- **THEN** legacy missing reasons remain and cancellation propagates without publication

### Requirement: Implementation provenance SHALL remain explicit
The v2 rule hash SHALL bind index code. The v1 source and stored runs SHALL remain immutable.

#### Scenario: Resume after rule drift
- **WHEN** an unfinished task's frozen v2 rule differs from current implementation
- **THEN** existing source-conflict handling rejects it without rewriting old quality or mixing new vectors

#### Scenario: Performance evidence
- **WHEN** a 200/1000 commit sparse and dense probe is run
- **THEN** exact output equivalence and measured timing/work are retained, with quadratic ancestry/author and native Git/lock limitations disclosed
