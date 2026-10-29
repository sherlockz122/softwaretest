# Reliable semantic asset verification tasks

## Why
Stage30 provides immutable, bounded research asset browsing and an operator installer. It does not persist a verification task, expose cancellation/recovery, or recover a publication lock after the installer is killed. These are concrete reliability prerequisites for later semantic preparation and training, not evidence of qualified training input or utility.

## What Changes
- Add A38, an authenticated Member+ idempotent request to verify an existing asset ID with a pinned receipt, consumer version and executable-source identity. Reuse async_task/outbox/leases/owner-or-Admin cancellation/retry/audit; do not create a parallel task state machine or a model.
- Verify bounded payloads in the Worker with cooperative deadline and lease checks, publish a minimal snapshot result only in the valid execution-token transaction, and reject drift, old executables, expired tokens and cancelled attempts. Recovery restarts complete verification under the original binding; no partial success or qualification promotion.
- Add task creation from the asset page, type filtering in the task center, and asset navigation/results on the task page, retaining memory-only authentication and safe re-login.
- Replace new installer publication locking with a persistent OS-held nonblocking lock and bounded owner metadata. Process exit releases the OS lock; the lock path is never deleted automatically. Unknown legacy stage30 locks fail closed for operator diagnosis. Verify real cross-process contention/kill/reacquisition and immutable asset preservation on Windows and Linux.

## Impact
US-12/21/24/25, NFR-01/02/04/05 prerequisites; API/Worker/UI/Compose/tests/OpenAPI, no new dependency or migration (0011). No new source/weight download, model fit, old test scoring, old scientific source or asset rewrite, or training/candidate/historical/type/language eligibility. Research input support, license/expert/independent evaluation and semantic utility remain the next substantive data/model work, not replaced by repeated verification batches.
