# Change: bounded semantic hunk coverage and window probe

## Why
Stage24 retained 136/660 train/validation records with unrepresented candidate hunks. Stage25 supplied the required native-read baseline. Increasing a selected text budget may improve coverage, but cannot establish full-change coverage, model utility or product readiness.

## What Changes
- Predeclare a deterministic label-independent stratified subset: train quotas10/24/20/9/1 and validation6/14/8/3/1 across hunk bins1,2-8,9-32,33-128,129-256, at most64/32; reject duplicates, wrong partitions and unavailable strata rather than substituting easy records.
- Add an independent offline v3 probe: preserve v2 code/artifacts, recompute selected v2 whole-line contexts exactly from bound Git objects, then use a fixed1008-token code-role budget,128-token windows with up to8 per code role. Keep message at the old2-window policy, frozen weights, CPU4/batch8 and content-token weighted pooling.
- Seal selection before Git/tokenizer/model computation. Record original full-population and selected-stratum impact, whole-line hunk representation, eligible/capped line coverage, omitted tokens, resource limits and exact numeric reproduction. No classifier fitting, prediction, oldtest analysis, label rewrite or readiness promotion.
- Add independent root/CPU tests, CI coverage, immutable source/weight bindings, isolated restoration and normal workflow closure. Document TUN compatibility based on actual DNS and clone results without changing global network configuration or SSRF checks.

## Impact
New offline modules/CLI and tests; CI adds the new CPU test file. Existing frozen pipelines, dependencies, models, datasets, API/schema and formal reviews remain unchanged. Current population support, provenance, licensing, Gold, contamination, raw-artifact determinism and product-task gates remain open.
