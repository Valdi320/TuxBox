#!/usr/bin/env python3
"""Aggregate the first failed #257 proof condition on private recorded inputs.

Build xshader from this diagnostic branch, then run:
    python tools/audit_pixel_proofs.py REQUESTS --xshader XSHADER --json proof-summary.json

The input archive is read-only. Report only fixed counts and the emitter
fingerprint, never shader bytes, identifiers, paths, or raw diagnostics.
Multiple retry reasons can count once each for a request. Unattributed
refusals need further inspection; they are not successful translations.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_tuxbox.inspect import digest
from project_tuxbox.runtime_shaders import XSHADER_UNSUPPORTED_HLSL
from tools.scarlett_replay_integrity import snapshot_request, pin_emitter
from tools.triage_scarlett_shaders import command, requests

REASONS = {
    "spill": frozenset("missing_lane_site non_scalar_lane_value invalid_lane_destination control_flow_or_special scalar_operation scalar_memory vector_operation interpolation other_encoding overlapping_vector_access missing_full_redefinition post_rewrite_overlap".split()),
    "attrs": frozenset("missing_hardware_count signature_limit count_exceeds_signature packed_half_layout attribute_outside_count half_read no_layout ambiguous_layout".split()),
}


def classify(status, stderr):
    if status == 0:
        return "translated", ()
    if status != XSHADER_UNSUPPORTED_HLSL:
        return "failed", ()
    # Use the actual final refusal, not incidental diagnostics on a retry.
    # Require exact known markers; never export registers, offsets or raw text.
    if re.search(r"is used both as a lane spill area and as a vector", stderr):
        family = "spill"
    elif re.search(r"PS attr\d+ has no signature register|\d+-bit read of half-float PS attr\d+", stderr):
        family = "attrs"
    else:
        return "other_refusal", ()
    known = set()
    for line in stderr.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[:2] == ["TB_PIXEL_PROOF", family] and fields[2] in REASONS[family]:
            known.add(fields[2])
    return family, tuple(sorted(known)) or ("unattributed",)


def probe(emitter, source, timeout):
    with TemporaryDirectory(prefix="tuxbox-proof-input-") as directory:
        work = Path(directory)
        try:
            snap = snapshot_request(source, work)
            run = subprocess.run(command(emitter, snap, work), env=dict(os.environ, TUXBOX_PIXEL_PROOF_AUDIT="1"),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                 errors="replace", timeout=timeout)
            return classify(run.returncode, run.stderr)
        except (OSError, ValueError, subprocess.SubprocessError):
            return "failed", ()


def audit(sources, emitter, jobs, timeout):
    with TemporaryDirectory(prefix="tuxbox-proof-tool-") as directory:
        pinned, fingerprint = pin_emitter(emitter, Path(directory) / "emitter")
        outcomes = Counter()
        counts = {family: Counter() for family in REASONS}
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for outcome, reasons in pool.map(lambda s: probe(pinned, s, timeout), sources):
                outcomes[outcome] += 1
                if outcome in counts:
                    counts[outcome].update(reasons)
        if digest(pinned) != fingerprint:
            raise ValueError("emitter changed")
    return dict(schema=1, scope="First failed proof conditions per request; emitter only, no gameplay claim",
                requests=len(sources), tool_sha256=fingerprint,
                outcomes={k: outcomes[k] for k in ("translated", "failed", "other_refusal", "spill", "attrs")},
                reasons={family: dict(sorted(rows.items())) for family, rows in counts.items()})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requests", type=Path)
    parser.add_argument("--xshader", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=30)
    a = parser.parse_args(argv)
    if not 1 <= a.jobs <= 16 or not 0 < a.timeout <= 3600:
        parser.error("jobs must be 1..16 and timeout in (0,3600]")
    if not a.requests.is_dir() or a.json.exists() or a.json.resolve().is_relative_to(a.requests.resolve()):
        parser.error("archive and fresh report outside archive required")
    sources = requests(a.requests)
    if not sources:
        parser.error("no recorded requests")
    try:
        report = audit(sources, a.xshader, a.jobs, a.timeout)
    except (OSError, ValueError):
        parser.error("emitter setup or integrity check failed")
    with a.json.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(json.dumps(report, indent=2))
    return int(bool(report["outcomes"]["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
