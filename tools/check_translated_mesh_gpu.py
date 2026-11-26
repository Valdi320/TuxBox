#!/usr/bin/env python3
"""GPU readback of synthetic console NGG mesh programs on desktop D3D12.

Checks translator output, not the Xbox command-list or root replacement ABI.
The independently written pixel stage writes a constant to covered pixels.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from examples.lanes.raster_fixture import container, image, signature, pack
from tools.frosty_gpu_guest import run_gpu_guest

SRC = ROOT / "tools/reference/mesh"
COUNTS = (3, 67, 128, 0, 129)
CASES = tuple(f"{name}-{count}" for count in COUNTS for name in ("plain", "swizzle")) + ("bad-index",)


def program(count, swizzle=False, bad_index=False):
    first = {67: 64, 128: 125}.get(count, 0)
    src = (SRC / "translated_shared_count.s").read_text()
    src = "\n".join(line for line in src.splitlines() if not line.startswith("//"))
    third = first + (3 if bad_index else 2)
    packed = first | ((first + 1) << 10) | (third << 20)
    src = src.replace("s_mov_b32 s20, 3", f"s_mov_b32 s20, {count}")
    src = src.replace("v_mov_b32 v27, 0x00200400", f"v_mov_b32 v27, {packed:#x}")
    src = src.replace("v_cmp_eq_u32 vcc, 2, v1", f"v_cmp_eq_u32 vcc, {first + 2}, v1")
    src = src.replace("v_cmp_eq_u32 vcc, 1, v1", f"v_cmp_eq_u32 vcc, {first + 1}, v1")
    if swizzle:
        # XOR1 before XOR1 exchange restores the group index. An identity
        # exchange instead makes the first triangle degenerate.
        src = src.replace(f"v_cmp_eq_u32 vcc, {first + 2}, v1",
                          "v_xor_b32 v25, 1, v1\nds_swizzle_b32 v26, v25 offset:0x41f\n"
                          f"s_waitcnt lgkmcnt(0)\nv_cmp_eq_u32 vcc, {first + 2}, v26")
        src = src.replace(f"v_cmp_eq_u32 vcc, {first + 1}, v1",
                          f"v_cmp_eq_u32 vcc, {first + 1}, v26")
    return src + "\n"


def assemble(llvm, src):
    run = subprocess.run([llvm, "-triple=amdgcn-amd-amdhsa", "-mcpu=gfx1030",
                          "-mattr=+wavefrontsize64,-wavefrontsize32", "-show-encoding"],
                         input=src, text=True, capture_output=True, check=True, timeout=30)
    raw = bytes(int(x, 16) for row in re.findall(r"encoding: \[([^\]]+)\]", run.stdout)
                for x in row.split(","))
    words = list(struct.unpack("<" + "I" * (len(raw) // 4), raw))
    # LLVM encodes the unused third VOP3 compare operand as inline zero;
    # the console recognizer's admitted form uses s0. It is not read.
    compares = [i for i, word in enumerate(words) if word in (0xd4c30000, 0xd4c30002)]
    if len(compares) != 2:
        raise ValueError("unexpected compare encoding")
    for i in compares:
        words[i + 1] &= ~(0x1ff << 18)
    return words


def shader(words):
    info = bytearray(48)
    info[24], info[27] = 13, 2
    struct.pack_into("<HH", info, 12, 128, 128)
    struct.pack_into("<3I", info, 36, 128, 1, 1)
    return container(0xd0065, image(7, words), [
        (b"OSGN", signature([("SV_Position", 0, 1, 3, 0, 15, 0)])),
        (b"PSV0", pack([48]) + info + pack([0])),
        (b"RTS0", pack([1, 0, 24, 0, 24, 0])),
    ])


def validate(status, fields, adapter):
    expected = {f"{case}_{clip}{suffix}": adapter if suffix else "64"
                for case in CASES for clip in ("full", "clipped") for suffix in ("", "_adapter")}
    return status == 0 and fields == expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("emitter", "dxc", "dxv", "llvm-mc", "provider", "proton", "prefix",
                 "compiler-dir", "work", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--adapter", required=True)
    a = parser.parse_args()
    if a.work.exists() or a.output.exists():
        parser.error("fresh work and output required")
    a.work.mkdir(parents=True)
    hashes = {}
    with TemporaryDirectory(dir=a.work) as tmp:
        trial = Path(tmp)
        for case in CASES:
            bad = case == "bad-index"
            count = 3 if bad else int(case.split("-")[1])
            source = trial / (case + ".dxbc")
            source.write_bytes(shader(assemble(a.llvm_mc, program(count, case.startswith("swizzle"), bad))))
            hlsl = trial / (case + ".hlsl")
            profile = trial / (case + ".profile")
            dxil = trial / (case + ".cso")
            subprocess.run([a.emitter, source, "--hlsl-out", hlsl, "--profile-out", profile],
                           check=True, timeout=30)
            if profile.read_text() != "ms_6_5":
                raise ValueError("unexpected mesh profile")
            subprocess.run([a.dxc, "-T", "ms_6_5", "-E", "main", "-O3", "-Fo", dxil, hlsl],
                           check=True, timeout=30)
            subprocess.run([a.dxv, dxil], check=True, timeout=30)
            hashes[case] = hashlib.sha256(source.read_bytes()).hexdigest()
        ps = trial / "pixel.hlsl"
        ps.write_text("RWByteAddressBuffer outputColor:register(u0); "
                      "void main(float4 position:SV_Position) { uint2 xy=uint2(position.xy); "
                      "outputColor.Store(4*(xy.x+xy.y*8),0x2468ace0u); }")
        subprocess.run([a.dxc, "-T", "ps_6_0", "-E", "main", "-O3", "-Fo", trial/"pixel.cso", ps],
                       check=True, timeout=30)
        subprocess.run([a.dxv, trial/"pixel.cso"], check=True, timeout=30)
        _, status, fields = run_gpu_guest(SRC/"translated_guest.cpp", "translated-mesh.exe", trial,
            a.work, a.proton, a.prefix, a.provider, a.compiler_dir,
            extra_env={"TUXBOX_NULL_PIPELINES": "0"})
    passed = validate(status, fields, a.adapter.lower())
    report = dict(passed=passed, scope="Synthetic console NGG translated to desktop D3D12 mesh shader",
                  adapter=a.adapter.lower(), cases=len(CASES)*2, compared_words=len(CASES)*128,
                  receipts=fields, source_sha256=hashes, guest_exit=status)
    with a.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(f"{len(CASES)*2} mesh draws; {len(CASES)*128} words expected; {'PASS' if passed else 'FAIL'}")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
