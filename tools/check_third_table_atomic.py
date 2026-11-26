#!/usr/bin/env python3
"""GPU readback of an atomic UAV recovered from compute root table 2."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build import find_dxc
from tools.frosty_gpu_guest import run_gpu_guest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("hlsl", "provider", "proton", "prefix", "compiler-dir", "work", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--adapter", required=True)
    a = parser.parse_args()
    a.work.mkdir(parents=True, exist_ok=True)
    src = a.hlsl.read_text()
    marker = "    // 0x0000 s_load_dwordx4"
    assert src.count(marker) == 1 and "raw_ubuf0.InterlockedAdd(" in src
    # Deliberately contend four lanes per word. Only supply live test values;
    # the emitted address and operation remain unchanged.
    src = src.replace(marker, "    v4 = 1u + gtid.x; v5 = 4u * (gtid.x % 16u);\n" + marker)
    guest = (ROOT / "tools/reference/independent-shaders/compute.cpp").read_text()
    marker = "D3D12_ROOT_SIGNATURE_DESC description{1, &table, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};"
    assert guest.count(marker) == 1
    guest = guest.replace(marker, """D3D12_ROOT_PARAMETER parameters[3]{table, table, table};
    D3D12_DESCRIPTOR_RANGE unused0{D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, 0};
    D3D12_DESCRIPTOR_RANGE unused1{D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 1, 0, 0};
    parameters[0].DescriptorTable = {1, &unused0};
    parameters[1].DescriptorTable = {1, &unused1};
    D3D12_DESCRIPTOR_RANGE third[]{{D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 2, 0, 0}, {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, 2}};
    parameters[2].DescriptorTable = {2, third};
    D3D12_ROOT_SIGNATURE_DESC description{3, parameters, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};""")
    guest = guest.replace("D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 1,", "D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 3,")
    marker = "    device->CreateUnorderedAccessView(out.Get(), nullptr, &uav, heap->GetCPUDescriptorHandleForHeapStart());"
    assert guest.count(marker) == 1
    guest = guest.replace(marker, """    auto handle = heap->GetCPUDescriptorHandleForHeapStart();
    D3D12_SHADER_RESOURCE_VIEW_DESC unused{};
    unused.Format = DXGI_FORMAT_R32_TYPELESS;
    unused.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
    unused.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    unused.Buffer = {0, 1, 0, D3D12_BUFFER_SRV_FLAG_RAW};
    device->CreateShaderResourceView(nullptr, &unused, handle);
    handle.ptr += 2 * device->GetDescriptorHandleIncrementSize(hd.Type);
    device->CreateUnorderedAccessView(out.Get(), nullptr, &uav, handle);""")
    guest = guest.replace("list->SetComputeRootDescriptorTable(0, heap->GetGPUDescriptorHandleForHeapStart());",
                          "for (UINT i = 0; i < 3; ++i) list->SetComputeRootDescriptorTable(i, heap->GetGPUDescriptorHandleForHeapStart());")
    rng = random.Random(0x5336)
    dxc = Path(find_dxc(None)).resolve()
    with TemporaryDirectory(dir=a.work) as tmp:
        trial = Path(tmp)
        (trial / "atomic.hlsl").write_text(src)
        (trial / "guest.cpp").write_text(guest)
        (trial / "context.h").write_text((ROOT / "tools/reference/independent-shaders/context.h").read_text().replace(
            '"../compute_buffers.h"', '"' + str(ROOT / "tools/reference/compute_buffers.h") + '"'))
        blob = trial / "atomic.cso"
        subprocess.run([str(dxc), "-T", "cs_6_0", "-E", "main", "-Fo", str(blob), str(trial / "atomic.hlsl")], check=True, timeout=30)
        subprocess.run([str(dxc.with_name("dxv")), str(blob)], check=True, timeout=30)
        expected = {}
        for case in range(8):
            name = f"case{case:02}"
            seed = [rng.getrandbits(32) for _ in range(384)]
            want = seed.copy()
            for lane in range(64):
                want[96 + lane % 16] = (want[96 + lane % 16] + lane + 1) & 0xffffffff
            (trial / (name + ".seed")).write_bytes(struct.pack("<384I", *seed))
            (trial / (name + ".cso")).hardlink_to(blob)
            expected[name] = struct.pack("<384I", *want)
        (trial / "cases.txt").write_text("".join(f"{n} 0 raw 1\n" for n in expected))
        _, status, fields = run_gpu_guest(trial / "guest.cpp", "third-table-atomic.exe", trial, a.work, a.proton, a.prefix, a.provider, a.compiler_dir)
        assert status == 0 and fields.get("adapter") == a.adapter, fields
        assert all((trial / (n + ".bin")).read_bytes() == want for n, want in expected.items())
        result = dict(passed=True, adapter=fields["adapter"], cases=8, compared_words=3072,
                      result_words=128, preserved_words=2944, mismatches=0,
                      hlsl_sha256=hashlib.sha256(a.hlsl.read_bytes()).hexdigest())
        a.output.write_text(json.dumps(result, indent=2) + "\n")
        print(result)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
