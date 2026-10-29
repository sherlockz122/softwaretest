# Versioned semantic asset consumption

## Why
The fixed660 v5 population and both complete frozen encoding passes passed stage29 exact-commit acceptance. Application consumers cannot safely reuse the research reader's1GiB JSON allocation limit, and no authenticated UI currently exposes these assets or their restrictions.

## What Changes
- Install an explicitly identified local v5 embedding artifact into a new immutable semantic-asset-read-v1 catalog using a bounded, cancel/deadline-aware operator command; preserve the original files and seal, without executing producer code or importing Torch.
- Preflight schema, manifest file sizes, NPY type/shape/header/body length before reading data; stream matrix validation, cross-check660 identities/partitions/coverage/masks and verify start/end drift. Use per-format bounds rather than a global4MiB substitution.
- Add authenticated read-only A36/A37 listing/detail APIs and a paginated research asset page. API mount is read-only, inputs are IDs and paging only; no arbitrary server paths, uploads or publishing controls.
- Keep source/label/license/expert/utility/independent-evaluation gates open. Installed means structurally readable, not training or product readiness. Reliable semantic generation/training tasks, data qualification and comparison remain separate next slices.

## Impact
US-24/25/27, NFR-01/02/05; new APIs and OpenAPI, Vue navigation/page, Compose read-only mount, new consumer modules/tests and operator command. No DB migration, new dependency, weight download, model fitting, old source/artifact or label/split rewrite. Only wang ordinary delivery with both exact-SHA CIs.
