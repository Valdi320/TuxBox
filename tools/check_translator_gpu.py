#!/usr/bin/env python3
"""Run every self-contained GPU check of the shader translator.

Each check assembles or builds synthetic console programs, translates them,
runs the result on the GPU and compares every output word with a reference
model. No game data is used. Builds the translator's example emitters first.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tools/shader-compiler/Cargo.toml"
TARGET = "x86_64-unknown-linux-gnu"

class Check:
    """One tool and where its program comes from.

    `emitter` names an example binary, or `xshader` for the compiler itself.
    `capture` names a unit test that prints the translation the tool runs and
    the option that takes it.
    """

    def __init__(self, tool, *, emitter=None, capture=None, llvm=False, dxc=False, extra=()):
        self.tool, self.emitter, self.capture = tool, emitter, capture
        self.llvm, self.dxc, self.extra = llvm, dxc, extra


def assembled(tool, *extra):
    return Check(tool, emitter="packed_integer", llvm=True, dxc=True, extra=extra)


CHECKS = {
    "alu": assembled("check_basic_alu_gpu"),
    "ordinary-integer": assembled("check_ordinary_integer_gpu"),
    "packed-integer": assembled("check_packed_integer_gpu"),
    "packed-saturate": assembled("check_packed_integer_gpu", "--clamp"),
    "packed-chain": assembled("check_packed_chain_gpu"),
    "packed-chain-saturate": assembled("check_packed_chain_gpu", "--include-saturate"),
    "packed-chain-unoptimized": assembled("check_packed_chain_gpu", "--optimization", "O0"),
    "sad-mad64": assembled("check_sad_mad64_gpu"),
    "f64": assembled("check_f64_exact_gpu"),
    "swizzle": Check("check_compute_swizzle", emitter="compute_swizzle"),
    "readlane": Check("check_readlane_gpu", emitter="readlane_probe", dxc=True),
    **{f"wave-extrema-{width}": Check("check_wave_extrema", emitter="wave_extrema", dxc=True,
                                      extra=("--width", str(width)))
       for width in (32, 64)},
    **{f"wave-guard-{width}-{variant}": Check("check_wave_guard", emitter="wave_guard",
                                              extra=("--width", str(width), "--variant", variant))
       for width in (32, 64) for variant in ("original", "chain", "chain-zero")},
    "wave-guard-64-2d": Check("check_wave_guard", emitter="wave_guard",
                              extra=("--width", "64", "--group", "8", "8", "1")),
    "wave-guard-64-3d": Check("check_wave_guard", emitter="wave_guard",
                              extra=("--width", "64", "--group", "4", "4", "4")),
    "lockstep": Check("check_lockstep_gpu", emitter="lockstep", llvm=True, dxc=True),
    "lds-bounds": Check("check_lds_bounds_gpu", emitter="lds_bounds", dxc=True),
    "paired-wave64": Check("check_paired_wave64", emitter="paired_wave64", llvm=True, dxc=True),
    "mask-ballots": Check("check_mask_ballots_gpu", emitter="mask_ballots", llvm=True, dxc=True),
    "dpp8-exchange": Check("check_dpp8_exchange", emitter="lone_lane_probe", dxc=True),
    **{f"dpp8-exchange-{layout}": Check("check_dpp8_exchange", emitter="lone_lane_probe", dxc=True,
                                       extra=("--layout", layout))
       for layout in ("2d", "3d")},
    **{f"dpp8-multiwave-{layout}": Check("check_dpp8_exchange", emitter="lone_lane_probe", dxc=True,
                                        extra=("--layout", layout, "--multiwave"))
       for layout in ("linear", "2d", "3d")},
    "pixel-heap-offsets": Check("check_pixel_heap_offsets", emitter="pixel_heap_offsets", dxc=True),
    "straight-pixel-quads": Check("check_straight_pixel_quads", emitter="straight_pixel_quads", dxc=True),
    "pixel-link": Check("check_portable_pixel_link", emitter="xshader", dxc=True),
    "pixel-link-slot86": Check("check_slot86_portable_link", emitter="xshader", dxc=True),
    "raw-index": Check("check_raw_index",
                       capture=("proven_byte_index_emits_addresses_without_widening_loads", "--hlsl")),
    "root-srv-remap": Check("check_raw_index",
                            capture=("compute_root_srv_uses_the_pipeline_register_not_the_shader_register", "--hlsl"),
                            extra=("--root-srv-remap",)),
    "dynamic-cbv": Check("check_dynamic_cbv", capture=("bounded_dynamic_cbv", "--hlsl"), dxc=True),
    "dynamic-code-offset": Check("check_dynamic_code_offset",
                                 capture=("bounded_runtime_code_offset", "--hlsl")),
    "wide-atomic": Check("check_wide_atomic",
                         capture=("verified_atomic_does_not_depend_on_shader_name", "--body")),
    "wide-atomic-nsa": Check("check_wide_atomic",
                             capture=("verified_nsa_atomic_uses_the_encoded_second_coordinate", "--body"),
                             extra=("--non-sequential",)),
}
CARGO = ["--release", "--locked", "--target", TARGET, "--manifest-path", str(MANIFEST),
         "--target-dir", str(ROOT / "build/shader-compiler")]


def build_emitters():
    """Build the compiler and the example emitters. Returns each one's path."""
    command = ["cargo", "build", *CARGO, "--bins"]
    examples = sorted({check.emitter for check in CHECKS.values()} - {None, "xshader"})
    for example in examples:
        command += ["--example", example]
    subprocess.run(command, check=True, cwd=ROOT)
    release = ROOT / "build/shader-compiler" / TARGET / "release"
    return {"xshader": release / "xshader", **{name: release / "examples" / name for name in examples}}


def printed_block(output):
    """The first text a unit test printed between `BEGIN_x` and `END_x` lines."""
    # The test harness may print the test's name on the line the marker starts.
    found = re.search(r"BEGIN_(\w+)[^\n]*\n(.*?)^END_\1\b", output, re.MULTILINE | re.DOTALL)
    if not found:
        raise ValueError("the unit test printed no program")
    return found.group(2)


def capture(test, path):
    """Save the program that the shader compiler's unit test `test` prints."""
    run = subprocess.run(["cargo", "test", *CARGO, "--lib", test, "--", "--nocapture", "--test-threads=1"],
                         check=True, cwd=ROOT, capture_output=True, text=True, timeout=600)
    path.write_text(printed_block(run.stdout))


def command_for(name, emitters, tools, host, work):
    check = CHECKS[name]
    command = [sys.executable, str(ROOT / "tools" / f"{check.tool}.py"), *check.extra]
    if check.emitter:
        command += ["--emitter", str(emitters[check.emitter])]
    if check.capture:
        command += [check.capture[1], str(work / f"{name}.hlsl")]
    if check.llvm:
        command += ["--llvm-mc", str(tools["llvm-mc"])]
    if check.dxc:
        command += ["--dxc", str(tools["dxc"]), "--dxv", str(tools["dxv"])]
    for option, value in host.items():
        command += [f"--{option}", str(value)]
    return command + ["--work", str(work / name), "--output", str(work / f"{name}.json")]


def vulkan_device(adapter, summary):
    """The fields `vulkaninfo --summary` lists for a PCI vendor:device ID, or None."""
    wanted = tuple(int(part, 16) for part in adapter.split(":"))
    for block in re.split(r"^GPU\d+:\s*$", summary, flags=re.MULTILINE)[1:]:
        fields = dict(re.findall(r"^\s*(vendorID|deviceID|deviceName|deviceUUID)\s*=\s*(.+?)\s*$", block, re.MULTILINE))
        if (int(fields.get("vendorID", "-1"), 16), int(fields.get("deviceID", "-1"), 16)) == wanted:
            return fields
    return None


def vulkan_device_name(adapter, summary):
    """The device name `vulkaninfo --summary` lists for a PCI vendor:device ID, or None."""
    return (vulkan_device(adapter, summary) or {}).get("deviceName")


def device_filter(fields):
    """The environment that makes DXVK expose only this device.

    A Proton guest can see a device under another name than the host prints:
    "AMD Radeon RX 6900 XT (RADV NAVI21)" on the host was
    "Radeon RX 6800/6800 XT / 6900 XT" in the guest. The device UUID is the
    same on both sides, so filter by it. DXVK compares the UUID as 32 hex
    digits. Without a UUID, fall back to the name.
    """
    uuid = re.sub(r"[^0-9a-f]", "", fields.get("deviceUUID", "").lower())
    if len(uuid) == 32:
        return {"DXVK_FILTER_DEVICE_UUID": uuid}
    name = fields.get("deviceName")
    return {"VKD3D_FILTER_DEVICE_NAME": name, "DXVK_FILTER_DEVICE_NAME": name} if name else {}


def select_adapter(adapter):
    """Have DXVK expose only the requested adapter.

    The guests create their device on the default adapter, so on a machine
    with several GPUs they would otherwise run on whichever one comes first.
    """
    try:
        summary = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True,
                                 timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    fields = vulkan_device(adapter, summary)
    if not fields:
        return None
    os.environ.update(device_filter(fields))
    return fields.get("deviceName")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("provider", "proton", "prefix", "compiler-dir", "dxc", "work"):
        ap.add_argument("--" + name, type=Path, required=True)
    ap.add_argument("--llvm-mc", default="llvm-mc")
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--only", choices=tuple(CHECKS), action="append")
    a = ap.parse_args(argv)
    llvm_mc = shutil.which(a.llvm_mc)
    if not llvm_mc:
        ap.error("install llvm-mc from LLVM, or pass --llvm-mc")
    dxc = a.dxc.resolve()
    tools = {"llvm-mc": llvm_mc, "dxc": dxc, "dxv": dxc.with_name("dxv")}
    if not (dxc.is_file() and tools["dxv"].is_file()):
        ap.error("--dxc must name a DXC with dxv beside it")
    work = a.work.resolve()
    if work.exists():
        ap.error("choose a fresh work directory")
    work.mkdir(parents=True)
    emitters = build_emitters()
    device = select_adapter(a.adapter)
    print(f"adapter {a.adapter.lower()}: {device or 'default Vulkan device (vulkaninfo did not list it)'}",
          flush=True)
    host = {"provider": a.provider.resolve(), "proton": a.proton.resolve(), "prefix": a.prefix.resolve(),
            "compiler-dir": a.compiler_dir.resolve(), "adapter": a.adapter.lower()}
    results = []
    for name in a.only or CHECKS:
        started = monotonic()
        log = work / f"{name}.log"
        with log.open("w") as stream:
            try:
                if CHECKS[name].capture:
                    capture(CHECKS[name].capture[0], work / f"{name}.hlsl")
                status = subprocess.run(command_for(name, emitters, tools, host, work), cwd=ROOT, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=600).returncode
            except subprocess.TimeoutExpired:
                status = "timed out"
            except (subprocess.CalledProcessError, ValueError) as error:
                stream.write(f"{error}\n")
                status = "no program"
        lines = log.read_text(errors="replace").strip().splitlines()
        summary = lines[-1][:300] if lines else ""
        results.append({"check": name, "passed": status == 0, "seconds": round(monotonic() - started, 1),
                        "summary": summary})
        print(f"{'PASS' if status == 0 else 'FAIL'}  {name}  {summary}", flush=True)
        if status != 0:
            print("\n".join(lines[-12:]), flush=True)
    passed = all(row["passed"] for row in results)
    (work / "summary.json").write_text(json.dumps(
        {"schema": 1, "adapter": host["adapter"], "passed": passed, "checks": results}, indent=2) + "\n")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
