#!/usr/bin/env python3
"""Compare emitted compute loop-guard sites on identical archived requests.

CPU only. Static counts are not dynamic iteration counts or proof of a hang.
No shader bytes, HLSL, raw compiler diagnostics or paths enter the report.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from project_tuxbox.inspect import digest
from project_tuxbox.runtime_shaders import bounded_shader, XSHADER_UNSUPPORTED_HLSL
from tools.scarlett_replay_integrity import pin_emitter, snapshot_request
from tools.triage_scarlett_shaders import command, requests
from tools.scarlett_worklist import selected_sources


def guard_sites(hlsl):
    # Match the generated control-flow conditions, not mentions in comments.
    code = re.sub(rb"//[^\n]*|/\*[\s\S]*?\*/", b"", hlsl)
    structured = len(re.findall(rb"\+\+tb_guard\s*<\s*65536u\b", code))
    dispatch = re.findall(rb"\bwhile\s*\(\s*tb_pc\s*!=\s*\d+u\s*(?:&&\s*tb_guard\+\+\s*<\s*(1000000u))?\s*\)", code)
    # The persistent emitter uses a program-counter range check instead of
    # either generic guard. This is a separate shape, not evidence of a bound
    # on dynamic iterations (the PC can move backwards).
    persistent = len(re.findall(rb"\bwhile\s*\(\s*tb_pc\s*<\s*\d+u\s*\)", code))
    return dict(structured=structured, guarded_dispatch=dispatch.count(b"1000000u"),
                other_dispatch=dispatch.count(b""), persistent_dispatch=persistent)


GUARD_FIELDS = ("structured", "guarded_dispatch", "other_dispatch", "persistent_dispatch")


def has_guard(row):
    return row["status"] == "compute" and bool(row["structured"] or row["guarded_dispatch"])


def guard_shape(row):
    if row["status"] != "compute":
        return row["status"]
    return "+".join(k for k in GUARD_FIELDS if row[k]) or "no_matched_sites"


def probe(emitter, source, timeout):
    with TemporaryDirectory(prefix="tuxbox-guard-emission-") as tmp:
        work = Path(tmp)
        try:
            run = subprocess.run(command(emitter, source, work), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=timeout)
            if run.returncode:
                return dict(status="refused" if run.returncode == XSHADER_UNSUPPORTED_HLSL else "failed")
            profile = bounded_shader(work / "profile").strip()
            hlsl = bounded_shader(work / "shader.hlsl")
            if not hlsl or not re.fullmatch(rb"(?:cs|ps|vs|gs|ms|as|lib)_\d+_\d+", profile):
                return dict(status="failed")
            if not profile.startswith(b"cs_"):
                return dict(status="other_stage")
            return dict(status="compute", hlsl_bytes=len(hlsl), **guard_sites(hlsl))
        except (OSError, ValueError, subprocess.SubprocessError):
            return dict(status="failed")


def audit(sources, baseline, candidate, jobs, timeout, *, include_keys=False):
    with TemporaryDirectory(prefix="tuxbox-guard-tools-") as tmp:
        root = Path(tmp)
        old, old_hash = pin_emitter(baseline, root / "baseline")
        new, new_hash = pin_emitter(candidate, root / "candidate")

        def pair(source):
            with TemporaryDirectory(prefix="tuxbox-guard-input-") as directory:
                try:
                    snap = snapshot_request(source, Path(directory))
                except (OSError, ValueError):
                    return source.name, dict(status="failed"), dict(status="failed")
                return source.name, probe(old, snap, timeout), probe(new, snap, timeout)

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            rows = list(pool.map(pair, sources))
        if digest(old) != old_hash or digest(new) != new_hash:
            raise ValueError("emitter changed during audit")

    totals = {}
    for name, pos in (("baseline", 1), ("candidate", 2)):
        side = [row[pos] for row in rows]
        counts = Counter(r["status"] for r in side)
        compute = [r for r in side if r["status"] == "compute"]
        totals[name] = dict(statuses={k: counts[k] for k in ("compute", "other_stage", "refused", "failed")},
                            compute_with_guard=sum(has_guard(r) for r in compute),
                            **{k: sum(r[k] for r in compute) for k in GUARD_FIELDS})
    admitted = [r for _, before, r in rows if before["status"] == "refused" and r["status"] == "compute"]
    retained = [(before, after) for _, before, after in rows
                if before["status"] == after["status"] == "compute"]
    transitions = Counter((guard_shape(before), guard_shape(after)) for _, before, after in rows)
    report = dict(schema=2, scope="Static emitted HLSL guard sites; not runtime loop counts or causation",
                  requests=len(rows), tool_sha256=dict(baseline=old_hash, candidate=new_hash),
                  totals=totals, newly_admitted_compute=len(admitted),
                  newly_admitted_guarded=sum(has_guard(r) for r in admitted),
                  newly_guarded_existing_compute=sum(not has_guard(b) and has_guard(a) for b, a in retained),
                  no_longer_guarded_existing_compute=sum(has_guard(b) and not has_guard(a) for b, a in retained),
                  shape_transitions=[dict(baseline=b, candidate=a, requests=n)
                                     for (b, a), n in sorted(transitions.items())],
                  inconclusive=sum("failed" in (b["status"], a["status"]) for _, b, a in rows))
    if include_keys:
        report["rows"] = [dict(request=key, baseline=b, candidate=a) for key, b, a in rows]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requests", type=Path)
    for name in ("baseline", "candidate", "json"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--select", type=Path, help="exact local request worklist manifest")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--include-keys", action="store_true", help="local-only per-request digests")
    a = parser.parse_args(argv)
    if not 1 <= a.jobs <= 8 or not 0 < a.timeout <= 3600:
        parser.error("jobs must be 1..8 and timeout in (0,3600]")
    if not a.requests.is_dir() or a.json.exists() or a.json.resolve().is_relative_to(a.requests.resolve()):
        parser.error("archive directory and fresh output outside archive required")
    try:
        sources = requests(a.requests)
        if a.select:
            sources = selected_sources(sources, json.loads(a.select.read_text()))
        if not sources:
            raise ValueError("empty selection")
        report = audit(sources, a.baseline, a.candidate, a.jobs, a.timeout, include_keys=a.include_keys)
    except (OSError, ValueError):
        parser.error("archive, selection or emitter integrity check failed")
    with a.json.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(f"{report['requests']} requests; {report['newly_admitted_compute']} newly admitted compute, "
          f"{report['newly_admitted_guarded']} with guards; {report['inconclusive']} inconclusive")
    return int(bool(report["inconclusive"]))


if __name__ == "__main__":
    raise SystemExit(main())
