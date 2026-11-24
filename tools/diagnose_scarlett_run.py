#!/usr/bin/env python3
"""Offline evidence-based launch diagnosis; never infer a crash from silence."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

STAMP = re.compile(r"^\[TuxBox ([^\]]+)\] (.*)$")
PROVIDER = re.compile(r"TuxBox d3d12_xs: (.*)")
SHADER = re.compile(r"^(?:Xbox shader ([0-9a-f]{64}(?:_rt[0-7]|_[0-9a-f]{64}_[0-9a-f]{64})?)[: ] .*?failed \((\d+)\):|Compiled Xbox shader ([0-9a-f]{64}(?:_rt[0-7]|_[0-9a-f]{64}_[0-9a-f]{64})?)(?:\s|$))")
EXIT = re.compile(r"^Game exited with (-?\d+) after (\d+(?:\.\d+)?)s$")
HRESULT = {
    "0x80004005": "unspecified API failure",
    "0x80004001": "not implemented",
    "0x8007007e": "module not found (which module is not established)",
    "0x80070057": "invalid argument",
    "0x8007000e": "out of memory reported by API",
    "0x887a0005": "device removed",
    "0x887a0006": "device hung",
    "0x887a0007": "device reset",
}
MILESTONES = (
    ("device_created", r"^created host D3D12 device"),
    ("first_submission", r"^submitted first guest batch"),
    ("first_present", r"^PresentX presented first frame \(\d+x\d+\)"),
    ("first_pipeline_stream", r"^created pipeline from Xbox slot 86 stream"),
    ("page_mapping", r"^CopyPageMappingsBatchX mapped \d+ batches"),
    ("raster_draw", r"^recorded raster draw "),
)
# Do not treat ordinary SEH dispatch, Wine fixme, or shader refusals as fatal.
FAULTS = (
    ("pipeline_stream_failure", r"^slot 86 pipeline stream \(\d+ bytes\) failed: (0x[0-9a-fA-F]{8})"),
    ("pipeline_stage_failure", r"^slot 86 pipeline stage failure #\d+ (0x[0-9a-fA-F]{8})"),
    ("compute_pipeline_failure", r"^compute pipeline creation failed \((0x[0-9a-fA-F]{8}),"),
    ("resource_failure", r"^resource slot \d+ failed (0x[0-9a-fA-F]{8})"),
    ("allocation_recording_failure", r"^command list cannot record allocation info"),
    ("recording_failure", r"^command list cannot record (?!allocation info)"),
    ("null_compute_dispatch", r"^null compute dispatch "),
)
SURVEY = (
    ("survey_assert_continued", re.compile(r"^(?:survey: stepped over|continued past recoverable) title assert RVA (0x[0-9a-f]+) \(hit (\d+)\)")),
    ("unsupported_vtable_slot", re.compile(r"^survey: unsupported D3D12\.x vtable slot (\d+) from title RVA (0x[0-9a-f]+) \(hit (\d+)\)")),
)
GUIDANCE = {
    "pipeline_stream_failure": "Inspect slot 86 stream construction, stage failures and actual library loader diagnostics.",
    "pipeline_stage_failure": "Replay the matching stages and root signature using the shader comparison tools.",
    "compute_pipeline_failure": "Replay the reported shader and inspect its native pipeline creation result.",
    "resource_failure": "Inspect the exact resource description and API result at the referenced line.",
    "allocation_recording_failure": "Inspect allocation format, dimensions, flags and alignment at the referenced line.",
    "recording_failure": "Inspect the unsupported command at the referenced line.",
    "null_compute_dispatch": "A substitute pipeline was dispatched; check missing translation before attributing visual defects.",
    "survey_assert_continued": "Inspect this title-relative address for the exact executable build. This assert was continued, not necessarily terminal.",
    "unsupported_vtable_slot": "Inspect the guest ABI call site and slot. This survey event is not proof of the final stop.",
    "unhandled_wine_exception": "Inspect the terminal exception and same-thread unwind. A module-relative address requires matching image metadata.",
    "device_loss_report": "Check matching driver/kernel diagnostics from this launch; device loss alone does not identify its cause.",
    "missing_import_library": "Inspect the explicitly named import and the staged provider or library dependencies.",
}
UNHANDLED = re.compile(r"^wine: (?:Unhandled exception (0x[0-9a-fA-F]+)|Unhandled page fault\b|Unimplemented function\b)")
MISSING_IMPORT = re.compile(r':err:module:import_dll Library ([a-zA-Z0-9_.-]{1,128}) \(which is needed by L"[^"]+"\) not found')
EXCEPTION_SITE = re.compile(r" at address (?:0x)?([0-9a-fA-F]{1,16})\b")
EXCEPTION_THREAD = re.compile(r"\(thread ([0-9a-fA-F]{1,8})\)")
DEVICE_LOST = re.compile(r"\b(?:VK_ERROR_DEVICE_LOST|DXGI_ERROR_DEVICE_(?:REMOVED|HUNG|RESET))\b")
MAX_LINE = 65536
MAX_BYTES = 512 * 1024 * 1024


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo is not None else None
    except ValueError:
        return None


def event(kind, number, time, detail=None, code=None):
    # No raw lines, paths or shader bytes in the report.
    result = {"kind": kind, "line": number, "utc": time}
    if detail is not None:
        result["detail"] = detail
    if code:
        result["code"] = code
        if code in HRESULT:
            result["meaning"] = HRESULT[code]
    return result


class Session:
    def __init__(self, number):
        self.first_line = number
        self.last_line = number
        self.start = None
        self.last_stamp = None
        self.clock_reversed = False
        self.milestones = {}
        self.findings = {}
        self.counts = Counter()
        self.shaders = {}
        self.shader_events = Counter()
        self.terminal = None

    def mark(self, ev):
        self.milestones.setdefault(ev["kind"], ev)

    def finding(self, ev):
        key = (ev["kind"], ev.get("code"), ev.get("detail", {}).get("rva"), ev.get("detail", {}).get("slot"), ev.get("detail", {}).get("library"))
        self.counts[key] += 1
        if key not in self.findings:
            self.findings[key] = {"first": ev, "last": ev}
        self.findings[key]["last"] = ev

    def consume(self, number, line):
        self.last_line = number
        stamp = STAMP.match(line)
        time = None
        if stamp:
            time = timestamp(stamp[1])
            line = stamp[2]
            if time:
                if self.last_stamp and datetime.fromisoformat(time) < datetime.fromisoformat(self.last_stamp):
                    self.clock_reversed = True
                self.start = self.start or time
                self.last_stamp = time
        if stamp and line == "Starting game":
            self.mark(event("game_start", number, time))
        if stamp and (ex := EXIT.fullmatch(line)):
            self.terminal = event("exit", number, time, {"code": int(ex[1]), "seconds": float(ex[2])})
        elif stamp and line == "Launch interrupted":
            self.terminal = event("interrupted", number, time)
        elif stamp and line.startswith("Launch failed:"):
            self.terminal = event("launch_failure", number, time)
        if stamp and (shader := SHADER.match(line)):
            key = shader[1] or shader[3]
            status = "compiled" if shader[3] else "refused" if shader[2] == "65" else "failed"
            self.shader_events[status] += 1
            self.shaders[key] = status

        match = PROVIDER.search(line)
        if match:
            detail = match[1]
            for kind, pattern in MILESTONES:
                if re.match(pattern, detail):
                    self.mark(event(kind, number, time))
            for kind, pattern in FAULTS:
                found = re.match(pattern, detail)
                if found:
                    self.finding(event(kind, number, time, code=found[1].lower() if found.lastindex else None))
            for kind, pattern in SURVEY:
                if found := pattern.match(detail):
                    if kind == "unsupported_vtable_slot":
                        data = {"slot": int(found[1]), "rva": found[2], "reported_hit": int(found[3])}
                    else:
                        data = {"rva": found[1], "reported_hit": int(found[2])}
                    self.finding(event(kind, number, time, data))
        if unhandled := UNHANDLED.search(line):
            site, thread = EXCEPTION_SITE.search(line), EXCEPTION_THREAD.search(line)
            data = {}
            if site:
                data["absolute_address"] = hex(int(site[1],16))
            if thread:
                data["wine_thread"] = hex(int(thread[1],16))
            self.finding(event("unhandled_wine_exception", number, time, data,
                               code=unhandled[1].lower() if unhandled[1] else None))
        if missing := MISSING_IMPORT.search(line):
            self.finding(event("missing_import_library", number, time, {"library": missing[1].lower()}))
        if DEVICE_LOST.search(line):
            self.finding(event("device_loss_report", number, time))

    def report(self):
        ordered = sorted(self.milestones.values(), key=lambda e:e["line"])
        findings = [{"count": self.counts[key], **value} for key,value in self.findings.items()]
        findings.sort(key=lambda x:x["first"]["line"])
        for finding in findings:
            finding["next_check"] = GUIDANCE[finding["last"]["kind"]]
        last = self.terminal
        if last:
            outcome = ("normal_exit" if last["kind"] == "exit" and last["detail"]["code"] == 0 else
                       "nonzero_exit" if last["kind"] == "exit" else last["kind"])
        elif any(k[0] == "unhandled_wine_exception" for k in self.findings):
            outcome = "unhandled_exception_observed_no_exit_record"
        else:
            outcome = "incomplete_log_no_exit_record"
        # Terminal-like evidence is shown first, then diagnostics closest to
        # the end by line number, not guessed causal likelihood.
        investigation = sorted(findings, key=lambda f: (
            f["last"]["kind"] not in ("unhandled_wine_exception", "device_loss_report"),
            -f["last"]["line"]))[:5]
        return {"first_line": self.first_line, "last_line": self.last_line,
                "first_launcher_timestamp": self.start, "last_launcher_timestamp": self.last_stamp,
                "timestamp_order_reversed": self.clock_reversed, "outcome":outcome,
                "terminal":last, "milestones":ordered,
                "last_milestone":ordered[-1]["kind"] if ordered else None,
                "findings":findings, "investigation_order":investigation, "shader_events":dict(self.shader_events),
                "last_observed_shader_status":dict(Counter(self.shaders.values())),
                "unique_shader_requests":len(self.shaders)}


def analyze(path):
    sessions = []
    current = None
    digest = hashlib.sha256()
    total = 0
    number = 0
    long_lines = 0
    with path.open("rb") as stream:
        while raw := stream.readline(MAX_LINE+1):
            number += 1
            digest.update(raw)
            total += len(raw)
            if len(raw) > MAX_LINE:
                long_lines += 1
                while not raw.endswith(b"\n"):
                    raw = stream.readline(MAX_LINE+1)
                    if not raw:
                        break
                    total += len(raw)
                    digest.update(raw)
                    if total > MAX_BYTES:
                        raise ValueError("log exceeds 512 MiB limit")
                if total > MAX_BYTES:
                    raise ValueError("log exceeds 512 MiB limit")
                continue
            if total > MAX_BYTES:
                raise ValueError("log exceeds 512 MiB limit")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            stamp = STAMP.match(line)
            if current is None or (stamp and stamp[2].startswith("Game log: ")):
                if current is not None:
                    sessions.append(current.report())
                current = Session(number)
            current.consume(number,line)
    if current is not None:
        sessions.append(current.report())
    if not sessions:
        raise ValueError("no readable log records")
    return {"schema":1,"log_sha256":digest.hexdigest(),"lines":number,
            "oversize_lines_skipped":long_lines,"sessions":sessions,
            "scope":"Observed launch milestones and candidate fault evidence only; not proof of causal failure, hang, visible image or gameplay."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = analyze(args.log)
        with args.json.open("x",encoding="utf-8") as stream:
            json.dump(report,stream,indent=2)
            stream.write("\n")
    except (OSError,ValueError) as exc:
        parser.error(str(exc))
    for n,session in enumerate(report["sessions"],1):
        print(f"Session {n}: {session['outcome']}; last milestone: {session['last_milestone']}")
        for f in session["investigation_order"]:
            e=f["last"]
            print(f"  {e['kind']} {e.get('code','')} (last line {e['line']}; {f['count']} log events)")
            print("    "+f["next_check"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
