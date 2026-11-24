"""Compile Xbox shaders and link raster stages requested by a running Proton game."""
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_futures
import errno
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread

from .inspect import digest
from .shader_prep import SOURCE_CACHE, ToolFailure, compiler_tools, has_dxil_container, run_tool


MAX_SHADER_BYTES = 64 << 20
XSHADER_UNSUPPORTED_HLSL = 65
SOURCE_KEY = r"[a-f0-9]{64}"
STENCIL_KEY = SOURCE_KEY + r"_rt[0-7]"
LINK_KEY = "_".join([SOURCE_KEY] * 3)
REQUEST_PATTERNS = {".link": LINK_KEY, ".stencil": STENCIL_KEY, ".dxbc": SOURCE_KEY}


class ShaderRefused(ValueError):
    def __init__(self, message, *, cached):
        super().__init__(message)
        self.cached = cached


def verified_input(path, expected):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path.name} is not a regular shader file")
        if info.st_size > MAX_SHADER_BYTES:
            raise ValueError("shader exceeds the 64 MiB runtime limit")
        data = stream.read(MAX_SHADER_BYTES + 1)
    if len(data) > MAX_SHADER_BYTES or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"{path.name} contents differ from the requested digest")
    return data


def portable_stage(data):
    if not has_dxil_container(data):
        return None
    count, = struct.unpack_from("<I", data, 28)
    stages = [struct.unpack_from("<H", data, offset + 10)[0]
              for offset, in struct.iter_unpack("<I", memoryview(data)[32:32 + count * 4])
              if data[offset:offset + 4] == b"DXIL"]
    return stages[0] if len(stages) == 1 else None


def portable_pixel(data):
    return portable_stage(data) == 0  # D3D12_SHVER_PIXEL_SHADER


def archive_request(archive, source, inputs):
    """Publish the exact verified inputs for a later launch's compiler."""
    dest = archive / source.name
    if dest.exists():
        return
    try:
        if source.suffix == ".link":
            staged = inputs[0].parent / "record"
            staged.mkdir()
            for path, name in zip(inputs, ("producer.dxbc", "pixel.dxbc", "pixel.cso")):
                os.link(path, staged / name)
            staged.rename(dest)
        else:
            os.link(inputs[0], dest)
    except OSError as exc:
        if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise


def compile_request(source, key, tools, cache, session, *, remember=None):
    linked = source.suffix == ".link"
    xshader, dxc, dxv = tools
    with TemporaryDirectory(dir=cache) as tmp:
        work = Path(tmp)
        flags = []
        if linked:
            if not stat.S_ISDIR(source.lstat().st_mode):
                raise ValueError("linked shader request must be a directory")
            producer_hash, pixel_hash, dxil_hash = key.split("_")
            cooked = work / "producer.dxbc"
            cooked.write_bytes(verified_input(source / "producer.dxbc", producer_hash))
            pixel = work / "pixel.dxbc"
            pixel.write_bytes(verified_input(source / "pixel.dxbc", pixel_hash))
            pixel_code = verified_input(source / "pixel.cso", dxil_hash)
            if not portable_pixel(pixel_code):
                raise ValueError("pixel order requires a portable pixel DXIL container")
            order = work / "pixel.cso"
            order.write_bytes(pixel_code)
            flags = ["--link-pixel", pixel, "--pixel-order", order]
            inputs = (cooked, pixel, order)
        else:
            cooked = work / "input.dxbc"
            if source.suffix == ".stencil":
                source_hash, slot = key.split("_rt")
                flags = ["--stencil-target", slot]
            else:
                source_hash = key
            cooked.write_bytes(verified_input(source, source_hash))
            inputs = (cooked,)

        if remember:
            remember(source, inputs)

        dest = cache / (key + ".cso")
        checksum = dest.with_suffix(".sha256")
        refusal = dest.with_suffix(".unsupported")
        reply = work / "reply"
        if (dest.is_file() and dest.stat().st_size <= MAX_SHADER_BYTES
                and checksum.is_file() and checksum.stat().st_size == 64):
            # Check the exact bytes that will be returned even if a concurrent
            # launch replaces the persistent entry while this worker is reading.
            shutil.copyfile(dest, reply)
            if checksum.read_bytes() == digest(reply).encode("ascii"):
                if session is not None:
                    os.replace(reply, session / dest.name)
                return False

        if refusal.is_file() and refusal.stat().st_size == len(key) and refusal.read_bytes() == key.encode("ascii"):
            raise ShaderRefused("cached unsupported HLSL translation", cached=True)

        hlsl, output, profile = (work / name for name in ("shader.hlsl", "out.cso", "profile"))
        if linked:
            run_tool([dxv, order], timeout=10)
        try:
            run_tool([xshader, cooked, *flags, "--hlsl-out", hlsl, "--profile-out", profile], timeout=30)
        except ToolFailure as error:
            # xshader reserves this status for the in-process HLSL emitter. DXC, DXV,
            # input parsing, tool startup and filesystem failures remain retryable.
            if error.returncode != XSHADER_UNSUPPORTED_HLSL:
                raise
            marker = work / "unsupported"
            marker.write_text(key, encoding="ascii")
            os.replace(marker, refusal)
            raise ShaderRefused(str(error), cached=False) from error
        target = profile.read_text().strip()
        # Mesh and amplification stages start at shader model 6.5. Linked
        # requests currently repack the outputs of vertex and geometry shaders.
        profiles = r"(vs|gs)_6_[016]" if linked else r"((cs|ps|vs|gs)_6_[016]|cs_6_[45]|(ms|as)_6_[56]|lib_6_3)"
        if not re.fullmatch(profiles, target):
            raise ValueError(f"unsupported compiler profile {target!r}")
        entry = [] if target == "lib_6_3" else ["-E", "main"]
        # cs_6_6 programs from this boot either finish default optimisation in
        # under a second or sit in it until the cap. The ones that sit there
        # compile at -O0 in a few seconds, and the driver optimises that DXIL
        # again when it builds the pipeline.
        if target == "cs_6_6":
            run_tool([dxc, "-T", target, *entry, "-O0", "-Fo", output, hlsl], timeout=15)
        else:
            try:
                run_tool([dxc, "-T", target, *entry, "-Fo", output, hlsl], timeout=15)
            except subprocess.TimeoutExpired:
                output.unlink(missing_ok=True)
                run_tool([dxc, "-T", target, *entry, "-O0", "-Fo", output, hlsl], timeout=15)
        run_tool([dxv, output], timeout=10)
        if not has_dxil_container(output.read_bytes()):
            raise ValueError("compiler returned an invalid DXIL container")
        stamp = work / "sha256"
        stamp.write_text(digest(output))
        if session is not None:
            shutil.copyfile(output, reply)
        os.replace(output, dest)
        os.replace(stamp, checksum)
        if session is not None:
            os.replace(reply, session / dest.name)
    return True


def remove_request(path):
    if not path.is_symlink() and path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def serve(session, stop, report, archive=None, jobs=1):
    # Cargo can replace the release executable while another title is running.
    # Keep the executable we fingerprint private to this host worker.
    with TemporaryDirectory(prefix="tuxbox-xshader-") as tmp, request_notifications(session / "requests", stop) as wait:
        _serve_requests(session, stop, report, Path(tmp) / "xshader", wait, archive, jobs)


@contextmanager
def request_notifications(directory, stop):
    # The Proton guest publishes both shader files and complete link directories
    # by renaming them into this directory. Install the watch before scanning.
    import ctypes
    import select
    libc = ctypes.CDLL(None)
    init = getattr(libc, "inotify_init1", None)
    fd = init(os.O_NONBLOCK | os.O_CLOEXEC) if init else -1
    if fd < 0:
        yield lambda: stop.wait(0.05)
        return
    try:
        moved_to = 0x00000080
        if libc.inotify_add_watch(fd, os.fsencode(directory), moved_to) < 0:
            yield lambda: stop.wait(0.05)
            return
        changes = select.poll()
        changes.register(fd, select.POLLIN)

        def wait():
            # A bounded fallback scan also handles filesystems without events
            # and lets shutdown finish when there is no outstanding work.
            if changes.poll(50):
                os.read(fd, 65536)

        yield wait
    finally:
        os.close(fd)


def snapshot_compiler_cache(xshader, cache_root):
    """Pin the executable used for all requests under this toolchain fingerprint."""
    compiler, dxc, dxv = compiler_tools()
    shutil.copyfile(compiler, xshader)
    xshader.chmod(0o700)
    tools = (xshader, dxc, dxv)
    version = subprocess.check_output([dxc, "--version"], timeout=10)
    fingerprint = hashlib.sha256(version)
    for tool in tools:
        fingerprint.update(bytes.fromhex(digest(Path(tool))))
    cache = cache_root / "runtime" / fingerprint.hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    return tools, cache


def runtime_jobs(env, report):
    """Bound live compiler subprocesses; honor the launcher's CPU affinity."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    cap = max(1, min(8, cpus))
    default = min(4, cap)
    value = env.get("TUXBOX_RUNTIME_SHADER_JOBS")
    if value is None:
        return default
    try:
        jobs = int(value)
        if not 1 <= jobs <= 8:
            raise ValueError
    except ValueError:
        report(f"Invalid TUXBOX_RUNTIME_SHADER_JOBS={value!r}; using {default}")
        return default
    return min(jobs, cap)


def ordered_requests(directory):
    """Mix ordinary, linked and stencil requests in publication order."""
    entries = []
    for source in directory.iterdir():
        if source.suffix not in REQUEST_PATTERNS:
            continue
        try:
            timestamp = source.lstat().st_mtime_ns
        except FileNotFoundError:
            continue
        entries.append((timestamp, source.name, source))
    return [source for _, _, source in sorted(entries)]


def _serve_requests(session, stop, report, xshader, wait, archive, jobs=1):
    def bind_cache():
        tools, cache = snapshot_compiler_cache(xshader, session.parent)
        # Publish only after the exact compiler used by this worker is pinned.
        pending = session / "compiler-id.tmp"
        pending.write_text(cache.name, encoding="ascii")
        pending.replace(session / "compiler-id")
        return tools, cache

    tools = cache = None
    if (session.parent / "runtime").is_dir():
        try:
            tools, cache = bind_cache()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # Retry and report the error if this game needs a shader.
    refused = cached_refusals = 0
    record_requests = archive is not None
    archive_lock = Lock()
    archive_errors = []

    def remember(source, inputs):
        nonlocal archive
        with archive_lock:
            if archive is not None:
                try:
                    archive_request(archive, source, inputs)
                except OSError as exc:
                    archive_errors.append(f"Could not save ProjectScarlett shader requests for the next launch: {exc}")
                    archive = None

    def compile_one(source, key, bound_tools, bound_cache):
        # Only this job owns the source. Archive publication is separately
        # serialized; compiler temporary directories are private per request.
        if record_requests:
            return compile_request(source, key, bound_tools, bound_cache, session, remember=remember)
        return compile_request(source, key, bound_tools, bound_cache, session)

    def complete(source, key, future=None, error=None):
        nonlocal refused, cached_refusals
        failed = False
        try:
            if error is not None:
                raise error
            if future.result():
                report(f"Compiled Xbox shader {key}")
        except ShaderRefused as exc:
            failed = True
            if exc.cached:
                cached_refusals += 1
            else:
                refused += 1
                report(f"Xbox shader {key}: {exc}")
        except Exception as exc:
            # A worker failure must release its guest waiter. Never turn a
            # transient tool or programming error into a persistent refusal.
            failed = True
            report(f"Xbox shader {key}: {exc}")
        finally:
            # Publish the terminal result before releasing the source name.
            # Once it disappears, the guest may clear the old failure and
            # publish a new generation of the same request.
            if failed:
                (session / (key + ".failed")).touch()
            remove_request(source)

    directory = session / "requests"
    active = {}
    report(f"Xbox shader worker: up to {jobs} concurrent requests")
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="xshader") as pool:
        while active or not stop.is_set():
            for key, (source, future) in list(active.items()):
                if future.done():
                    complete(source, key, future)
                    del active[key]
            with archive_lock:
                messages = archive_errors[:]
                archive_errors.clear()
            for message in messages:
                report(message)
            # No unbounded executor queue: on shutdown only running jobs
            # finish, before the session's files and pinned compiler disappear.
            if not stop.is_set() and len(active) < jobs:
                for source in ordered_requests(directory):
                    if stop.is_set() or len(active) >= jobs:
                        break
                    key = source.stem
                    if not re.fullmatch(REQUEST_PATTERNS[source.suffix], key):
                        remove_request(source)
                        continue
                    if key in active:
                        continue
                    if tools is None:
                        try:
                            tools, cache = bind_cache()
                        except (OSError, ValueError, subprocess.SubprocessError) as exc:
                            complete(source, key, error=exc)
                            continue
                    active[key] = (source, pool.submit(compile_one, source, key, tools, cache))
            if active:
                # Wake immediately when any worker completes, with a bounded
                # rescan interval for new requests. Do not spin during stop.
                wait_futures([future for _, future in active.values()],
                             timeout=0.05, return_when=FIRST_COMPLETED)
            elif not stop.is_set():
                wait()
    if refused or cached_refusals:
        report(f"Xbox shader worker: {refused} new unsupported translations, {cached_refusals} cached refusals")


@contextmanager
def runtime_shader_compiler(backend, env, executable, report):
    if backend != "proton" or env.get("TUXBOX_RUNTIME_SHADERS") == "0" or not executable.with_name("d3d12_xs.dll").is_file():
        yield
        return
    cache = Path(env["WINEPREFIX"]) / SOURCE_CACHE.parent
    from .scarlett_prep import scarlett_requests

    archive = None
    try:
        archive = scarlett_requests(executable, cache)
        if archive is not None:
            archive.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as exc:
        report(f"Could not open ProjectScarlett's shader inputs: {exc}")
        archive = None
    with shader_session(cache, env, report, archive=archive):
        yield


@contextmanager
def shader_session(cache, env, report, *, archive=None):
    cache.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="run-", dir=cache) as tmp:
        session = Path(tmp)
        (session / "requests").mkdir()
        env["TUXBOX_SHADER_SESSION"] = session.name
        stop = Event()
        worker = Thread(target=serve, args=(session, stop, report, archive, runtime_jobs(env, report)),
                        name="shader-compiler", daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join()
            env.pop("TUXBOX_SHADER_SESSION", None)
