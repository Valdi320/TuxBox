"""Shared shader refusal grouping for preparation and offline triage."""
import re

MASKS = (
    # "[buffer_load_dwordx3 @ 0x1a0]" names one variant of the same gap.
    (re.compile(r"\s*\[[a-z0-9_]+ @ 0x[0-9a-fA-F]+\]"), ""),
    (re.compile(r"0x[0-9a-fA-F]+"), "0x_"),
    (re.compile(r"\b([svt])\[?\d+(?::\d+)?\]?"), r"\1N"),
    (re.compile(r"\b\d+\b"), "N"),
)


def normalize_refusal(message):
    """One line per refusal, with program counters, registers and counts masked."""
    message = re.sub(r"^xshader(?:\.exe)? failed \(65\):\s*", "", message)
    line = next((part.strip() for part in reversed(message.splitlines()) if part.strip()), "")
    for pattern, replacement in MASKS:
        line = pattern.sub(replacement, line)
    return line or "(no message)"
