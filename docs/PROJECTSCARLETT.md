# Set up ProjectScarlett (GTA VI) on Linux

This guide stages GTA VI 1.0.2.1 (`ProjectScarlett`) with TuxBox's providers and starts it on Valve's stock Proton Experimental. The current result is the player selection prompt at 3840×2160. [Title progress](PROGRESS.md) · [Run record](../evidence/projectscarlett-startup.json)

You supply the game package. TuxBox doesn't include any game files. The recorded runs use package `RockstarGamesInc.28872443D20A7` 1.0.2.1, whose `game_scarlett_master.exe` has SHA-256 `e95020b2fa4a721db3df59fdecf75621730ac559d8835252f6c3b658d5a08993`. Other builds may need different providers or shader requests.

## Requirements

- The project's Linux build tools: Python 3.10 or newer, Rust with the `x86_64-pc-windows-msvc` target, `clang-cl`, `lld-link` and the xwin Windows SDK. [Build requirements](../examples/lanes/README.md#build-requirements)
- Proton Experimental from Steam and `umu-run`.
- Microsoft's Linux DXC release with `dxc` and `dxv`, for shader preparation and runtime translation. Arch's `directx-shader-compiler` 1.10.2605.37 fails on a minimal compute shader. Use Microsoft's release instead. [DirectX Shader Compiler releases](https://github.com/microsoft/DirectXShaderCompiler/releases)
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
