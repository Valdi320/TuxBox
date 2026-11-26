"""Opt-in aggregate timing of the host shader pipeline; never store title identifiers."""
from collections import Counter
from contextvars import ContextVar
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic_ns


_active = ContextVar("shader_profile", default=None)
_stage = ContextVar("shader_profile_stage", default=None)


class DeferredShader(Exception):
    """Retry a translated cold request when a large-compile slot is free."""


class ShaderProfile:
    def __init__(self):
        self._start = monotonic_ns()
        self._lock = Lock()
        self._requests = Counter()
        self._stages = {}
        self._reuse = Counter()
        self._memory = {}

    def record(self, stage, elapsed, outcome):
        with self._lock:
            stats = self._stages.setdefault(stage, [0, 0, Counter()])
            stats[0] += elapsed
            stats[1] = max(stats[1], elapsed)
            stats[2][outcome] += 1

    def request(self, function, refusal_type, *args, **kwargs):
        token = _active.set(self)
        started = monotonic_ns()
        outcome = "failed"
        try:
            result = function(*args, **kwargs)
            outcome = "compiled" if result else "cached"
            return result
        except DeferredShader:
            outcome = "deferred"
            raise
        except refusal_type as error:
            outcome = "cached_refusal" if error.cached else "refused"
            raise
        finally:
            _active.reset(token)
            elapsed = monotonic_ns() - started
            with self._lock:
                self._requests[outcome] += 1
            self.record("request_total", elapsed, outcome)

    def reused(self, kind):
        with self._lock:
            self._reuse[kind] += 1

    def record_memory(self, stage, peak):
        with self._lock:
            stats = self._memory.setdefault(stage, {
                "attempts": 0, "measured": 0, "max_peak_rss_kib": None,
            })
            stats["attempts"] += 1
            if peak is not None:
                stats["measured"] += 1
                stats["max_peak_rss_kib"] = max(stats["max_peak_rss_kib"] or 0, peak)

    def summary(self):
        with self._lock:
            return {
                "schema": 1,
                "elapsed_ms": round((monotonic_ns() - self._start) / 1e6, 3),
                "requests": dict(self._requests),
                "reuse": dict(self._reuse),
                "process_memory": {name: dict(stats) for name, stats in sorted(self._memory.items())},
                "stages": {
                    name: {
                        "calls": sum(counts.values()),
                        "total_ms": round(total / 1e6, 3),
                        "max_ms": round(maximum / 1e6, 3),
                        "outcomes": dict(counts),
                    }
                    for name, (total, maximum, counts) in sorted(self._stages.items())
                },
            }

    def report(self, label, emit):
        emit(f"Shader stage timings ({label}): " +
             json.dumps(self.summary(), sort_keys=True, separators=(",", ":")))


def timed(stage, function, *args, **kwargs):
    profile = _active.get()
    if profile is None:
        return function(*args, **kwargs)
    started = monotonic_ns()
    outcome = "error"
    token = _stage.set(stage)
    try:
        result = function(*args, **kwargs)
        outcome = "ok"
        return result
    except (TimeoutError, subprocess.TimeoutExpired):
        outcome = "timeout"
        raise
    finally:
        _stage.reset(token)
        profile.record(stage, monotonic_ns() - started, outcome)


def count_reuse(kind):
    profile = _active.get()
    if profile is not None:
        profile.reused(kind)


@lru_cache(maxsize=1)
def gnu_time():
    """GNU time reaps the tool in a small independent process."""
    executable = shutil.which("time")
    if executable:
        try:
            result = subprocess.run([executable, "--version"], capture_output=True, timeout=2)
            if result.returncode == 0 and b"GNU Time" in result.stdout + result.stderr:
                return executable
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def read_usage(path, status):
    """GNU time reports KiB and exit status; signalled tools have %x=0."""
    try:
        peak, code = map(int, path.read_text(encoding="ascii").split())
        if peak < 0 or not 0 <= code <= 255:
            return None, status
        if status >= 128 and code == 0:
            return peak, -(status - 128)
        if code == status:
            return peak, status
    except (OSError, ValueError):
        pass
    return None, status


def run_profiled_tool(command, *, timeout=None):
    """Measure CLI peak RSS using Linux child resource accounting, when enabled.

    Running wait4 in our Python process can inherit its high-water RSS into
    the child on Linux. GNU time execs first, then waits for the tool itself.
    No tool is rerun, and a timed-out wrapper and its descendants are killed
    together. An unavailable measurement is reported as null.
    """
    options = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   text=True, errors="replace")
    profile, stage = _active.get(), _stage.get()
    if profile is None or stage is None or sys.platform != "linux":
        return subprocess.run(command, timeout=timeout, **options)
    peak = None
    try:
        timer = gnu_time()
        # Let subprocess report an absent or inaccessible executable in the
        # usual way, rather than turning a launch error into a tool exit code.
        if timer is None or shutil.which(os.fspath(command[0])) is None:
            return subprocess.run(command, timeout=timeout, **options)
        with TemporaryDirectory(prefix="tuxbox-rss-") as directory:
            receipt = Path(directory) / "usage"
            wrapped = [timer, "-q", "-f", "%M %x", "-o", str(receipt), "--", *command]
            try:
                process = subprocess.Popen(wrapped, start_new_session=True, **options)
            except OSError:
                # The optional timer may have disappeared after discovery.
                # Popen did not start a child, so running the tool is safe.
                return subprocess.run(command, timeout=timeout, **options)
            with process:
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except BaseException as error:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    if isinstance(error, subprocess.TimeoutExpired):
                        raise subprocess.TimeoutExpired(command, timeout,
                                                        output=error.output, stderr=error.stderr) from error
                    raise
                peak, code = read_usage(receipt, process.returncode)
                # GNU time uses 126/127 when exec fails. The compiler's own
                # errors with these codes are also retryable; neither may
                # become a persistent unsupported translation.
                return subprocess.CompletedProcess(command, code, stdout, stderr)
    finally:
        profile.record_memory(stage, peak)
