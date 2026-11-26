"""Identify the staged graphics provider used as input to a launch.

This checks files, not the Windows loader's choice of module. Never deploy or
replace a DLL implicitly; a developer may deliberately run an older build.
"""
from pathlib import Path
from .inspect import digest

BUILT = Path(__file__).resolve().parents[1] / "build/d3d12-smoke/d3d12_xs.dll"


def report_provider(executable, report, *, require_current=False, built=BUILT):
    def problem(message):
        report(message)
        if require_current:
            raise ValueError(message)

    try:
        candidates = [p for p in executable.parent.iterdir()
                      if p.name.casefold() == "d3d12_xs.dll"]
        if len(candidates) > 1:
            return problem("D3D12 provider: ambiguous staged filenames")
        if not candidates or not candidates[0].is_file():
            if require_current:
                return problem("D3D12 provider: staged DLL missing")
            return
        staged = candidates[0]
        actual = digest(staged)
        report(f"D3D12 staged provider: sha256={actual}")
        if not built.is_file():
            return problem("D3D12 provider: local build unavailable for comparison")
        expected = digest(built)
        report(f"D3D12 local build: sha256={expected}")
        if actual != expected:
            return problem("D3D12 provider mismatch: staged DLL differs from local build; "
                           "deploy deliberately before comparing revisions")
        report("D3D12 staged provider matches local build")
    except OSError as exc:
        problem(f"D3D12 provider: could not read staged/build identity ({type(exc).__name__})")
