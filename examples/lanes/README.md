# Lanes

Lanes is an original, turn-based homebrew game for Project TuxBox. Collect as many coins as possible over 16 turns and avoid the red barriers. The bottom row is the next turn. Left and Right move one lane and advance. Space advances while holding your lane. A click on a lane takes one step toward it, or holds position in the current lane. On an XInput controller, use the D-pad or left stick to move and A to hold. R or Start restarts the course, and Escape closes the window. You can also click the board to restart after a finish.

## Build requirements

Use an x86-64 PC with Python 3.10 or newer, Rust 1.95 or newer with Cargo, and the DirectX Shader Compiler (`dxc`) on your PATH. Add the Windows build target once with `rustup target add x86_64-pc-windows-msvc`. [DirectX Shader Compiler releases](https://github.com/microsoft/DirectXShaderCompiler/releases)

- **Linux** needs `clang-cl`, `lld-link` and the Windows SDK and C runtime staged with xwin. To stage them in the cargo-xwin cache that `build.py` reads, run `cargo install cargo-xwin` and then `cargo xwin cache xwin --xwin-arch x86_64` once. If the SDK is outside the cargo-xwin cache, set `XWIN_SDK_ROOT` to the directory containing `sdk/` and `crt/`. The launcher builds a small Vulkan layer on first run, so also install the Vulkan headers (`vulkan-headers` on Arch). Use Microsoft's Linux DXC release: Arch's `directx-shader-compiler` 1.10.2605.37 aborts on every shader, and `build.py` reports this before building. Install UMU (`umu-run`, the `umu-launcher` package in the AUR on Arch) and [GE-Proton11-14](https://github.com/GloriousEggroll/proton-ge-custom/releases/tag/GE-Proton11-14), extracted into Steam's `compatibilitytools.d`, to play. From the project folder, run `python3 -m venv .venv` and `source .venv/bin/activate` before installing the Python dependencies.
- **Nix** users can run `nix develop` (or `direnv allow`) for a shell with these Linux tools. The Windows SDK still needs `cargo xwin cache xwin --xwin-arch x86_64` once. The shell also provides GE-Proton11-14 and sets `PROTONPATH` to it, so the launcher uses it without a Steam install; pass `--proton "$PROTONPATH"` to `tuxbox.py check` for the GPU checks. On NixOS, prefix the GPU checks with `tuxbox-fhs` (`tuxbox-fhs python tuxbox.py check ...`), which supplies the system libraries that Proton's Wine expects.
- **Windows** needs Visual Studio Build Tools with the C++ tools and Windows SDK. Run the commands in an **x64 Native Tools Command Prompt**.

## Build and play

Run these commands from the project folder.

```sh
python -m pip install -r requirements.txt
python build.py lanes
python tuxbox.py run --app-id Lanes build/lanes
```

The package in `build/lanes` contains the game, three TuxBox providers and a local Scarlett manifest. The launcher defaults to Proton on x86-64 Linux and native execution on x64 Windows. Add `--backend`, `--prefix` and `--proton` to the launcher before the package path when selecting a different Linux setup. An optional guest argument `--adapter VVVV:DDDD` selects a hardware GPU by PCI IDs.

## What the game exercises

| Game operation | Xbox library |
| --- | --- |
| Allocate and release the game state and course | `xmem.dll` |
| Create the guest device, dispatch the course shader, upload the board and display it with `PresentX` | `d3d12_xs.dll` |
| Read keyboard, mouse and controller state | `GameInput.dll`, through the same factory ordinal Frosty imports |

The game also imports Windows core API sets for services such as files, timing and synchronization. The providers handle desktop graphics, the window, and host input. The game draws its mouse cursor from GameInput movement deltas. [GDK mouse readings](https://learn.microsoft.com/en-us/gaming/gdk/docs/reference/input/gameinput/structs/gameinputmousestate)

## Self-test

`lanes.exe` also checks Xbox graphics behaviour that retail titles depend on, without opening a window. It creates the Series X|S device layout (`ID3D12Device11`) and runs each scenario on a fresh device. A scenario fails if the GPU has not finished within two seconds, so a deadlock fails the check instead of hanging it.

| Scenario | What it checks |
| --- | --- |
| `labels` | `Write32BitValueBottomOfPipeX` and `Write64BitValueBottomOfPipeX`, with and without a flush |
| `cross-queue` | The compute queue waits for a direct-queue label, writes a 64-bit payload and signals a reply. The direct queue waits for the reply and copies the payload for CPU readback. |
| `compute-frame` | `ExecuteCommandListsX` on a compute queue |
| `skipped-command` | A command that cannot be recorded leaves its list, and the list's fence write, intact |
| `copy-memory` | `CopyMemoryToMemoryX` copies from the source to the destination |
| `direct-copy-wait` | A direct queue waits before or after a buffer copy. The copy before the wait can finish while a second copy waits for the compute producer’s label and payload. |
| `shared-texture` | A queue waiting on a label reads a guest texture that the queue setting the label writes |
| `indirect-dispatch` | `ExecuteIndirect` on its Series X\|S slot matches a direct dispatch |
| `gpu-indirect-dispatch` | A compute shader writes indirect dispatch arguments for 0, 1 and 2 groups. Check all 24 output words, including untouched guards. |
| `dispatch-wait` | Copies a producer's word after a label wait, first at list start and then immediately after a course dispatch. Reports whether compute finished before producer submission and the word read by the CPU. |
| `texture-readback` | `CopyMemoryToMemoryX` reads a guest texture into title memory |
| `ray-tracing-builder` | `XboxBvhBuilder` builds a structure with a size report, copies it and builds it again in a batch (needs ray tracing) |
| `thread-dimensions` | Slot 80 `USE_THREAD_DIMENSIONS` (`0x20`) converts thread counts using the bound PSO's `[numthreads]` and fails closed without one |
| `descriptor-reclaim` | Shader-visible descriptor heap reclamation across completed batches, overlapping in-flight `Reset()`, `ClearState`, evicted-slot rebinding, and unsubmitted pin exhaustion |
| `xbox-shader` | A console shader's pipeline and dispatch ([shader cache](#shader-cache)) |
| `wave64-shader` | A console shader that sums across 64-lane waves, translated while the game runs; not part of `all` |
| `raster-tables` | A console vertex and pixel pair drawn under a root with two descriptor tables, reading through the second ([raster tables](#raster-tables)) |
| `raster-samplers` | A console pixel program's literal linear sampler, under a root whose static sampler at that register is point-filtered ([raster tables](#raster-tables)) |
| `compute-root` | Two root-less console compute programs under a root with three descriptor tables that the provider cannot reflect ([compute roots](#compute-roots)) |

```sh
python tuxbox.py verify --only lanes-selftest --output build/lanes-selftest.json
```

To run one scenario, or all of them in one process:

```sh
python tuxbox.py run --app-id Lanes build/lanes --selftest all --report selftest.txt
```

## Benchmark

`lanes.exe --benchmark` times fixed operations and reports each in microseconds, as a median unless noted. Lower is better.

| Reading | Operation |
| --- | --- |
| `submit_us` | Record, submit and wait for a list holding one fence write |
| `roundtrip_us` | Hand a fence label from the direct queue to a waiting compute queue |
| `copy64k_us` | Copy 64 KiB of title memory with `CopyMemoryToMemoryX` |
| `frame_us`, `frame_p99_us` | Paint the game board, upload it through the Xbox command list and read it back (typical and 99th percentile, without a window) |
| `shader_pipeline_us` | Create a compute pipeline from the console shader, found in the prepared cache (Proton only) |
| `dxil_pipeline_us` | Create the same pipeline from its translation directly. The difference from `shader_pipeline_us` is the cache lookup (Proton only) |

`python tuxbox.py check` runs it. Compare with an earlier run on the same machine:

```sh
cp build/check/results.json build/baseline.json
python tuxbox.py check --baseline build/baseline.json
```

The comparison reports changes and does not fail the check. Timings vary with other load on the machine, so close other GPU work before drawing conclusions.

## Shader cache

Console shaders reach the GPU through TuxBox's translation cache. Lanes carries a small hand-written console compute shader (`shader_fixture.py`; it is not taken from any game) and the `xbox-shader` scenario creates a pipeline from it and dispatches it. The provider substitutes an empty pipeline when it has no translation, so `verify` also requires the provider to report loading the translated shader.

```sh
python tuxbox.py verify --only lanes-shader-cache --output build/lanes-cache.json
```

| Check | What it proves |
| --- | --- |
| Prepared | A translation made before launch is loaded from the prepared cache |
| Compiled | An uncached shader is translated by the host worker while the game runs |
| Reused | The next launch uses that translation without compiling it again |

Lanes also carries a console program that sums across 64-lane waves. The `wave64-shader` scenario runs it and compares every thread's word, twice: once with the provider told the host has 32-lane waves ([host profile](../../docs/USAGE.md#host-profile)), so any card takes the [32-lane translation](../../docs/SHADERS.md#64-lane-programs-on-32-lane-hosts), and once with the card as it is.

| Check | What it proves |
| --- | --- |
| Wave, narrow | The provider asks for the 32-lane translation while the game runs, loads it, and the results are the console's |
| Wave, host | The results are the console's with whatever translation this card needs |

These checks run on the Proton backend. They remove only these shaders' cache entries from the prefix.

## Raster tables

The `raster-tables` scenario draws a fullscreen triangle with two hand-written console programs (`raster_fixture.py`; not taken from any game). The pixel program reads one dword through slot 2 of the root's second descriptor table and writes it to a uint target. Its FSSM records and PSV binding name that descriptor `t3, space2`, while the root declares `t0` to `t3` in space 1 there. The provider has to build the root the program was compiled against, with the register at that slot, and translate both programs for it. Under the title's root the pipeline becomes a stand-in and the target keeps its clear value. A wrong table or slot reads a decoy that names its view.

The `raster-samplers` scenario draws with the same vertex program and a third hand-written pixel program. It samples a 2x1 texture of 1.0 and 3.0, at table 1 slot 0, with a linear clamped sampler it builds from literal words, as the console compiles a static sampler into a program. It declares that sampler as `s0`, where the title's root has a point-filtered static sampler. The provider has to give the literal state a static sampler of its own in the reflected root, so every texel reads 1.5. Bound to the root's `s0`, the texels read the point-filtered 1.0.

That root is made while the game runs, so nothing can be prepared before launch. `verify` runs both scenarios with the host worker, requires the provider to report the reflected root and pipeline, and fails if either program was translated without a root.

```sh
python tuxbox.py verify --only lanes-raster-tables --output build/lanes-raster-tables.json
```

The suite runs on the Proton backend and removes only these programs' cache entries from the prefix.

## Compute roots

The `compute-root` scenario creates two root-less console compute programs (`shader_fixture.py`; not taken from any game) under a root with three descriptor tables. Without user-data records the provider cannot reflect that root, so it must translate each program against the root as given. Both programs store a word through table 1, and their FSSM records and PSV binding name that descriptor `u3, space2`, a register the root does not declare. A translation made without the root therefore cannot be bound.

- The first program stores through slot 2, which the root declares as `u2, space1`. Its translation for the root writes the word to that view; a wrong view, or a stand-in that leaves the decoy, fails the scenario.
- The second stores through slot 5, past the end of the table. The translator refuses it for this root, and the provider must give it a stand-in rather than fall back to the root-less translation.

`verify` runs the scenario with the host worker. It fails if either program was translated without its root, if the second was not refused, or if the provider substituted anything but that one stand-in.

```sh
python tuxbox.py verify --only lanes-compute-root --output build/lanes-compute-root.json
```

It runs on the Proton backend and removes only these programs' cache entries from the prefix.

## Deterministic replay

```sh
python tuxbox.py run --app-id Lanes build/lanes --replay --report replay.txt
```

The replay checks all 16 GPU-generated rows, a win, a missed coin and a collision. It uploads two 520 × 640 boards through the Xbox command list, reads both textures back, and checks every pixel plus the lane and player colors. The replay runs without opening a window. During interactive play, the same textures are presented through the Xbox command queue.

Run `python tuxbox.py verify --only lanes --output build/lanes-results.json` to check the replay and audit both the regular and delay-load import tables. The verifier requires all three Xbox libraries and rejects direct desktop graphics, drawing or input imports. [Proton Experimental](../../evidence/providers/lanes-xbox-valve.json) · [GE-Proton](../../evidence/providers/lanes-xbox-ge.json) · [Radeon replay](../../evidence/providers/lanes-xbox-radeon.json)

An interactive run compared the displayed board after keyboard presses, mouse clicks, and input from a hot-plugged virtual Xbox controller. It also checked the three DLLs loaded from the package. [Input and loader results](../../evidence/lanes-xbox-input.json) · [Captured board](../../evidence/lanes-xbox-board.png)

To repeat the keyboard part on Linux, run `python tools/check_lanes_input.py --prefix PREFIX --output build/lanes-input.json`. It launches the package with `tuxbox.py run`, presses keys through XTest, and reads the presented board from the X root window. Left and Right must move the highlighted lane. Steering into the next barrier must show COLLISION, and a later move must be ignored. R must restart, and Escape must exit with status 0 and leave no process in the prefix. The checks read the board, so they hold for any course. On a Wayland desktop the game runs in a private rootful Xwayland; elsewhere, run the check under `xvfb-run`. Add `--adapter VVVV:DDDD` and `--proton` as for the launcher.
