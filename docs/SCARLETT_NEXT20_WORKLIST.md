# Scarlett next twenty validation objectives

Scope: concrete independent controls, not a claim of retail rendering.
The aggregate comparison #90 and the prior #85–89 are upstream. The
follow-ups #92–94 and older #60–62 are integrated here experimentally.

| # | Objective | Verified scope / remaining gate |
|---|---|---|
| 21 | Same input | Digest-verified per-pair snapshots in both comparison tools |
| 22 | Validated DXIL | Aggregate full production pipeline, synthetic execution tested |
| 23 | Private report | Fixed buckets; no shader keys, raw errors or output hashes |
| 24 | Regressions | Baseline success to refusal or failure gate |
| 25 | Display priority | Ranked display association; causal title impact unverified |
| 26 | Trace integrity | Per-kind totals, missing/invalid intervals, trigger agreement |
| 27 | Tool failures | Fixed failure buckets, distinct refusal and tool errors |
| 28 | Output changes | Optional identical DXIL gate; differences are not semantic proof |
| 29 | Tool integrity | Pin emitters; before/after DXC/DXV hashes; dependent libraries not pinned |
| 30 | Concurrent cache changes | Per-pair verified copy and fresh cache |
| 31 | Boundary values | Wave finite/infinite and saturation boundaries, CBV negative tests |
| 32 | Reflected descriptors | Nine negative CBV cases; not all resource classes |
| 33 | Graphics association | Stage/trigger validation; actual mixed pipeline linkage remains open |
| 34 | Dependent vector chains | New VOP1/VOP3 saturating pack: 8192 executions O0/O3 |
| 35 | Wave extrema | 128 dispatches / 49152 words; not arbitrary divergence or NaN policy |
| 36 | Parallel vs serial | 96 synthetic requests, 1728 validated outputs, 3.025x median |
| 37 | Reproducibility | Exact private rows and GPU O0/O3 readback digests agree |
| 38 | GPU false positives | Explicit no fallback for positive controls, ten passes |
| 39 | Original files | Fresh reports outside request archive and copied Proton prefix |
| 40 | Acceptance gates | Joint local 405 tests and Windows typecheck; not mandatory upstream CI |

See the individual PR documentation and [joint receipt](../evidence/scarlett-next20-joint.json).
GPU results were obtained on the separate tested builds, not a new GPU run
of the integrated branch. No retail archive was used.
