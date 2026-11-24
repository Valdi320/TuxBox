#!/usr/bin/env python3
"""Compare two xshader builds against identical recorded requests, CPU only.

Each request passes through the production HLSL -> DXC -> DXV pipeline in
a fresh temporary cache. No GPU, live cache changes or shader exports.
An accepted result is validated DXIL, not proof of correct rendering.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from project_tuxbox.inspect import digest
from project_tuxbox.runtime_shaders import compile_request, ShaderRefused, ToolFailure
from project_tuxbox.shader_diagnostics import normalize_refusal
from tools.triage_scarlett_shaders import requests


def compile_one(source, tools):
    with TemporaryDirectory(prefix="tuxbox-compare-") as temp:
        cache = Path(temp)
        try:
            if not compile_request(source, source.stem, tools, cache, None):
                raise ValueError("unexpected hit in a fresh cache")
            return {"status": "compiled", "dxil_sha256": digest(cache / (source.stem + ".cso"))}
        except ShaderRefused as exc:
            return {"status": "refused", "reason": normalize_refusal(str(exc))}
        except (OSError, ValueError, ToolFailure, subprocess.SubprocessError) as exc:
            return {"status": "failed", "reason": normalize_refusal(str(exc))}


def compare(sources, baseline, candidate, dxc, dxv, jobs):
    # Pin both emitters before the first request so a concurrent build cannot
    # change the executable underneath the comparison.
    with TemporaryDirectory(prefix="tuxbox-compare-tools-") as temp:
        pinned = []
        for name, source in (("baseline", baseline), ("candidate", candidate)):
            dest = Path(temp) / name / "xshader"
            dest.parent.mkdir()
            shutil.copyfile(source, dest)
            dest.chmod(0o700)
            pinned.append(dest)
        fingerprints = {name: digest(path) for name, path in zip(("baseline", "candidate"), pinned)}
        fingerprints.update(dxc=digest(dxc), dxv=digest(dxv))

        def pair(source):
            before = compile_one(source, (pinned[0], dxc, dxv))
            after = compile_one(source, (pinned[1], dxc, dxv))
            return {"request": source.name, "before": before, "after": after}

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            rows = list(pool.map(pair, sources))
        if fingerprints["dxc"] != digest(dxc) or fingerprints["dxv"] != digest(dxv):
            raise ValueError("DXC/DXV changed during comparison")

    counts = Counter((row["before"]["status"], row["after"]["status"]) for row in rows)
    statuses = ("compiled", "refused", "failed")
    return {
        "scope": "CPU-only production compiler pipeline; no rendering or retail gameplay claim",
        "tool_sha256": fingerprints,
        "requests": len(rows),
        "before": {s: sum(n for (a, b), n in counts.items() if a == s) for s in statuses},
        "after": {s: sum(n for (a, b), n in counts.items() if b == s) for s in statuses},
        "recovered": counts["refused", "compiled"],
        "regressed": counts["compiled", "refused"],
        "candidate_failures_after_success": counts["compiled", "failed"],
        "inconclusive": sum(n for (a, b), n in counts.items() if "failed" in (a, b)),
        "transitions": {a: {b: counts[a, b] for b in statuses} for a in statuses},
        "rows": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requests", type=Path)
    for name in ("baseline", "candidate", "dxc", "dxv", "json"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0, help="first N requests, zero for all")
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.limit < 0:
        parser.error("--jobs must be positive and --limit nonnegative")
    if not args.requests.is_dir():
        parser.error("recorded request directory required")
    if args.json.resolve().is_relative_to(args.requests.resolve()):
        parser.error("write the report outside the recorded request directory")
    if args.json.exists():
        parser.error("report already exists; choose a new output path")
    sources = requests(args.requests)[:args.limit or None]
    if not sources:
        parser.error("no recorded requests")
    report = compare(sources, args.baseline.resolve(), args.candidate.resolve(),
                     args.dxc.resolve(), args.dxv.resolve(), args.jobs)
    with args.json.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(f"{report['requests']} paired requests: {report['before']['compiled']} -> "
          f"{report['after']['compiled']} compiled; {report['recovered']} recovered, "
          f"{report['regressed']} regressed, {report['inconclusive']} inconclusive.")
    # Fail a regression gate even if an earlier success now has a retryable
    # error; distinguish that from a confirmed emitter refusal in the report.
    return int(bool(report["regressed"] or report["candidate_failures_after_success"]))


if __name__ == "__main__":
    raise SystemExit(main())
