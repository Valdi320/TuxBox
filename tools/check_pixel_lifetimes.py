#!/usr/bin/env python3
"""Read back pixel shaders with separated spill lifetimes and partial attributes."""
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

GUARD = (17, 29, 43, 71)


def expected(axis, clipped):
    out = bytearray()
    for y in range(16):
        for x in range(16):
            color = (7, 23, 19, 23) if axis == "spill" else (0x3fc00000, 0x40b00000, 0x3fc00000, 0x40b00000)
            if clipped and not (3 <= x < 11 and 3 <= y < 11):
                color = GUARD
            out.extend(struct.pack("<4I", *color))
    return bytes(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("spill", "attr", "dxc", "dxv", "provider", "proton", "prefix", "compiler-dir", "work", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--adapter", required=True)
    a = parser.parse_args()
    if a.work.exists() or a.output.exists():
        parser.error("fresh work and output required")
    a.work.mkdir(parents=True)
    rows = []
    with TemporaryDirectory(dir=a.work) as tmp:
        trial = Path(tmp)
        vertex = """struct VOut {
    float a : TEXCOORD0; nointerpolation float b : TEXCOORD1;
    float c : TEXCOORD2; nointerpolation float d : TEXCOORD3;
    float4 position : SV_Position;
};
VOut main(uint id : SV_VertexID) {
    VOut o; o.position = float4(id==2?3:-1,id==1?3:-1,0.5,1);
    o.a = 9.0; o.b = 1.5; o.c = 13.0; o.d = 5.5;
    return o;
}"""
        sources = {"vertex": ("vs_6_0", vertex),
                   "copy": ("ps_6_0", a.spill.read_text()), "inplace": ("ps_6_0", a.attr.read_text())}
        for name, (profile, source) in sources.items():
            src = trial / (name + ".hlsl")
            dst = trial / (name + ".cso")
            src.write_text(source)
            subprocess.run([a.dxc, "-T", profile, "-E", "main", "-O3", "-Fo", dst, src], check=True, timeout=30)
            subprocess.run([a.dxv, dst], check=True, timeout=30)
        # Reuse the integer render-target fixture; its two filenames identify
        # the two test programs here, not DPP operations.
        exe, status, fields = run_gpu_guest(ROOT / "tools/reference/straight-pixel-quads/main.cpp",
            "pixel-lifetimes.exe", trial, a.work, a.proton, a.prefix, a.provider, a.compiler_dir,
            extra_env={"TUXBOX_NULL_PIPELINES": "0"})
        for name, axis in (("copy", "spill"), ("inplace", "attr")):
            for clipped in (False, True):
                case = name + ("_clipped" if clipped else "_full")
                path = trial / (case + ".bin")
                want = expected(axis, clipped)
                actual = path.read_bytes() if path.is_file() else b""
                row = dict(case=case, matched=actual == want)
                if len(actual) == len(want) and actual != want:
                    at = next(i for i in range(0, len(want), 16) if actual[i:i+16] != want[i:i+16])
                    row.update(first_mismatch_pixel=at // 16, actual=list(struct.unpack_from("<4I", actual, at)),
                               expected=list(struct.unpack_from("<4I", want, at)))
                rows.append(row)
        result = dict(passed=status == 0 and fields.get("adapter") == a.adapter and all(r["matched"] for r in rows),
                      scope="Synthetic pixel spill lifetimes and signature mapping, unmodified translator output",
                      adapter=fields.get("adapter"), cases=len(rows), pixels=256*len(rows),
                      compared_words=1024*len(rows), rows=rows,
                      hlsl_sha256={k:hashlib.sha256(getattr(a,k).read_bytes()).hexdigest() for k in ("spill","attr")},
                      provider_sha256=hashlib.sha256(a.provider.read_bytes()).hexdigest())
        with a.output.open("x") as out:
            json.dump(result, out, indent=2)
            out.write("\n")
    print(result)
    return int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
