# Context
v2 message features and historical replay are independent kernels. The online service must bind real accepted Git and avoid later Fix reviews leaking into features.

# Decisions
1. Existing API defaults v1, UI defaults explicit v2 with legacy selection. v2 forbids fix_run_id, both purposes use frozen message indicator and hashes; merge encoding uncertainty stays null.
2. Verify accepted HEAD/private storage/reachability/parser/identity and every consumed DB projection against native Git. full population gaps and drift reject before publication.
3. Freeze minimal numeric/message/proof inputs; source hash binds version, bridge source, accepted snapshot and input. Reuse existing atomic batches, tokens, retries, cancellation, idempotency and quality release.
4. Quality separates feature population from label coverage. Label certification/product readiness remain false; old dataset adapter explicitly rejects v2.
5. Keep 1000 commits/10000 files/32MiB frozen inputs/30s numeric computation limits and D storage supervision. Dataset contracts, complete negative provenance and scaling optimizations remain subsequent batches.

# Validation
Real Git/MySQL drift, Fix isolation, cancellation/lease/capacity recovery, rule/gap/RBAC/idempotency/version checks; actual partial durable Worker SIGKILL on v1 400 and v2 120 commits; independent browser mobile and lost response coverage. Final counts and failure disposition are recorded in the stage report.
