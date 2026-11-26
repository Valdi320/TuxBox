"""Bounded snapshots of local shader cache files."""
import os
import stat


MAX_SHADER_BYTES = 64 << 20


def bounded_regular(path, limit):
    """Snapshot a regular file, without following links or blocking on FIFOs."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path.name} is not a regular shader file")
        if info.st_size > limit:
            raise ValueError("shader exceeds the 64 MiB runtime limit" if limit == MAX_SHADER_BYTES
                             else "file exceeds the cache snapshot limit")
        data = stream.read(info.st_size + 1)
    if len(data) != info.st_size:
        raise ValueError("file size changed during cache snapshot")
    return data


def bounded_shader(path):
    """Snapshot a regular shader under the provider's 64 MiB limit."""
    return bounded_regular(path, MAX_SHADER_BYTES)


def exact_marker(path, expected):
    """Read a tiny regular marker, without following links or blocking."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            return (stat.S_ISREG(info.st_mode) and info.st_size == len(expected)
                    and stream.read(len(expected) + 1) == expected)
    except OSError:
        return False
