# Experimental integrated validation — 2026-11-24

This branch combines pending PRs #44, #48, #51, #58, #60, #61 and #62 with upstream through `cf95792` (which includes #54, #69, #75, #78, #79 and #82). It is not a request to merge all PRs together. No private title or shader data are included.

## Reproduce the same-request comparison

Build the baseline in an independent worktree at `3b1197301d6c68872520bdb8cfa13cd16986eb00` (the issue #39 report), and build the candidate branch with the same Rust toolchain. Use fresh paths and the same DXC/DXV binaries:

```sh
cargo build --release --target x86_64-unknown-linux-gnu --manifest-path tools/shader-compiler/Cargo.toml
python tools/triage_scarlett_shaders.py "$REQUESTS" --aggregate-only \
  --baseline "$BASELINE_XSHADER" --xshader "$CANDIDATE_XSHADER" --jobs 2 \
  --json /tmp/scarlett-paired-aggregate.json
python tools/compare_scarlett_shaders.py "$REQUESTS" \
  --baseline "$BASELINE_XSHADER" --candidate "$CANDIDATE_XSHADER" \
  --dxc "$DXC" --dxv "$DXV" --jobs 4 --require-no-failures \
  --json /tmp/scarlett-private-dxc-comparison.json
```

Keep the archive unchanged and private. The first report uses fixed aggregate labels; the second is local-only and contains per-request identifiers and diagnostics. Emitter acceptance and validated DXIL are not proof of rendering correctness. A malformed synthetic negative fixture is intentionally inconclusive; it must never be counted as a successful translation.

## Runtime validation

Run the existing unit suites, build/restage the providers from a clean tree, and execute the D3D12 tables, stream, heaps and placement controls on the selected adapter. Record the tested commit, GPU, driver, Proton and provider fingerprints. For a later authorized title run on the same hardware, keep the initial archive, cache state, scene, and settings fixed and use the existing diagnostic tools. In particular use `TUXBOX_TRACE_HEAP_IMPORTS=1`, diagnose the new log, and check the first and last observed null substitutions. For aligned SDR reference/candidate PNGs the frame comparison reports the first threshold violation and first candidate-only almost-black frame. Log line order does not establish a causal dependency or precise timing.

## Compilation workers

The synthetic cold compilation measurement is 96 native compute requests, three alternating trials per worker count with fresh per-request caches. All DXIL digests matched across 1, 2 and 4 workers. Median times: 1.4678, 0.7365 and 0.3901 seconds. Four workers are an appropriate measured starting point on this machine for this workload, not a universal optimum or a retail FPS claim. Runtime request concurrency is already implemented upstream (#54); the bounded preparation pool is already four workers.

## Closure gates still open

- Exact AMD import/linkage/embedded-root failures must be reproduced with authorized fixtures. General D3D12 smoke tests and the metadata/cache controls are not substitutes.
- Real coverage needs an unchanged local archive. Do not share the raw per-request report.
- Arbitrary dynamic offsets need verified descriptor/address provenance and bounds; genuinely divergent waves need participation-correct reconvergence and numerical GPU controls.
- The conservative raw-index stride proof preserves a negative control, but may refuse a shader accepted by current upstream. Compare the two on the actual archive before proposing this combined branch for production.
- The first bad aligned frame can be located offline, but proving the first incorrect render pass requires matching captures and pass-level replay.

See the aggregate [receipt](../evidence/scarlett-integrated-20261124-v2.json).
