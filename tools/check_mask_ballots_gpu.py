#!/usr/bin/env python3
"""Check on a real GPU that lane masks read as numbers hold the whole wave's lanes.

Each synthetic program counts a mask's lanes, finds its first one or counts
those before each lane, some of them after EXEC was narrowed to one elected
lane. The reference models the console. Each program runs in waves of its own
width. Only the translator's input and output are instrumented. No game data is
needed.
"""
import argparse
import json
import re
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from check_lockstep_gpu import LIMIT, branch, label, words_of  # noqa: E402
from check_wave_guard import cases  # noqa: E402
from frosty_gpu_guest import run_gpu_guest  # noqa: E402
from project_tuxbox.inspect import digest  # noqa: E402

# Results go to v10, v11 and v12. s12 holds the entry EXEC and v14 the lane's index.
WAVE_OPTIONS = {32: (), 64: ("-mattr=-wavefrontsize32,+wavefrontsize64",)}
WAVE32 = {
    # A compare's own mask.
    "compare": ["v_cmp_lt_u32_e32 vcc_lo, 0x3e8, v0", "s_bcnt1_i32_b32 s20, vcc_lo", "v_mov_b32_e32 v10, s20",
                "v_mbcnt_lo_u32_b32 v11, vcc_lo, 0", "s_ff1_i32_b32 s21, vcc_lo", "v_mov_b32_e32 v12, s21"],
    # A copy of EXEC, read after one lane of it was elected.
    "elected": ["v_mbcnt_lo_u32_b32 v14, -1, 0", "v_cmp_lt_u32_e32 vcc_lo, 0x3e8, v0",
                "s_and_saveexec_b32 s10, vcc_lo", branch("s_cbranch_execz", "end"),
                "s_mov_b32 s11, exec_lo", "v_mbcnt_lo_u32_b32 v11, s11, 0",
                "s_ff1_i32_b32 s21, s11", "v_mov_b32_e32 v12, s21",
                "v_cmpx_eq_u32_e32 s21, v14", "s_bcnt1_i32_b32 s20, s11", "v_mov_b32_e32 v10, s20",
                label("end")],
    # A mask gathered over a loop.
    "gathered": ["s_mov_b32 s11, 0", "s_mov_b32 s22, 0", label("again"),
                 "v_and_b32_e32 v15, 3, v0", "v_cmp_eq_u32_e32 vcc_lo, s22, v15", "s_or_b32 s11, s11, vcc_lo",
                 "s_add_u32 s22, s22, 1", "s_cmp_lt_u32 s22, 3", branch("s_cbranch_scc1", "again"),
                 "s_bcnt1_i32_b32 s20, s11", "v_mov_b32_e32 v10, s20", "v_mbcnt_lo_u32_b32 v11, s11, 0",
                 "v_mov_b32_e32 v12, 0"],
    # The lanes left out, and a branch on their number.
    "others": ["v_cmp_lt_u32_e32 vcc_lo, 0x3e8, v0", "s_andn2_b32 s11, exec_lo, vcc_lo",
               "s_bcnt1_i32_b32 s20, s11", branch("s_cbranch_scc0", "end"), "v_mov_b32_e32 v10, s20",
               "v_mbcnt_lo_u32_b32 v11, s11, 0", "v_mov_b32_e32 v12, 1", label("end")],
}
WAVE64 = {
    "compare": ["v_cmp_lt_u32_e32 vcc, 0x3e8, v0", "s_bcnt1_i32_b64 s20, vcc", "v_mov_b32_e32 v10, s20",
                "v_mbcnt_lo_u32_b32 v11, vcc_lo, 0", "v_mbcnt_hi_u32_b32 v11, vcc_hi, v11",
                "s_ff1_i32_b64 s21, vcc", "v_mov_b32_e32 v12, s21"],
    "elected": ["v_mbcnt_lo_u32_b32 v14, -1, 0", "v_mbcnt_hi_u32_b32 v14, -1, v14",
                "v_cmp_lt_u32_e32 vcc, 0x3e8, v0", "s_and_saveexec_b64 s[10:11], vcc", branch("s_cbranch_execz", "end"),
                "s_mov_b64 s[14:15], exec", "v_mbcnt_lo_u32_b32 v11, s14, 0", "v_mbcnt_hi_u32_b32 v11, s15, v11",
                "s_ff1_i32_b64 s21, s[14:15]", "v_mov_b32_e32 v12, s21",
                "v_cmpx_eq_u32_e32 s21, v14", "s_bcnt1_i32_b64 s20, s[14:15]", "v_mov_b32_e32 v10, s20",
                label("end")],
    "gathered": ["s_mov_b64 s[14:15], 0", "s_mov_b32 s22, 0", label("again"),
                 "v_and_b32_e32 v15, 3, v0", "v_cmp_eq_u32_e32 vcc, s22, v15", "s_or_b64 s[14:15], s[14:15], vcc",
                 "s_add_u32 s22, s22, 1", "s_cmp_lt_u32 s22, 3", branch("s_cbranch_scc1", "again"),
                 "s_bcnt1_i32_b64 s20, s[14:15]", "v_mov_b32_e32 v10, s20",
                 "v_mbcnt_lo_u32_b32 v11, s14, 0", "v_mbcnt_hi_u32_b32 v11, s15, v11", "v_mov_b32_e32 v12, 0"],
    "others": ["v_cmp_lt_u32_e32 vcc, 0x3e8, v0", "s_andn2_b64 s[14:15], exec, vcc",
               "s_bcnt1_i32_b64 s20, s[14:15]", branch("s_cbranch_scc0", "end"), "v_mov_b32_e32 v10, s20",
               "v_mbcnt_lo_u32_b32 v11, s14, 0", "v_mbcnt_hi_u32_b32 v11, s15, v11", "v_mov_b32_e32 v12, 1",
               label("end")],
}


def program(body, width):
    size = "64" if width == 64 else "32"
    exec_, saved = ("exec", "s[12:13]") if width == 64 else ("exec_lo", "s12")
    return [f"s_mov_b{size} {saved}, {exec_}", *body, f"s_mov_b{size} {exec_}, {saved}", "s_endpgm"]


def model(name, active, values, host):
    """v10, v11 and v12 of every lane, with the group run as waves of `host` lanes."""
    rows = []
    for start in range(0, len(values), host):
        lanes = range(host)
        on = [active[start + i] for i in lanes]
        value = values[start:start + host]
        over = [on[i] and value[i] > LIMIT for i in lanes]
        mask = {"compare": over, "elected": over, "gathered": [on[i] and value[i] & 3 < 3 for i in lanes],
                "others": [on[i] and not over[i] for i in lanes]}[name]
        count, first = sum(mask), mask.index(True) if any(mask) else 0xffffffff
        for i in lanes:
            rank = sum(mask[:i])
            if not on[i] or not any(mask) and name in ("elected", "others"):
                rows.append((0, 0, 0))
            elif name == "compare":
                rows.append((count, rank, first))
            elif name == "elected":
                rows.append((count if i == first else 0, rank, first) if mask[i] else (0, 0, 0))
            else:
                rows.append((count, rank, int(name == "others")))
    return rows


def instrument(hlsl, width):
    start = "    v0 = gtid.x; v1 = gtid.y; v2 = gtid.z;"
    load = ("    uint input = output[gtid.x];\n    exec_lo = input >> 31;\n"
            "    v0 = input & 0x7fffffffu; v1 = gtid.y; v2 = gtid.z;")
    if hlsl.count(start) != 1 or "WaveActiveBallot" not in hlsl or f"[WaveSize({width})]" not in hlsl:
        raise ValueError("fixture must take a ballot in waves of its own width")
    stores = (f"    output[{width}u + gtid.x] = v10 | (v11 << 8u) | (WaveGetLaneCount() << 16u);\n"
              f"    output[{2 * width}u + gtid.x] = v12;\n")
    hlsl = hlsl.replace(start, load)
    end = hlsl.rstrip().rfind("}")
    return "RWBuffer<uint> output : register(u0);\n" + hlsl[:end] + stores + "}\n"


def paired_instrument(hlsl):
    """Supply the guest EXEC and two independent values to each host lane."""
    start = re.search(r"^    s\d+ = gid\.x; s\d+ = gid\.y; s\d+ = gid\.z;\n", hlsl, re.M)
    if not start or "[WaveSize(32)]" not in hlsl or "[numthreads(32, 1, 1)]" not in hlsl:
        raise ValueError("unexpected paired Wave64 prologue")
    load = (
        "    uint in0 = output[tb_pair], in1 = output[tb_pair + 32u];\n"
        "    v0__0 = in0 & 0x7fffffffu; v0__1 = in1 & 0x7fffffffu;\n"
        "    exec_lo = WaveActiveBallot((in0 >> 31u) != 0u).x;\n"
        "    exec_hi = WaveActiveBallot((in1 >> 31u) != 0u).x;\n"
    )
    hlsl = hlsl[:start.end()] + load + hlsl[start.end():]
    end = hlsl.rstrip().rfind("}")
    stores = "".join(
        f"    output[64u + tb_pair + {32*h}u] = v10__{h} | (v11__{h} << 8u) | (WaveGetLaneCount() << 16u);\n"
        f"    output[128u + tb_pair + {32*h}u] = v12__{h};\n" for h in (0, 1))
    return "RWBuffer<uint> output : register(u0);\n" + hlsl[:end] + stores + "}\n"


def wave64_mode(adapter, info):
    """Select the native or paired path from the selected Vulkan device."""
    wanted = tuple(int(part, 16) for part in adapter.split(":"))
    ranges = set()
    for block in re.split(r"^GPU\d+:\s*$", info, flags=re.M)[1:]:
        def value(key):
            m = re.search(rf"^\s*{key}\s*=\s*(0x[0-9a-fA-F]+|[0-9]+)\s*$", block, re.M)
            return int(m[1], 0) if m else None
        if (value("vendorID"), value("deviceID")) != wanted:
            continue
        ranges.add((value("minSubgroupSize"), value("maxSubgroupSize")))
    if len(ranges) != 1:
        raise ValueError("ambiguous or missing selected Vulkan device")
    low, high = ranges.pop()
    if low is None or high is None or not 0 < low <= high:
        raise ValueError("missing subgroup range")
    if low <= 64 <= high:
        return "native"
    if low <= 32 <= high:
        return "paired"
    raise ValueError("device supports neither native Wave64 nor paired Wave32")


def inputs(width):
    yield from cases(width)
    yield "below", (1 << width) - 1, list(range(width))
    yield "one_above", (1 << width) - 1, [LIMIT + 1 if lane == 17 else lane for lane in range(width)]
    yield "some_above", (1 << width) - 1, [LIMIT - 20 + lane for lane in range(width)]
    yield "inactive_above", 0x0000ffff, [lane if lane < 16 else LIMIT + lane for lane in range(width)]


def seed_for(mask, values):
    width = len(values)
    seed = [0x76540000 + i for i in range(384)]
    seed[96:96 + width] = [v | (((mask >> lane) & 1) << 31) for lane, v in enumerate(values)]
    return seed


def difference(name, seed, mask, values, data, expected_host=None):
    """The first word that is wrong, if any."""
    if len(data) != 4 * len(seed):
        return "no readback"
    actual, expected, width = struct.unpack("<384I", data), list(seed), len(values)
    host = actual[96 + width] >> 16
    if host != (expected_host if expected_host is not None else width):
        return f"host wave of {host}"
    rows = model(name, [bool((mask >> lane) & 1) for lane in range(width)], values, width)
    for lane, (count, rank, last) in enumerate(rows):
        expected[96 + width + lane] = count | rank << 8 | host << 16
        expected[96 + 2 * width + lane] = last
    return next((dict(word=i, got=a, wanted=b) for i, (a, b) in enumerate(zip(actual, expected)) if a != b), None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("emitter", "llvm-mc", "dxc", "dxv", "provider", "proton", "prefix", "compiler-dir", "work",
                 "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--adapter", required=True)
    args = parser.parse_args(argv)
    try:
        info = subprocess.run(["vulkaninfo"], capture_output=True, text=True, timeout=30, check=True).stdout
        mode = wave64_mode(args.adapter, info)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(f"cannot determine Wave64 test mode: {error}")
    print(f"Wave64 control: {mode}", flush=True)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=work) as tmp:
        trial = Path(tmp)
        trials, shaders = {}, {}
        for width, bodies in ((32, WAVE32), (64, WAVE64)):
            for name, body in bodies.items():
                words = words_of(program(body, width), args.llvm_mc, *WAVE_OPTIONS[width])
                paired = width == 64 and mode == "paired"
                emitted = subprocess.check_output(
                    [args.emitter.resolve(), *(["--paired"] if paired else []), str(width),
                     *(f"{word:08x}" for word in words)], text=True, timeout=30)
                source = trial / f"{name}{width}.hlsl"
                source.write_text(paired_instrument(emitted) if paired else instrument(emitted, width))
                blob = trial / f"{name}{width}.cso"
                subprocess.run([args.dxc, "-T", "cs_6_6", "-E", "main", "-Fo", blob, source], check=True, timeout=30,
                               capture_output=True)
                subprocess.run([args.dxv, blob], check=True, timeout=30, capture_output=True)
                shaders[f"{name}{width}"] = dict(hlsl_sha256=digest(source), dxil_sha256=digest(blob))
                for case, mask, values in inputs(width):
                    trial_name = f"{name}{width}_{case}"
                    trials[trial_name] = name, seed_for(mask, values), mask, values
                    (trial / f"{trial_name}.seed").write_bytes(struct.pack("<384I", *trials[trial_name][1]))
                    (trial / f"{trial_name}.cso").hardlink_to(blob)
        (trial / "cases.txt").write_text("".join(f"{name} 0 uint 1\n" for name in trials))
        exe, status, fields = run_gpu_guest(
            ROOT / "tools/reference/independent-shaders/compute.cpp", "ballots.exe", trial, work,
            args.proton.resolve(), args.prefix, args.provider, args.compiler_dir)
        wrong = {}
        for trial_name, (name, seed, mask, values) in trials.items():
            path = trial / f"{trial_name}.bin"
            found = difference(name, seed, mask, values, path.read_bytes() if path.is_file() else b"",
                               32 if mode == "paired" else len(values))
            if found:
                wrong[trial_name] = found
        passed = status == 0 and fields.get("adapter") == args.adapter and not wrong
        report = dict(passed=passed, scope="Synthetic programs reading lane masks as numbers",
                      adapter=fields.get("adapter"), wave64_mode=mode, guest_exit=status, cases=len(trials),
                      compared_words=len(trials) * 384, wrong=wrong, shaders=shaders,
                      emitter_sha256=digest(args.emitter), guest_sha256=digest(exe))
        if fields.get("error"):
            report["error"] = fields["error"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    for name, detail in wrong.items():
        print(f"WRONG {name}: {detail}")
    if not passed:
        print(f"FAIL: {len(wrong)} of {len(trials)} cases, guest exit {status}")
        return 1
    print(f"PASS: {len(trials)} cases, {len(trials) * 384} words, adapter {fields['adapter']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
