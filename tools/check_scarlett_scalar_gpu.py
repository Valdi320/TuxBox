#!/usr/bin/env python3
"""Execute minimized scalar chains with dynamic inputs on a real D3D12 GPU.

Synthetic native code is translated by the #60 production-emitter test bridge.
Only the eight initial literal writes, initial comparison operand, dispatch
index and observable stores are instrumented. Independent Python reference
evaluates equivalent native programs with actual seed values. No title data.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import re
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scarlett_campaign import parts, flatten, canonical_init, write_corpus, key
from reference.rdna2_scalar import execute
from frosty_gpu_guest import run_gpu_guest

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def replace_one(text,pattern,value):
    text,n=re.subn(pattern,value,text,count=1,flags=re.M)
    if n!=1:
        raise ValueError("expected initial translator statement missing: "+pattern)
    return text

def instrument(text):
    for i in range(8):
        reg=i+2
        text=replace_one(text,rf"(    // 0x[0-9a-f]+ s_mov_b32\n    s{reg} = )0x[0-9a-f]+u;",rf"\g<1>output[9u * gid.x + {i}u];")
    text=replace_one(text,r"(    // 0x[0-9a-f]+ s_cmp_eq_u32\n    scc = \(0x0u == )0x[01]u",r"\g<1>output[9u * gid.x + 8u]")
    text,n=re.subn(r"^output\[([0-8])u\] = (s[2-9]|scc);$",r"output[9u * gid.x + \1u] = \2;",text,flags=re.M)
    if n!=9:
        raise ValueError("expected exactly nine observed stores")
    return text

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for n in ("bridge","dxc","dxv","provider","proton","prefix","compiler-dir","work","output","reproducers"):
        parser.add_argument("--"+n,type=Path,required=True)
    parser.add_argument("--adapter",required=True)
    args=parser.parse_args()
    if args.output.exists():
        parser.error("choose a fresh output")
    args.work.mkdir(parents=True,exist_ok=False)
    report=json.loads(args.reproducers.read_text())
    findings=report["findings"]
    assert len(findings)==12
    rng=random.Random(0x53434152)
    hashes={n:digest(getattr(args,n)) for n in ("bridge","dxc","dxv","provider","reproducers")}
    rows=[]
    with TemporaryDirectory(dir=args.work) as td:
        trial=Path(td)
        guest=ROOT/"tools/reference/independent-shaders/compute.cpp"
        (trial/"guest.cpp").write_text(guest.read_text())
        (trial/"context.h").write_text((guest.parent/"context.h").read_text().replace('"../compute_buffers.h"','"'+str(ROOT/"tools/reference/compute_buffers.h")+'"'))
        expected={}
        for ix,row in enumerate(findings):
            original=tuple(int(v,16) for v in row["reduced_words"])
            if key(original)!=row["reduced_id"] or execute(original)!=row["expected"]:
                raise ValueError("reproducer hash or result mismatch")
            blocks=parts(original)
            body=blocks[9:]
            # The initial registers and SCC are explicitly set in the ISA,
            # then replaced with dynamic seed reads in emitted HLSL.
            program=flatten(parts(tuple(canonical_init([0x12345678]*8,1)+[0xbf810000]))+body)
            name=f"scalar{ix:02}"
            corpus=trial/(name+"-corpus");corpus.mkdir()
            write_corpus(corpus,[program])
            work=trial/(name+"-bridge");work.mkdir()
            env=dict(os.environ,TUXBOX_ORACLE_PROGRAMS=str(corpus/"programs.txt"),TUXBOX_ORACLE_OUTPUT=str(work))
            subprocess.run([str(args.bridge.resolve()),"--exact","hlsl::cpu_oracle::scalar_oracle_bridge","--nocapture"],env=env,check=True,capture_output=True,timeout=30)
            assert (work/"status.tsv").read_text()=="000000\tcompiled\t0\t0\n"
            hlsl=work/"batch-00000.hlsl"
            src=instrument(hlsl.read_text())
            shader=trial/(name+".hlsl");shader.write_text(src)
            blob=trial/(name+".cso")
            subprocess.run([args.dxc,"-T","cs_6_0","-E","main","-O3","-Fo",blob,shader],check=True,capture_output=True,timeout=30)
            subprocess.run([args.dxv,blob],check=True,capture_output=True,timeout=15)
            seed=[rng.getrandbits(32) for _ in range(384)]
            want=seed.copy()
            for j in range(16):
                values=[rng.getrandbits(32) for _ in range(8)]
                if j<8:
                    edge=[0,1,0xffffffff,0x80000000,0x7fffffff,31,32,63]
                    values=edge[j:]+edge[:j]
                flag=j%2
                offset=96+9*j
                seed[offset:offset+9]=values+[1-flag]
                native=flatten(parts(tuple(canonical_init(values,flag)+[0xbf810000]))+body)
                want[offset:offset+9]=execute(native)
            (trial/(name+".seed")).write_bytes(struct.pack("<384I",*seed))
            expected[name]=struct.pack("<384I",*want)
            rows.append({"case":name,"reproducer_id":row["reduced_id"],"native_words":[f"{v:08x}" for v in program],
                         "emitted_sha256":digest(hlsl),"instrumented_sha256":digest(shader),"dxil_sha256":digest(blob)})
        (trial/"cases.txt").write_text("".join(f"{n} 4 structured 16\n" for n in expected))
        exe,status,fields=run_gpu_guest(trial/"guest.cpp","scalar-gpu.exe",trial,args.work,args.proton,args.prefix,args.provider,args.compiler_dir,
                                      extra_env={"TUXBOX_NULL_PIPELINES":"0","TUXBOX_LOG":"stderr"})
        if status or fields.get("adapter")!=args.adapter:
            raise RuntimeError(f"guest failed: {status} {fields}")
        for row in rows:
            name=row["case"];actual=trial/(name+".bin")
            observed=actual.read_bytes()
            if observed!=expected[name]:
                a,b=struct.unpack("<384I",observed),struct.unpack("<384I",expected[name])
                raise ValueError(f"{name}: GPU mismatch {[(i,x,y) for i,(x,y) in enumerate(zip(a,b)) if x!=y][:12]}")
            row["readback_sha256"]=digest(actual)
        receipt={"scope":"Dynamic runtime data through translated synthetic ordinary scalar chains; instrumented initial values and readback; no game",
                 "adapter":fields["adapter"],"proton":(args.proton/"version").read_text().strip(),"seed":0x53434152,
                 "chains":len(rows),"executed_programs":16*len(rows),"observed_register_words":16*9*len(rows),
                 "compared_words":384*len(rows),"guard_words":(384-16*9)*len(rows),"mismatches":0,"tools_sha256":hashes,
                 "guest_sha256":digest(exe),"rows":rows}
    if hashes!={n:digest(getattr(args,n)) for n in hashes}:
        raise ValueError("tool/input changed during GPU check")
    with args.output.open("x") as f:
        json.dump(receipt,f,indent=2);f.write("\n")
    print(f"PASS: {receipt['executed_programs']} dynamic programs, {receipt['compared_words']} GPU words, zero mismatches")

if __name__=="__main__":
    main()
