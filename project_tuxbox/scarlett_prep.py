"""Compile ProjectScarlett's recorded shader requests before launching the title."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic

from .inspect import digest
from .prep_progress import PrepPhase, desktop_progress, report_progress
from .runtime_shaders import REQUEST_PATTERNS, ShaderRefused, compile_request, snapshot_compiler_cache
from .shader_prep import SOURCE_CACHE
from .shader_diagnostics import normalize_refusal


def scarlett_requests(executable, cache):
    if executable.name.casefold() != "game_scarlett_master.exe" or not executable.with_name("shaders.rpf").is_file():
        return None
    return cache / "projectscarlett" / digest(executable) / "requests"


def prepare_scarlett(executable, host):
    if (host.env.get("TUXBOX_RUNTIME_SHADERS") == "0"
            or not executable.with_name("d3d12_xs.dll").is_file()):
        return False
    cache_root = Path(host.env["WINEPREFIX"]) / SOURCE_CACHE.parent
    archive = scarlett_requests(executable, cache_root)
    if archive is None:
        return False
    sources = sorted(
        path for path in archive.iterdir()
        if path.suffix in REQUEST_PATTERNS and re.fullmatch(REQUEST_PATTERNS[path.suffix], path.stem)
    ) if archive.is_dir() else []
    if not sources:
        report_progress(None, "ProjectScarlett shader preparation will start after the game records its first shader requests.")
        return True

    started = monotonic()
    totals = Counter()
    reasons = Counter()
    # Use the runtime toolchain and cache so the guest can load both translations
    # and deterministic refusals directly, including requests with a pipeline root.
    env = {"TUXBOX_SHADER_PREP_UI": "1", **host.env}
    with desktop_progress("ProjectScarlett", env) as progress, TemporaryDirectory(prefix="tuxbox-prep-") as tmp:
        report_progress(progress, "Preparing ProjectScarlett shaders")
        tools, cache = snapshot_compiler_cache(Path(tmp) / "xshader", cache_root)
        phase = PrepPhase(progress, "Previously used Xbox shaders", len(sources))

        def compile_one(source):
            try:
                compiled = compile_request(source, source.stem, tools, cache, None)
                return ("compiled" if compiled else "cached"), None, None
            except ShaderRefused as exc:
                if exc.cached:
                    return "unsupported_cached", None, None
                return "unsupported", None, normalize_refusal(str(exc))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return "failed", f"{source.name}: {exc}", None

        workers = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1))
        try:
            pending = [workers.submit(compile_one, source) for source in sources]
            for completed, result in enumerate(as_completed(pending), 1):
                category, error, reason = result.result()
                totals[category] += 1
                if reason is not None:
                    reasons[reason] += 1
                if error and totals["failed"] <= 3:
                    report_progress(progress, f"ProjectScarlett shader preparation: {error}", completed, len(sources))
                phase.update(completed, totals["compiled"],
                             unsupported=totals["unsupported"] + totals["unsupported_cached"], failed=totals["failed"])
        finally:
            workers.shutdown(cancel_futures=True)

        elapsed = monotonic() - started
        unsupported = totals["unsupported"] + totals["unsupported_cached"]
        report_progress(progress, f"ProjectScarlett shader preparation took {elapsed:.1f}s. "
                        f"{totals['compiled']} compiled, {totals['cached']} cached, "
                        f"{unsupported} unsupported, {totals['failed']} failed.", 1, 1)
    if unsupported:
        report_progress(None, f"ProjectScarlett shader refusals: {totals['unsupported']} new, "
                        f"{totals['unsupported_cached']} cached (reason unavailable).")
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:10]:
            report_progress(None, f"  {count} x {reason}")
    return True
