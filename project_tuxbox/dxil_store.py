"""Reuse validated DXIL when a compiler update leaves a shader's HLSL unchanged.

The runtime cache is keyed by the translator binary, so each translator change
starts an empty cache. Most shaders still translate to the same HLSL, and DXC
is nearly all of the compile time. This store keys DXIL by what DXC's output
depends on: the HLSL, the target profile, and the DXC and validator in use.
"""
from collections import Counter
import hashlib
import os
from pathlib import Path
import re
from tempfile import mkstemp

from .inspect import digest
from .shader_prep import has_dxil_container
from .shader_blob import MAX_SHADER_BYTES, bounded_shader, exact_marker, bounded_regular


# Bump when a policy change can make cached DXIL unacceptable under validation.
# Output-equivalent optimisation and fallback variants share the policy value.
POLICY = 1
IDENTITY_FILE = "dxil-identity"
LIBRARIES = ("libdxcompiler*", "libdxil*", "dxcompiler.dll", "dxil.dll")
reused = Counter()

# Compute programs that fix their wave size. Unoptimised, their DXIL has about
# four times the code blocks, and vkd3d-proton's control-flow pass takes minutes
# on the largest: 364 s against 30 s for one ProjectScarlett program. At default
# settings DXC spends nearly all its time in Jump Threading instead, which turning
# lifetime markers off avoids.
COMPUTE_FLAGS = ("-O1", "-disable-lifetime-markers")
# Targets whose cached DXIL depends on the flags above.
VARIANTS = {"cs_6_6": " ".join(COMPUTE_FLAGS)}


def write_identity(cache, version, dxc, dxv):
    """Record which DXC toolchain fills this runtime cache. This turns reuse on."""
    dxc, dxv = Path(dxc), Path(dxv)
    libraries = sorted({path for directory in (dxc.parent, dxc.parent.parent / "lib")
                        for pattern in LIBRARIES for path in directory.glob(pattern) if path.is_file()})
    identity = hashlib.sha256(f"{POLICY}\n".encode() + version)
    for tool in (dxc, dxv, *libraries):
        identity.update(bytes.fromhex(digest(tool)))
    _publish(cache / IDENTITY_FILE, identity.hexdigest().encode("ascii"))


def _publish(path, data):
    """Replace `path` atomically with `data`."""
    handle, pending = mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(pending, path)
    finally:
        Path(pending).unlink(missing_ok=True)


class DxilStore:
    def __init__(self, root):
        self.root = root

    @classmethod
    def for_cache(cls, cache):
        """The store behind `cache`, or None when reuse is not enabled for it."""
        try:
            identity = bounded_regular(cache / IDENTITY_FILE, 64).decode("ascii")
            if not re.fullmatch(r"[a-f0-9]{64}", identity):
                return None
            # <cache root>/runtime/<translator fingerprint> -> <cache root>/dxil/<toolchain>
            root = cache.parent.parent / "dxil" / identity
            root.mkdir(parents=True, exist_ok=True)
        except (OSError, UnicodeError, ValueError):
            return None
        return cls(root)

    @staticmethod
    def key(hlsl, target):
        tag = f"{target} {VARIANTS[target]}" if target in VARIANTS else target
        digest = hashlib.sha256(f"{tag}\n".encode("ascii"))
        digest.update(hlsl)
        return digest.hexdigest()

    def fetch(self, key, output):
        """Return and stage one bounded, verified snapshot of stored DXIL."""
        try:
            data = bounded_shader(self.root / f"{key}.cso")
            checksum = hashlib.sha256(data).hexdigest().encode("ascii")
            if exact_marker(self.root / f"{key}.sha256", checksum) and has_dxil_container(data):
                output.write_bytes(data)
                reused["dxil"] += 1
                return data
        except (OSError, ValueError):
            pass
        output.unlink(missing_ok=True)
        return None

    def save(self, key, data):
        """Keep already-validated DXIL bytes. Failure only costs a later compile."""
        if len(data) > MAX_SHADER_BYTES:
            return
        try:
            _publish(self.root / f"{key}.cso", data)
            _publish(self.root / f"{key}.sha256", hashlib.sha256(data).hexdigest().encode("ascii"))
        except OSError:
            pass

    def is_slow(self, request, target):
        """Whether this request's optimised compile has timed out before."""
        return (self.root / f"{request}.{target}.slow").is_file()

    def mark_slow(self, request, target):
        try:
            (self.root / f"{request}.{target}.slow").touch()
        except OSError:
            pass
