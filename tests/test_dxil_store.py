from concurrent.futures import ThreadPoolExecutor
import hashlib
from threading import Barrier, Lock
from pathlib import Path
import struct
import subprocess
import sys
from time import monotonic, sleep
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from project_tuxbox.dxil_store import DxilStore, write_identity
from project_tuxbox.runtime_shaders import compile_request


def container(tag):
    bitcode = b"BC\xc0\xde" + tag.ljust(4, b"\0")
    # Shader kind 0 (pixel), model 6.0, matching the tests' pixel profiles.
    program = struct.pack("<II4sIII", 0x60, len(bitcode) // 4 + 6, b"DXIL", 0x100, 16, len(bitcode)) + bitcode
    chunk = struct.pack("<4sI", b"DXIL", len(program)) + program
    return b"DXBC" + bytes(16) + struct.pack("<IIII", 1, 36 + len(chunk), 1, 36) + chunk


@unittest.skipUnless(sys.platform == "linux", "Uses Proton request file verification")
class DxilReuse(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tools = tuple(self.root / name for name in ("xshader", "dxc", "dxv"))
        for path in self.tools:
            path.write_text(path.name)
        self.hlsl = "void main() {}"
        self.profile = "ps_6_0"
        self.timeouts = 0
        self.calls = []

    def cache(self, translator, toolchain=b"dxc 1"):
        cache = self.root / "runtime" / translator
        cache.mkdir(parents=True)
        if toolchain:
            write_identity(cache, toolchain, *self.tools[1:])
        return cache

    def run_tool(self, args, *, timeout):
        if args[0].name == "xshader":
            args[args.index("--hlsl-out") + 1].write_text(self.hlsl)
            args[args.index("--profile-out") + 1].write_text(self.profile)
        elif args[0].name == "dxc":
            self.calls.append("-O0" in args)
            if self.timeouts:
                self.timeouts -= 1
                raise subprocess.TimeoutExpired(args, timeout)
            args[args.index("-Fo") + 1].write_bytes(container(args[-1].read_bytes()[:4]))

    def compile(self, cache, data=b"shader"):
        key = hashlib.sha256(data).hexdigest()
        source = self.root / (key + ".dxbc")
        source.write_bytes(data)
        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=self.run_tool):
            self.assertTrue(compile_request(source, key, self.tools, cache, None))
        return (cache / (key + ".cso")).read_bytes()

    def test_output_built_with_other_compute_flags_is_not_reused(self):
        plain = hashlib.sha256(b"cs_6_6\nshader").hexdigest()
        self.assertNotEqual(DxilStore.key(b"shader", "cs_6_6"), plain)
        # Other targets keep their key, so their cached output stays usable.
        self.assertEqual(DxilStore.key(b"shader", "ps_6_0"), hashlib.sha256(b"ps_6_0\nshader").hexdigest())

    def test_new_translator_with_same_hlsl_skips_dxc(self):
        first = self.compile(self.cache("a"))
        second = self.compile(self.cache("b"))
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.root / "runtime/b" / (hashlib.sha256(b"shader").hexdigest() + ".sha256")).read_text(),
                         hashlib.sha256(second).hexdigest())

    def test_parallel_distinct_sources_share_identical_cold_hlsl(self):
        caches = [self.cache("a"), self.cache("b")]
        sources = []
        for i in range(2):
            data = f"distinct source {i}".encode()
            key = hashlib.sha256(data).hexdigest()
            source = self.root / (key + ".dxbc")
            source.write_bytes(data)
            sources.append((source, key))
        translated = Barrier(2)
        misses = Barrier(2)
        guard = Lock()
        original_fetch = DxilStore.fetch
        fetched = set()

        def run(args, *, timeout):
            self.run_tool(args, timeout=timeout)
            if args[0].name == "xshader":
                translated.wait(5)

        def fetch(store, key, output):
            # Force both initial lookups to miss before either can publish.
            result = original_fetch(store, key, output)
            with guard:
                first = output not in fetched
                fetched.add(output)
            if first:
                misses.wait(5)
            return result

        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run), \
                patch.object(DxilStore, "fetch", fetch), ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(compile_request, source, key, self.tools, cache, None)
                    for (source, key), cache in zip(sources, caches)]
            self.assertTrue(all(job.result(timeout=10) for job in jobs))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(*( (cache / (key + ".cso")).read_bytes()
                            for (_, key), cache in zip(sources, caches)))

    def test_content_lock_releases_after_failed_compile_and_never_caches_failure(self):
        cache = self.cache("a")
        original = self.run_tool
        def fail(args, *, timeout):
            if args[0].name == "dxc":
                raise OSError("transient compile failure")
            original(args, timeout=timeout)
        data = b"failed source"
        key = hashlib.sha256(data).hexdigest()
        source = self.root / (key + ".dxbc")
        source.write_bytes(data)
        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=fail):
            with self.assertRaisesRegex(OSError, "transient"):
                compile_request(source, key, self.tools, cache, None)
        self.assertFalse((cache / (key + ".unsupported")).exists())
        self.assertEqual(self.compile(self.cache("b"), b"new source"), container(b"void"))
        self.assertEqual(self.compile(cache, data), container(b"void"))
        self.assertEqual(len(self.calls), 1)

    def test_different_content_locks_do_not_serialize_unrelated_shaders(self):
        caches = [self.cache("a"), self.cache("b")]
        ready = Barrier(2)
        active = Barrier(2)
        def run(args, *, timeout):
            if args[0].name == "xshader":
                args[args.index("--hlsl-out") + 1].write_bytes(args[1].read_bytes())
                args[args.index("--profile-out") + 1].write_text("ps_6_0")
                ready.wait(5)
            elif args[0].name == "dxc":
                active.wait(5)  # Both independent content keys must proceed.
                self.run_tool(args, timeout=timeout)
        sources = []
        for i in range(2):
            data = f"distinct source {i}".encode()
            key = hashlib.sha256(data).hexdigest()
            source = self.root / (key + ".dxbc")
            source.write_bytes(data)
            sources.append((source, key))
        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run), ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(compile_request, source, key, self.tools, cache, None)
                    for (source, key), cache in zip(sources, caches)]
            self.assertTrue(all(job.result(timeout=10) for job in jobs))
        self.assertEqual(len(self.calls), 2)

    def test_content_lock_coordinates_independent_processes(self):
        from project_tuxbox.runtime_shaders import compile_lock
        store = DxilStore.for_cache(self.cache("a"))
        key = "a" * 64
        entered = self.root / "entered"
        attempted = self.root / "attempted"
        program = (
            "from pathlib import Path; import sys; "
            "from project_tuxbox.runtime_shaders import compile_lock; "
            "Path(sys.argv[3]).touch(); "
            "lock = compile_lock(Path(sys.argv[1]), sys.argv[2], timeout=5); "
            "lock.__enter__(); Path(sys.argv[4]).touch(); lock.__exit__(None, None, None)"
        )
        with compile_lock(store.root, key):
            child = subprocess.Popen([sys.executable, "-c", program, str(store.root), key,
                                      str(attempted), str(entered)],
                                     cwd=Path(__file__).resolve().parents[1],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = monotonic() + 3
                while not attempted.exists() and monotonic() < deadline:
                    sleep(.005)
                self.assertTrue(attempted.exists())
                sleep(.1)
                self.assertFalse(entered.exists())
            except BaseException:
                child.kill()
                child.communicate()
                raise
        try:
            _, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertTrue(entered.exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_changed_hlsl_profile_or_toolchain_recompiles(self):
        self.compile(self.cache("a"))
        self.hlsl = "void main() { }"
        self.compile(self.cache("b"))
        self.profile = "ps_6_6"
        self.compile(self.cache("c"))
        self.compile(self.cache("d", b"dxc 2"))
        self.assertEqual(len(self.calls), 4)

    def test_cache_without_an_identity_never_reuses(self):
        self.compile(self.cache("a", None))
        self.compile(self.cache("b", None))
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.root / "dxil").exists())

    def test_damaged_entry_is_recompiled(self):
        self.compile(self.cache("a"))
        stored, = (self.root / "dxil").glob("*/*.cso")
        stored.write_bytes(container(b"bad!"))
        self.compile(self.cache("b"))
        self.assertEqual(len(self.calls), 2)
        self.compile(self.cache("c"))
        self.assertEqual(len(self.calls), 2, "the recompile replaced the damaged entry")

    def test_timed_out_request_goes_straight_to_o0_afterwards(self):
        self.timeouts = 1
        self.compile(self.cache("a"))
        self.assertEqual(self.calls, [False, True])
        self.hlsl = "void main() { }"
        self.compile(self.cache("b"))
        self.assertEqual(self.calls, [False, True, True])
        self.compile(self.cache("c"), b"another")
        self.assertEqual(len(self.calls), 3, "same HLSL is reused across requests")

    def test_identity_follows_the_toolchain_files(self):
        cache = self.cache("a")
        before = DxilStore.for_cache(cache).root
        write_identity(cache, b"dxc 1", *self.tools[1:])
        self.assertEqual(DxilStore.for_cache(cache).root, before)
        self.tools[2].write_text("another validator")
        write_identity(cache, b"dxc 1", *self.tools[1:])
        self.assertNotEqual(DxilStore.for_cache(cache).root, before)


if __name__ == "__main__":
    unittest.main()
