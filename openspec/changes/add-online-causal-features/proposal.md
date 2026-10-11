# Change: Online causal feature version and accepted Git binding

## Why
The independent v2 kernel needs durable online integration, while complete negative label provenance remains unverified. Mixing this integration with dataset readiness would overstate the evidence.

## What Changes
- Add explicit v2 selection to existing feature API, durable tasks and quality page, retaining v1 default API and rounds.
- Certify frozen feature inputs against accepted native Git records; reject drift and preserve minimal message hashes.
- Keep label coverage and product readiness false; defer dataset A17–19 and full negative source certification.

## Impact
Existing feature service, A15/A16/OpenAPI and frontend; schema0008_features unchanged. No new dependency or model training.
