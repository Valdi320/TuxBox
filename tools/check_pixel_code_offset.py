#!/usr/bin/env python3
"""Read back a translated pixel shader's bounded embedded-table accesses."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.frosty_gpu_guest import run_gpu_guest

TABLE = (101, 217, 333, 449)
GUARD = (17, 29, 43, 71)


def instrument(hlsl, axis):
    # Only provide a varying input for the entry-live scalar s12. Leave the
    # translated mask, address arithmetic, table, load and export untouched.
    marker = "    // 0x0000 s_getpc_b64"
    if hlsl.count(marker) != 1 or hlsl.count("struct SIn {") != 1:
        raise ValueError("unexpected pixel fixture")
    return hlsl.replace("struct SIn {", "struct SIn { float4 position : SV_Position;").replace(
        marker, f"    s12 = (uint)i.position.{axis};\n" + marker)


def expected(axis, clipped):
    out = bytearray()
    for y in range(16):
        for x in range(16):
            val = TABLE[(x if axis == "x" else y) & 3]
            color = (val,) * 4
            if clipped and not (3 <= x < 11 and 3 <= y < 11):
                color = GUARD
            out.extend(struct.pack("<4I", *color))
    return bytes(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("hlsl", "dxc", "dxv", "provider", "proton", "prefix", "compiler-dir", "work", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--adapter", required=True)
    a = parser.parse_args()
    if a.work.exists() or a.output.exists():
        parser.error("fresh work and output required")
    a.work.mkdir(parents=True)
    rows = []
    with TemporaryDirectory(dir=a.work) as tmp:
        trial = Path(tmp)
        hlsl = a.hlsl.read_text()
        sources = {"vertex": ("vs_6_0", "float4 main(uint id:SV_VertexID):SV_Position { return float4(id==2?3:-1,id==1?3:-1,0.5,1); }"),
                   "copy": ("ps_6_0", instrument(hlsl, "x")), "inplace": ("ps_6_0", instrument(hlsl, "y"))}
        for name, (profile, source) in sources.items():
            src = trial / (name + ".hlsl")
            dst = trial / (name + ".cso")
            src.write_text(source)
            subprocess.run([a.dxc, "-T", profile, "-E", "main", "-O3", "-Fo", dst, src], check=True, timeout=30)
            subprocess.run([a.dxv, dst], check=True, timeout=30)
        # Reuse the integer render-target fixture; its two filenames identify
        # the two test axes here, not DPP operations.
        exe, status, fields = run_gpu_guest(ROOT / "tools/reference/straight-pixel-quads/main.cpp",
            "pixel-code-offset.exe", trial, a.work, a.proton, a.prefix, a.provider, a.compiler_dir,
            extra_env={"TUXBOX_NULL_PIPELINES": "0"})
        for name, axis in (("copy", "x"), ("inplace", "y")):
            for clipped in (False, True):
                case = name + ("_clipped" if clipped else "_full")
                path = trial / (case + ".bin")
                rows.append(dict(case=case, matched=path.is_file() and path.read_bytes() == expected(axis, clipped)))
        result = dict(passed=status == 0 and fields.get("adapter") == a.adapter and all(r["matched"] for r in rows),
                      scope="Synthetic pixel code-table loads, entry-live scalar input instrumented",
                      adapter=fields.get("adapter"), cases=len(rows), pixels=256*len(rows),
                      compared_words=1024*len(rows), rows=rows,
                      hlsl_sha256=hashlib.sha256(a.hlsl.read_bytes()).hexdigest(),
                      provider_sha256=hashlib.sha256(a.provider.read_bytes()).hexdigest())
        with a.output.open("x") as out:
            json.dump(result, out, indent=2)
            out.write("\n")
    print(result)
    return int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
