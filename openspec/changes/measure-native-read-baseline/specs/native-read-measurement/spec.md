## ADDED Requirements

### Requirement: Bounded reproducible native read measurement
The probe SHALL bind immutable source metadata, ordered members, current code and limits, report phase timings, command and supervisor counts, sampled process-tree RSS and at least three alternating cached/uncached trials, and reject unequal records, frozen inputs or numeric values/reasons. Unmeasured database, lock, task and semantic paths SHALL remain explicitly unmeasured.

#### Scenario: Equivalent repeated local source
- **WHEN** three interleaved pairs consume the same frozen local Git window
- **THEN** each native/input/numeric digest is checked, individual results and median/range are retained, and uncontrolled OS cache and sampling limits are disclosed

#### Scenario: Invalid or unsafe source
- **WHEN** the source seal, members, path, object storage, deadline, capacity or RSS violates its contract
- **THEN** the probe fails or stops expansion without overwriting old evidence or granting product readiness

#### Scenario: Incomplete stage coverage
- **WHEN** only offline read and numeric stages were measured
- **THEN** online database and full Worker throughput are not certified and later measurement remains planned

### Requirement: Final native reader supervision
The production native reader SHALL check supervision, deadline and both output bounds after child exit and closed output handles before accepting any bytes or status. Existing allow-limit semantics SHALL remain bounded without bypassing error or time checks.

#### Scenario: Terminal stdout or stderr burst
- **WHEN** the final poll flushes bytes beyond an output bound
- **THEN** output is rejected, or oversized stdout returns the existing allow-limit sentinel, and temporary streams are removed

#### Scenario: Terminal deadline or cancellation
- **WHEN** the child exits concurrently with deadline expiry or supervision revocation
- **THEN** success is refused and temporary resources are cleaned

#### Scenario: Valid terminal result
- **WHEN** output and time bounds hold and supervision remains valid
- **THEN** original bytes and allowed exit statuses are preserved
