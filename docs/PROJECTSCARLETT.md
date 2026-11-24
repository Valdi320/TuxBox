# Set up ProjectScarlett (GTA VI) on Linux

This guide stages GTA VI 1.0.2.1 (`ProjectScarlett`) with TuxBox's providers and starts it on Valve's stock Proton Experimental. The current result is the player selection prompt at 3840×2160. [Title progress](PROGRESS.md) · [Run record](../evidence/projectscarlett-startup.json)

You supply the game package. TuxBox doesn't include any game files. The recorded runs use package `RockstarGamesInc.28872443D20A7` 1.0.2.1, whose `game_scarlett_master.exe` has SHA-256 `e95020b2fa4a721db3df59fdecf75621730ac559d8835252f6c3b658d5a08993`. Other builds may need different providers or shader requests.

## Requirements

- The project's Linux build tools: Python 3.10 or newer, Rust with the `x86_64-pc-windows-msvc` target, `clang-cl`, `lld-link` and the xwin Windows SDK. [Build requirements](../examples/lanes/README.md#build-requirements)
- Proton Experimental from Steam and `umu-run`.
- Microsoft's Linux DXC release with `dxc` and `dxv`, for shader preparation and runtime translation. Arch's `directx-shader-compiler` 1.10.2605.37 fails on a minimal compute shader. Use Microsoft's release instead. [DirectX Shader Compiler releases](https://github.com/microsoft/DirectXShaderCompiler/releases)
- The Windows x64 DXC release (same-release `dxcompiler.dll` and `dxil.dll`) for generating rectangle geometry shaders inside the game process. The Linux `dxc` executable alone does not supply these DLLs. Use [Microsoft's official DXC releases](https://github.com/microsoft/DirectXShaderCompiler/releases).
- A C compiler and Vulkan headers. The launcher builds TuxBox's constant-buffer Vulkan layer on first use.

Run `python tuxbox.py inspect /path/to/package` to confirm the package. It should report one executable entry, `GTAO  Scarlett  game_scarlett_master.exe  present`.

## Build the providers

```sh
python -m pip install -r requirements.txt
python build.py
```

To build only what ProjectScarlett loads, run `python build.py <target>` for each of `xmem`, `d3d12`, `xg`, `xapu`, `acphal`, `xcrdapi`, `gameinput` and `xgameruntime`. Some Linux distributions currently need the build workarounds described in [issue #4](https://github.com/brcly/TuxBox/issues/4).

## Stage a working copy

Keep the original package unchanged and launch a copy. The package doesn't contain any of the provider DLL names, so TuxBox's copies are added beside the game files rather than replacing them.

```sh
cp -a --reflink=auto /path/to/GTA6.Xbox.v1.0.2.1 /path/to/scarlett-run
cp build/xmem-smoke/{xmem,tuxbox_xhttp}.dll \
   build/d3d12-smoke/d3d12_xs.dll \
   build/xg-provider/xg_xs.dll \
   build/xapu/{xapu,chad}.dll \
   build/acphal/AcpHal.dll \
   build/xcrdapi/xcrdapi.dll \
   build/gameinput/GameInput.dll \
   build/xgameruntime/xgameruntime.dll \
   /path/to/scarlett-run/
```

On a copy-on-write filesystem such as Btrfs or XFS, `--reflink=auto` copies the package almost instantly without using more disk space. If you use a hard-link copy (`cp -al`) to save space, never write into the linked game files. Writing to one also changes the original package.

| Provider | Build output | Loaded by |
| --- | --- | --- |
| `xmem.dll` | `build/xmem-smoke/` | the executable, `Bink2Scarlett.dll`, `XboxBvhBuilder.dll`, `amd_fidelityfx_upscaler_xbox.dll` |
| `tuxbox_xhttp.dll` | `build/xmem-smoke/` | `xmem.dll`, which forwards `XMemSetWinHttpHooks` to it |
| `d3d12_xs.dll` | `build/d3d12-smoke/` | the executable, `XboxBvhBuilder.dll`, `amd_fidelityfx_upscaler_xbox.dll` |
| `xg_xs.dll` | `build/xg-provider/` | the executable |
| `xapu.dll`, `chad.dll` | `build/xapu/` | the executable |
| `AcpHal.dll` | `build/acphal/` | the executable |
| `xcrdapi.dll` | `build/xcrdapi/` | the executable |
| `GameInput.dll` | `build/gameinput/` | the executable |
| `xgameruntime.dll` | `build/xgameruntime/` | the executable, at startup by name |

If using the application-local Windows compiler, copy both DLLs from the
same official release's `bin/x64` directory into the working copy beside
the game executable. Do not overwrite existing package files: compare them
first and use an untouched copy if they differ. The launcher warns about
missing or incompatible local DLLs but does not block a system-installed
compiler. The runtime reports an actual compiler load failure separately
from other pipeline errors. This is not a confirmed fix for the black-screen
report in #39.

Copy the providers again after each rebuild. The launcher enables the constant-buffer layer only when the staged `d3d12_xs.dll` is byte-identical to `build/d3d12-smoke/d3d12_xs.dll`.

## Launch offline

The game installs a Breakpad crash handler that uploads minidumps to an online crash service. Run it without network access so that crashes during testing are not uploaded. On Linux, start the launcher in a new network namespace:

```sh
UMU_RUNTIME_UPDATE=0 unshare --user --map-current-user --net -- \
    python tuxbox.py run --log-file build/scarlett.log /path/to/scarlett-run
```

Inside the namespace the only network device is a loopback interface that is down. `UMU_RUNTIME_UPDATE=0` stops umu-run from trying to update the Steam Runtime. Offline launches need unprivileged user namespaces.

Add `--mangohud` before the package path to record frame times. The prefix defaults to `~/.local/share/project-tuxbox/prefixes/Proton - Experimental`. Pass `--prefix` to use another one.

## Shader preparation

The first launch records each shader the game requests in the prefix. Before later launches, the launcher compiles the recorded requests and shows its progress, so the game reuses those translations instead of compiling them during play. Pass `--prepare-only` to prepare without starting the game, or `--no-shader-prep` to skip preparation for one launch. Place either option before the package path. [Preparation results](../evidence/projectscarlett-shader-preparation.json)

Preparation also reports the ten most frequent refusal reasons for newly attempted requests, grouped without register numbers and program counters. Cached refusals are counted separately because their reasons are not stored. For a full breakdown including cached refusals, run the read-only `tools/triage_scarlett_shaders.py` on the recorded request directory. When the request files are unavailable, the same tool can rank newly refused keys and literal sampler states from an existing runtime log: `python tools/triage_scarlett_shaders.py --log build/scarlett.log`. This log-only result is not a coverage estimate.

### Live compilation workload

During gameplay, unrelated shader requests can compile concurrently rather
than waiting for one another. The launcher defaults to at most four jobs
(and no more than its available CPU cores); set
`TUXBOX_RUNTIME_SHADER_JOBS=1` for the old serial behavior or a value from 1
to 8 to tune the CPU/memory cost. Identical shader requests within the game process still share one
translation, including retries while the previous request remains published. Persistent refusal markers are only written for deterministic
emitter refusals, not transient tool errors. This reduces queueing delays but
does not make unsupported shader instructions compatible.

Synthetic compute benchmark (not game performance): generate a fresh corpus with
`python tools/reference/scarlett_worker_fixture.py /path/to/new-corpus`.
Use `tools/benchmark_scarlett_worker.py --help` for a paired comparison of
the old and new worker, keeping the compiler executables and corpus identical.
On the measured Ryzen 5 5600X, 64 cold requests took a median 0.881 s serial
and 0.300 s with four jobs (five rounds per configuration); eight jobs took
0.229 s but had higher peak aggregate RSS. All outputs matched by SHA-256.
A forced host-side warm-cache burst took 0.074 s serial and 0.096 s with four
jobs; a single cold request remained about 0.065 s. The actual guest normally
reads verified warm-cache entries directly. These are small synthetic compute
controls, not a prediction of GTA VI loading times.

The live guest control `tools/check_live_shader_worker.py` uses the synthetic
corpus above and a disposable offline Proton prefix. It makes 24 simultaneous
Xbox compute-pipeline calls for 16 distinct shaders, with null-pipeline fallback
disabled. On the RTX 4080, all 24 calls succeeded at both four and eight jobs;
each distinct source was compiled once in each run and the validated outputs
matched across configurations. See `--help` for reproduction arguments and
[the receipt](../evidence/scarlett-live-worker-control.json).



## What to expect

The game opens its Americas window, loads its archives and presents 3840×2160 frames. The recorded run reached the player selection prompt, with Account Picker, Screen Narration and TuxBox options. It stopped at a title breakpoint 97 seconds after launch. Intro video, input after player selection and gameplay aren't verified yet. [Current priorities](PROGRESS.md#current-priorities)

In a new prefix, the game stops at a breakpoint during System Init because it reads its package through the `G:` drive, as on the console, and the launcher doesn't map that drive yet. A fix is in review. [Pull request #6](https://github.com/brcly/TuxBox/pull/6)

When you report a run, include the package version, executable hash, GPU and driver, Proton build, and the log printed in the first line of the launcher's output.


### Compare compiler revisions without running the game

Use the same recorded request directory for a paired CPU-only comparison,
with two host xshader executables and the same DXC and DXV:

```sh
python tools/compare_scarlett_shaders.py /path/to/requests \
    --baseline /path/to/old/xshader --candidate /path/to/new/xshader \
    --dxc /path/to/dxc --dxv /path/to/dxv --jobs 2 --json comparison.json
```

The production compile pipeline verifies the request digests, translates,
compiles, and validates DXIL in fresh temporary caches. The report separates
confirmed refusal-to-compiled recoveries, compiled-to-refused regressions,
and retryable errors. The command returns nonzero if a baseline success
does not compile with the candidate. The result is shader compilation
coverage on this exact request set, not proof of rendering or gameplay.
 For a small synthetic smoke comparison without game files, generate a fresh request
directory with `python tools/reference/scarlett_compare_fixture.py /path/to/new-directory`.
The nine synthetic controls include two valid guarded reductions, two unproven
guards, verified indexed reads, an unknown stride, two ordinary compute
programs and a deliberately malformed input. Pass
`--expect-candidate /path/to/new-directory/expected.json` when testing the
verified patches to fail if any expected status changes. For a real request
set use `--require-no-failures` to fail on any inconclusive tool/input error.


### Prioritize, replay and protect shader coverage

Rank distinct observed refusals from a runtime log, preserving an instruction
name when the emitter supplies one and exact literal sampler variants:

```sh
python tools/scarlett_worklist.py --log scarlett.log --json worklist.json \
    --manifest next-group.json --group 1
```

A worklist reports observed requests, not the number a proposed patch will
recover. An absent log entry does not establish a fix. The replay manifest
contains request names, not shader bytes. The owner must already have the
matching recorded inputs; missing requests fail before compilation. Replay
one or more groups using the existing production compiler comparison:

```sh
python tools/compare_scarlett_shaders.py /path/to/requests \
    --baseline /path/to/old/xshader --candidate /path/to/new/xshader \
    --dxc /path/to/dxc --dxv /path/to/dxv --jobs 2 \
    --select next-group.json --require-no-failures --json comparison.json
```

Convert a paired comparison into a new worklist. This distinguishes recovered
requests from unchanged refusals, different refusal reasons or sampler
variants, and inconclusive tool/input failures. Changed successful output
digests are flagged for review, not assumed to be incorrect rendering.

```sh
python tools/scarlett_worklist.py --comparison comparison.json \
    --json remaining.json --manifest protected.json --protect-compiled
```

The protected manifest records every successful candidate in that comparison
as an absolute compilation expectation. Pass it to `--select` on a later
comparison (using a fresh report path). A lost success fails even if the
new baseline also refuses it. Keep a full-corpus comparison for broad
coverage; a selected replay does not replace that check.

To coordinate work, optionally pass `--claims claims.json` to the worklist.
The file contains explicit, manually maintained ownership hints, for example:

```json
[
  {"ref": "#example", "opcode": "buffer_load_dword"},
  {"ref": "#another", "reason": "wave reduction at 0x_ may execute in divergent control flow"}
]
```

Use a reason, opcode, or both (exact match, combined with AND). The report
lists unclaimed ranks, overlapping claims, and claims not observed in the
input. A claim is not evidence of a fix; refresh it as PRs change. This
tool does not infer ownership from GitHub titles or generate native shader
code from an error message. Reports can contain request digests and emitter
diagnostics; review them before sharing. All operations above use CPU only.

[Validation receipt](../evidence/scarlett-worklist-control.json): 836 distinct
refusals from the public #39 log; real compiler comparisons of nine synthetic
requests; five protected successes, three deliberately lost successes
detected on downgrade, and three overlapping ownership hints in a synthetic
coordination control. No title data or game execution was needed.



### Investigate allocation-size mismatches

For a diagnostic launch, set `TUXBOX_LOG=stderr` and
`TUXBOX_TRACE_ALLOCATION_BYTES=8192,16384,invalid` before the normal offline
launch command. Rebuild and restage the provider first. The optional trace
reports exact selected allocation sizes (decimal or 0x-prefixed hex), including
the failing size sentinel with `invalid`. It records the title-relative caller,
resource descriptions and returned alignment, not resource contents. Repeated
records are deduplicated; at most 256 distinct records and eight descriptions
per record are emitted. Lines share an ID to correlate interleaved threads.
No allocation decisions are changed. This is intended to help locate the
source of a size mismatch, not to bypass the title's assertion.

### Ordinary scalar bit operations

The generic translator supports twelve additional scalar bit instructions
(32/64-bit zero counts, zero searches, leading-bit searches, reversal,
bit setting/clearing and testing). The local reaching-definition check
requires ordinary scalar values, not an assumed full-wave EXEC/VCC mask.
Unknown halves, branch entries and indirect control flow remain unsupported;
private packed and mesh paths are outside this proof. The rules follow
[AMD's RDNA 2 ISA reference](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf), SOP1/SOPC.

CPU-only controls:

- Capture `cargo test --manifest-path tools/shader-compiler/Cargo.toml --target x86_64-unknown-linux-gnu scalar_bit_cpu_oracle_fixture -- --nocapture`
  to a fresh fixture file. Run `python tools/check_scalar_bits.py --help`
  for the DXC/DXV numerical oracle: 1,944 cases, 7,776 checked values.
- Run `python tools/check_scalar_native.py --help` with the baseline and
  candidate xshader executables: 13 synthetic native programs recovered,
  six deliberately unsupported controls retained, one unchanged control.

[CPU evidence](../evidence/scarlett-ordinary-scalar-bits.json). These controls
are not retail shader coverage or proof of GPU execution.


The dependent-compute follow-up preserves ordinary scalar provenance through
verified min/max, wide shifts, bit updates and conditional/carry operations
only when the ordinary inputs (and, when required, SCC) are proven. It
does not relax branch, special wave-mask, mesh or unknown-input checks.
The CPU campaign found 483 rejected programs among 2,048 synthetic extended
programs on the previous #58 implementation, across twelve known
provenance-related refusal signatures. The twelve minimized native
reproducers are now permanent compiler tests. The full 2,048-program
campaign and the older 8,577 core plus 10,113 extended CPU-oracle programs
pass on the integrated current main + follow-up build (186,642 observed
register/flag values in total). Negative controls reject unknown SCC,
EXEC-derived SCC and unproven high halves. See
[evidence](../evidence/scarlett-dependent-scalar-chains.json). No GPU or
retail compatibility claim.


### CPU scalar reference and generated semantic tests

The independent reference model (`tools/reference/rdna2_scalar.py`) decodes
native words and models ordinary integer SGPR operations using the
[AMD RDNA 2 ISA reference](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)
(SOP1, SOP2 and SOPC). It is intentionally not a complete Xbox GPU simulator.
The model covers 77 instruction forms, ordinary registers s2..s9, SCC,
32/64-bit arithmetic, bit operations, shifts, comparisons and conditional
moves. It rejects unsupported selectors, incomplete programs, uninitialized
reads, and unsupported instruction classes instead of inventing results.
It does not simulate memory, textures, wave execution, vector lanes,
floating point, control flow or hardware timing.

Generate deterministic boundary-value and random programs, including
overlapping registers and chains. The core profile covers 65 forms accepted
by the baseline; extended adds the 12 forms proposed in #58.

```sh
python tools/check_scalar_encodings.py --llvm-mc /path/to/llvm-mc
python tools/generate_scalar_tests.py /tmp/scalar-corpus --per-op 128 --chains 256
cargo test --manifest-path tools/shader-compiler/Cargo.toml \
    --target x86_64-unknown-linux-gnu --lib --no-run
```

Use the Rust library test executable printed by Cargo (the
`tuxbox_xshader-...` binary, not the CLI executable):

```sh
python tools/check_scalar_oracle.py --corpus /tmp/scalar-corpus \
    --bridge /path/to/tuxbox_xshader-test-binary \
    --dxc /path/to/dxc --dxv /path/to/dxv \
    --work /tmp/scalar-check --output /tmp/scalar-receipt.json --jobs 2
```

The bridge runs the production native decoder and full translator. It batches
complete emitted HLSL function bodies only when their helper headers match,
and adds stores for all eight registers and the condition code. All input
registers and SCC are initialized by each synthetic program. DXC folds the
constant computations; DXV validates the resulting DXIL. Missing, duplicate,
nonconstant or mismatched stores fail the gate, as do translation refusals or
tool failures. The Python checker recomputes expected values independently
from the native words, not from the translator's HLSL or a trusted manifest.
The test does not execute generated DXIL on hardware.

Inspect any case step-by-step without invoking a compiler:

```sh
python tools/reference/rdna2_scalar.py /tmp/scalar-corpus/programs.txt --case 000000
```

Results on the measured CPU-only campaign:
- LLVM confirms all 77 instruction encodings.
- Core: 8,577/8,577 programs, 77,193 register/flag values, no mismatches;
  also passes on the separate #58 translator test build.
- Extended on #58: 10,026/10,113 programs, 90,234 compared values, no
  numerical mismatches. 87 chained programs are conservatively refused,
  and the gate correctly fails. These are reproducible remaining limitations,
  not confirmed failures in the retail game.
- A deliberately faulty addition-to-subtraction translator in a temporary
  test worktree produced five mismatches. The mutation is not in this PR.

See [the receipt](../evidence/scarlett-scalar-oracle.json) for case IDs,
seeds, exact corpus/compiler digests and counts. This is a reusable test tool,
not a change to the shipping translator or a claim of retail compatibility.

Rechecked the same core corpus after updating to upstream `3086e0b`: all
8,577 cases passed again. The earlier #58 and mutation receipts retain their
original source fingerprints.


### Bounded differential campaign and small reproducers

Use the existing CPU reference and full-translator bridge; no new runtime
dependencies. The campaign generates initialized straight-line scalar
instruction chains, verifies every distinct native instruction encoding
against LLVM gfx1030, checks the same programs with the #60 oracle and
DXC/DXV, and groups refusals by exact text with the instruction offset
removed (the same offset normalization principle as #44). Mismatches are
grouped by differing output indices, not claimed identical root causes.
A representative of each selected group is rechecked individually, reduced
by removing complete instructions after the fixed nine-instruction
initialization, then replayed twice in fresh compiler calls. Local
one-deletion minimality is recorded; this is not a global minimum. The
report stores the native words, assembly, expected state, case digest,
classification and counts, as well as structural opcode/alias/adjacency
features (not compiler branch coverage). Tool errors are not cached or
reported as confirmed shader defects.

```sh
python tools/scarlett_campaign.py --bridge /path/to/test-binary \
    --dxc /path/to/dxc --dxv /path/to/dxv --llvm-mc /path/to/llvm-mc \
    --profile extended --count 2048 --reduce 12 \
    --work /tmp/scalar-campaign --output /tmp/scalar-campaign.json
```

The default core profile is for main without #58. Extended requires the
proposed #58 operations for a passing run. Use a wrapper executable for
LLVM if it needs a container. Reuse a confirmed minimized corpus with
`--replay-report evidence/scarlett-differential-campaign.json --require-pass`
instead of generating new cases (use fresh work and output paths).
Replay deliberately bypasses cache. The normal discovery mode reports
refusals without failing the command; `--require-pass` also fails for them.
The campaign does not establish actual title coverage.

Optional trusted local `--cache /path/to/cache.json` and repeated
`--dependency /path/to/compiler-library.so` arguments retain exact
content-keyed results for repeat work. Include all actual loaded compiler
libraries (e.g. identify via ldd on trusted local binaries); a different
tool, source, dependency, Python version or environment refuses reuse.
Use one owner per cache, not concurrently shared or externally supplied
cache files. Cache is an optimization, not an independent proof; use a
fresh cache or no cache for release/regression verification. The tool
limits compiler workers to two, generated cases to 8,192, total evaluated
cases/cache entries to 20,000, reductions per representative to 64 and
representatives to 32. Work is temporary; per-tool timeouts apply and no
new evaluation starts beyond the 240-second campaign budget.

[Receipt](../evidence/scarlett-differential-campaign.json): 2,048 core
programs pass; 2,048 extended programs on the original #58 expose 483
refusals across twelve known provenance-related instruction signatures,
all confirmed as small reproducible cases. With the conservative compute
provenance follow-up all 2,048 pass, and the older 8,577 core plus 10,113
extended cases pass. The fix remains a separate change to #58. No accepted
case shows a numerical disagreement. Five alternating fresh/reused
evaluations of 512 identical programs (seed 17294091; LLVM checks in
both arms; cache primed once) measured 4.63 s vs 0.47 s median on the
local machine. This is repeat validation with an unchanged toolchain,
not faster cold discovery or a claim of higher FPS. The campaign remains
deliberately narrow: no memory, wave execution, branches or GPU testing.


## Offline launch and visual diagnostics

These tools consume existing logs or captures; neither starts the title,
touches the Wine prefix, changes driver settings, or uses a GPU.

### Locate a launch failure

```sh
python tools/diagnose_scarlett_run.py build/scarlett.log --json build/diagnosis.json
```

The diagnostic reads the launcher's actual status format, including appended
sessions, recorded D3D12 milestones, shader compiler outcomes, explicit
pipeline/resource/command failures, missing imports, survey call-site RVAs
and unhandled Wine exceptions. It reports line numbers and timestamps
where available, counts diagnostic events separately from distinct shader
requests, and suggests the next check for each observed fault type. Terminal
exception/device-loss evidence is listed first, then recent diagnostic
groups by line number; this is an investigation order, not causal ranking.
An absolute fault address is not a title RVA. Survey asserts that were
continued are not terminal errors. A missing exit record remains incomplete,
not a diagnosed hang. The first presentation does not prove visible output.
The input file should be complete and stable; mixed process output cannot
establish exact timing or causal attribution. Files over 512 MiB are rejected
and lines over 64 KiB are counted and skipped. Reports omit raw log messages
and paths, but may include module names and addresses; review before sharing.

In the public issue #39 log, the tool identifies the first presentation at
line 514, page mapping at 548, the slot 86 module-not-found HRESULT at 541,
64 logged compute-pipeline failure events and 836 distinct refused shaders.
The log has no exit record; the missing module is not identified by its
HRESULT, and these observations do not establish the ultimate stop.

### Compare rendered frames

Use a Python environment with Pillow installed (the existing project
requirements are unchanged; the frame analysis is optional).
Capture reference and candidate at the same camera, resolution, frame,
UI state and color settings, preferably lossless RGB or RGBA 8-bit SDR PNG.
The tool cannot automatically establish this equivalence.

```sh
python tools/compare_scarlett_frames.py reference.png candidate.png \
    --json frame-comparison.json --heatmaps frame-errors
```

For a sequence, pass two directories of equally named PNGs; names must
match exactly. The tool compares in lexicographic name order and counts
adjacent identical pixel digests, not elapsed time or proven freezes.
Dimensions, color metadata and modes must match. No automatic alignment,
resizing or color conversion. RGBA compares all four straight channels,
including transparent RGB. Optional `--roi X0 Y0 X1 Y1` uses half-open
coordinates to exclude deliberately chosen UI/overlay areas.

Default gates require exact agreement. Set `--tolerance` to a maximum
per-channel integer difference and both `--max-fraction` and
`--max-tile-fraction` to the allowed fraction of pixels exceeding it,
globally and in each tile. Choose thresholds before testing; the local
gate protects small defects from being hidden by global averages.
The report includes MAE, MSE, PSNR (null for exact equality), bounding box,
worst tiles, pixel digests, near-black/flat-color observations and optional
absolute-error grayscale PNGs (0=no difference, 255=maximum difference).
It returns 1 for a failed comparison, 2 for invalid input. Output paths
must be fresh. Maximum capture size is 16,777,216 pixels and maximum
sequence length 256 pairs. HDR/16-bit captures, animation, perceptual
quality and gameplay assessment are out of scope.

Reproduce six synthetic 4K comparisons and two independent launch sessions:

```sh
python tools/reference/scarlett_observability_fixture.py /tmp/observability-control
```

[Validation receipt](../evidence/scarlett-observability.json) includes the
public log fingerprint, controlled diagnostic outcomes and six 4K pairs
(identical, one changed pixel, local defect, black, tolerated one-level
changes, and swapped color channels). These are CPU-side checks, not real
title rendering comparisons.
