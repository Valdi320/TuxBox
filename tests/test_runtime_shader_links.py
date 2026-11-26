import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from project_tuxbox.runtime_shaders import COMPUTE_TIMEOUT, O0_TIMEOUT, O0_TIMEOUT_MAX, ShaderRefused, archive_request, compile_dxil, compile_request, o0_timeout, serve, snapshot_compiler_cache
from project_tuxbox.scarlett_prep import scarlett_requests
from project_tuxbox.shader_prep import SOURCE_CACHE, ToolFailure, prepare_for_launch


def portable_program(stage, body):
    bitcode = b"BC\xc0\xde" + body
    bitcode += bytes((-len(bitcode)) % 4)
    header = struct.pack("<II4sIII", stage << 16 | 0x60, (24 + len(bitcode)) // 4,
                         b"DXIL", 0x100, 16, len(bitcode))
    chunk = struct.pack("<4sI", b"DXIL", len(header) + len(bitcode)) + header + bitcode
    return b"DXBC" + bytes(16) + struct.pack("<IIII", 1, 36 + len(chunk), 1, 36) + chunk


@unittest.skipUnless(sys.platform == "linux", "The runtime shader worker belongs to Proton on Linux")
class RuntimeShaderLinks(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.session = directory / "run"
        self.requests = self.session / "requests"
        self.requests.mkdir(parents=True)
        self.cache = directory / "cache"
        self.cache.mkdir()
        self.tools = ("xshader", "dxc", "dxv")

    def test_unoptimised_cap_grows_with_generated_program_size(self):
        hlsl = self.cache / "program.hlsl"
        for size, expected in [(0, O0_TIMEOUT), (1 << 20, O0_TIMEOUT), (5 << 19, 225), (64 << 20, O0_TIMEOUT_MAX)]:
            hlsl.write_bytes(b" " * size)
            self.assertEqual(o0_timeout(hlsl), expected, size)

    def test_invalid_persistent_dxil_falls_back_to_cli_before_publication(self):
        data = b"synthetic compute shader"
        key = hashlib.sha256(data).hexdigest()
        source = self.requests / (key + ".dxbc")
        source.write_bytes(data)
        expected = portable_program(5, b"cli")
        messages = []
        calls = []

        def persistent_compile(dxc, hlsl, output):
            output.write_bytes(portable_program(0, b"wrong stage"))
            return True

        def tool(args, *, timeout):
            calls.append(args[0])
            if args[0] == "xshader":
                args[args.index("--hlsl-out") + 1].write_text("void main() {}")
                args[args.index("--profile-out") + 1].write_text("cs_6_0")
            elif args[0] == "dxc":
                output = args[args.index("-Fo") + 1]
                self.assertFalse(output.exists())
                output.write_bytes(expected)

        persistent = SimpleNamespace(compile=persistent_compile, report=messages.append)
        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=tool):
            self.assertTrue(compile_request(source, key, self.tools, self.cache, self.session, persistent=persistent))
        self.assertEqual(calls, ["xshader", "dxv", "dxc", "dxv"])
        self.assertEqual((self.session / (key + ".cso")).read_bytes(), expected)
        self.assertIn("using the CLI", messages[0])

    def request(self, pixel_order, pixel=b"Xbox pixel shader"):
        inputs = {"producer.dxbc": b"Xbox geometry shader", "pixel.dxbc": pixel, "pixel.cso": pixel_order}
        key = "_".join(hashlib.sha256(data).hexdigest() for data in inputs.values())
        source = self.requests / (key + ".link")
        source.mkdir()
        for name, data in inputs.items():
            (source / name).write_bytes(data)
        return key, source

    def test_exact_consumer_order_selects_variant_and_warm_hit_rechecks_inputs(self):
        def run(args, *, timeout):
            if args[0] == "xshader":
                self.assertEqual(args[1].read_bytes(), b"Xbox geometry shader")
                self.assertEqual(args[args.index("--link-pixel") + 1].read_bytes(), b"Xbox pixel shader")
                order = args[args.index("--pixel-order") + 1].read_bytes()
                args[args.index("--hlsl-out") + 1].write_bytes(order[-4:])
                args[args.index("--profile-out") + 1].write_text("gs_6_0")
            elif args[0] == "dxc":
                self.assertEqual(args[args.index("-T") + 1], "gs_6_0")
                args[args.index("-Fo") + 1].write_bytes(portable_program(2, args[-1].read_bytes()))
            else:
                self.assertEqual(args[0], "dxv")

        for signature in [b"r0.z", b"r2.x"]:
            key, source = self.request(portable_program(0, signature))
            dest = self.session / (key + ".cso")
            with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as pipeline:
                self.assertTrue(compile_request(source, key, self.tools, self.cache, self.session))
                self.assertEqual(dest.read_bytes(), portable_program(2, signature))
                self.assertEqual(pipeline.call_count, 4)
                dest.unlink()
                self.assertFalse(compile_request(source, key, self.tools, self.cache, self.session))
                self.assertEqual(dest.read_bytes(), portable_program(2, signature))
                for name in ["producer.dxbc", "pixel.dxbc", "pixel.cso"]:
                    path = source / name
                    old = path.read_bytes()
                    path.write_bytes(old + b"changed")
                    with self.assertRaisesRegex(ValueError, "digest"):
                        compile_request(source, key, self.tools, self.cache, self.session)
                    path.write_bytes(old)
                self.assertEqual(pipeline.call_count, 4)

    def test_compile_request_accepts_profile_with_trailing_whitespace(self):
        result = portable_program(2, b"main")
        key, source = self.request(portable_program(0, b"r0.z"))

        def run(args, *, timeout):
            if args[0] == "xshader":
                args[args.index("--hlsl-out") + 1].write_bytes(b"void main() {}")
                args[args.index("--profile-out") + 1].write_text("gs_6_0\r\n")
            elif args[0] == "dxc":
                self.assertEqual(args[args.index("-T") + 1], "gs_6_0")
                args[args.index("-Fo") + 1].write_bytes(result)
            else:
                self.assertEqual(args[0], "dxv")

        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run):
            self.assertTrue(compile_request(source, key, self.tools, self.cache, self.session))
            self.assertEqual((self.session / (key + ".cso")).read_bytes(), result)

    def test_wrong_stage_and_malformed_warm_entries_are_rebuilt(self):
        from project_tuxbox.runtime_shaders import cache_snapshot
        key, source = self.request(portable_program(0, b"order"))
        dest = self.cache / (key + ".cso")
        stamp = dest.with_suffix(".sha256")
        valid = portable_program(2, b"good")
        def run(args, *, timeout):
            if args[0] == "xshader":
                args[args.index("--hlsl-out") + 1].write_text("shader")
                args[args.index("--profile-out") + 1].write_text("gs_6_0")
            elif args[0] == "dxc":
                args[args.index("-Fo") + 1].write_bytes(valid)
        for wrong in (portable_program(0,b"wrong"), valid+b"junk", b"bad"):
            dest.write_bytes(wrong)
            stamp.write_text(hashlib.sha256(wrong).hexdigest())
            self.assertIsNone(cache_snapshot(dest, stamp, {1,2}))
            with patch("project_tuxbox.runtime_shaders.run_tool",side_effect=run) as compiler:
                self.assertTrue(compile_request(source,key,self.tools,self.cache,self.session))
                self.assertEqual(compiler.call_count,4)
                self.assertEqual(dest.read_bytes(),valid)
                self.assertFalse(compile_request(source,key,self.tools,self.cache,None))
                self.assertEqual(compiler.call_count,4)

    def test_cold_wrong_stage_never_published(self):
        key, source = self.request(portable_program(0,b"order"))
        def run(args, *, timeout):
            if args[0] == "xshader":
                args[args.index("--hlsl-out") + 1].write_text("shader")
                args[args.index("--profile-out") + 1].write_text("gs_6_0")
            elif args[0] == "dxc":
                args[args.index("-Fo") + 1].write_bytes(portable_program(0,b"wrong"))
        with patch("project_tuxbox.runtime_shaders.run_tool",side_effect=run):
            with self.assertRaisesRegex(ValueError,"different shader stage"):
                compile_request(source,key,self.tools,self.cache,self.session)
        for directory in (self.cache,self.session):
            for suffix in (".cso",".sha256",".unsupported"):
                self.assertFalse((directory/(key+suffix)).exists())

    def test_known_source_stage_and_bounded_cache_files(self):
        from project_tuxbox.runtime_shaders import cache_snapshot
        data=portable_program(5,b"source")
        key=hashlib.sha256(data).hexdigest()
        source=self.requests/(key+".dxbc");source.write_bytes(data)
        wrong=portable_program(0,b"wrong")
        dest=self.cache/(key+".cso");dest.write_bytes(wrong)
        stamp=dest.with_suffix(".sha256");stamp.write_text(hashlib.sha256(wrong).hexdigest())
        def run(args, *, timeout):
            if args[0]=="xshader":
                args[args.index("--profile-out")+1].write_text("ps_6_0")
        with patch("project_tuxbox.runtime_shaders.run_tool",side_effect=run):
            with self.assertRaisesRegex(ValueError,"profile differs"):
                compile_request(source,key,self.tools,self.cache,self.session)
        dest.unlink();os.mkfifo(dest)
        self.assertIsNone(cache_snapshot(dest,stamp,{5}))
        dest.unlink();dest.symlink_to(source)
        self.assertIsNone(cache_snapshot(dest,stamp,{5}))
        dest.unlink();dest.touch()
        with dest.open("r+b") as f:f.truncate((64<<20)+1)
        self.assertIsNone(cache_snapshot(dest,stamp,{5}))

    def test_duplicate_cold_requests_share_compile_across_workers(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, Lock
        from project_tuxbox.runtime_shaders import compile_lock
        key, source = self.request(portable_program(0,b"order"))
        second = self.session.parent / "second"
        second.mkdir()
        entered, release = Event(), Event()
        calls = []
        guard = Lock()
        def run(args, *, timeout):
            with guard:
                calls.append(args[0])
            if args[0] == "xshader":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test gate")
                args[args.index("--hlsl-out")+1].write_text("shader")
                args[args.index("--profile-out")+1].write_text("gs_6_0")
            elif args[0]=="dxc":
                args[args.index("-Fo")+1].write_bytes(portable_program(2,b"good"))
        with patch("project_tuxbox.runtime_shaders.run_tool",side_effect=run):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(compile_request,source,key,self.tools,self.cache,self.session)
                try:
                    self.assertTrue(entered.wait(5))
                    other=pool.submit(compile_request,source,key,self.tools,self.cache,second)
                    # Exact key isolation: unrelated keys remain available.
                    with compile_lock(self.cache,"0"*64,timeout=.1):
                        pass
                    with self.assertRaises(TimeoutError):
                        with compile_lock(self.cache,key,timeout=.05):
                            self.fail("same key unexpectedly acquired")
                finally:
                    release.set()
                self.assertTrue(first.result(timeout=5))
                self.assertFalse(other.result(timeout=5))
        self.assertEqual(calls,["dxv","xshader","dxc","dxv"])
        self.assertEqual((self.session/(key+".cso")).read_bytes(),(second/(key+".cso")).read_bytes())

    def test_lock_releases_after_retryable_failure(self):
        from project_tuxbox.runtime_shaders import compile_lock, exact_marker
        key, source = self.request(portable_program(0,b"order"))
        with self.assertRaisesRegex(ValueError,"test failure"):
            with compile_lock(self.cache,key):
                raise ValueError("test failure")
        with compile_lock(self.cache,key,timeout=.1):
            pass
        marker=self.cache/"marker"
        os.mkfifo(marker)
        self.assertFalse(exact_marker(marker,b"abc"))
        marker.unlink()
        marker.symlink_to(source/"pixel.cso")
        self.assertFalse(exact_marker(marker,b"abc"))

    def test_rejects_native_code_non_pixel_dxil_and_non_regular_input(self):
        for program in [b"Xbox-native shader", portable_program(1, b"r0.z")]:
            key, source = self.request(program)
            with self.assertRaisesRegex(ValueError, "portable pixel"):
                compile_request(source, key, self.tools, self.cache, self.session)
        key, source = self.request(portable_program(0, b"r0.z"))
        path = source / "pixel.cso"
        with path.open("r+b") as stream:
            stream.truncate((64 << 20) + 1)
        with self.assertRaisesRegex(ValueError, "64 MiB"):
            compile_request(source, key, self.tools, self.cache, self.session)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, "regular"):
            compile_request(source, key, self.tools, self.cache, self.session)
        path.unlink()
        path.symlink_to(source / "producer.dxbc")
        with self.assertRaises(OSError):
            compile_request(source, key, self.tools, self.cache, self.session)

    def test_persistent_helper_has_separate_request_and_hlsl_caches(self):
        release, dxc, snapshot, helper = (
            self.cache / name for name in ("release", "dxc", "snapshot", "helper")
        )
        release.write_bytes(b"xshader")
        dxc.write_text("#!/bin/sh\necho version\n")
        dxc.chmod(0o700)
        helper.write_bytes(b"helper A")
        with patch("project_tuxbox.runtime_shaders.compiler_tools", return_value=(release, dxc, dxc)):
            _, cli = snapshot_compiler_cache(snapshot, self.cache)
            _, experimental = snapshot_compiler_cache(snapshot, self.cache, dxc_helper=helper)
            helper.write_bytes(b"helper B")
            _, replacement = snapshot_compiler_cache(snapshot, self.cache, dxc_helper=helper)
        self.assertEqual(len({cli, experimental, replacement}), 3)
        self.assertEqual(len({(cache / "dxil-identity").read_bytes()
                              for cache in (cli, experimental, replacement)}), 3)

    def test_refusal_requires_exact_inputs_and_compiler_build(self):
        release, dxc, snapshot = (self.cache / name for name in ("release", "dxc", "snapshot"))
        dxc.write_text("#!/bin/sh\necho version\n")
        dxc.chmod(0o700)
        release.write_text("#!/bin/sh\nexit 65\n")
        key, source = self.request(portable_program(0, b"r0.z"))
        with patch("project_tuxbox.runtime_shaders.compiler_tools", return_value=(release, dxc, dxc)):
            tools, cache = snapshot_compiler_cache(snapshot, self.cache)
            for cached in (False, True):
                with self.assertRaises(ShaderRefused) as result:
                    compile_request(source, key, tools, cache, self.session)
                self.assertEqual(result.exception.cached, cached)
            path = source / "pixel.dxbc"
            path.write_bytes(b"changed consumer")
            with self.assertRaisesRegex(ValueError, "digest"):
                compile_request(source, key, tools, cache, self.session)
            path.write_bytes(b"Xbox pixel shader")
            variant, changed = self.request(portable_program(0, b"r0.z"), pixel=b"pixel with a different root")
            with self.assertRaises(ShaderRefused) as result:
                compile_request(changed, variant, tools, cache, self.session)
            self.assertFalse(result.exception.cached)
            release.write_text("#!/bin/sh\nexit 1\n")
            tools, rebuilt = snapshot_compiler_cache(snapshot, self.cache)
            self.assertNotEqual(cache, rebuilt)
            for _ in range(2):
                with self.assertRaises(ToolFailure):
                    compile_request(source, key, tools, rebuilt, self.session)
            self.assertFalse((rebuilt / (key + ".unsupported")).exists())

    def test_other_tool_failures_do_not_poison_shader_cache(self):
        key, source = self.request(portable_program(0, b"r0.z"))
        for tool, error in (("xshader", FileNotFoundError()), ("xshader", subprocess.TimeoutExpired("xshader", 30)),
                            ("xshader", ToolFailure("xshader", -11, "crash")), ("dxv", ToolFailure("dxv", 65, "bad DXIL"))):
            def run(args, *, timeout):
                if args[0] == tool:
                    raise error
            with self.subTest(error=error), patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run):
                with self.assertRaises(type(error)):
                    compile_request(source, key, self.tools, self.cache, self.session)
                self.assertFalse((self.cache / (key + ".unsupported")).exists())

    def test_dxc_retries_a_timeout_without_optimization(self):
        program = b"Xbox compute shader"
        key = hashlib.sha256(program).hexdigest()
        source = self.requests / (key + ".dxbc")
        source.write_bytes(program)
        dxil = portable_program(5, b"compute")

        def run(args, *, timeout):
            if args[0] == "xshader":
                args[args.index("--hlsl-out") + 1].write_text("compute shader")
                args[args.index("--profile-out") + 1].write_text("cs_6_0")
            elif args[0] == "dxc":
                output = args[args.index("-Fo") + 1]
                if "-O0" not in args:
                    output.write_bytes(b"partial")
                    raise subprocess.TimeoutExpired("dxc", timeout)
                self.assertFalse(output.exists())
                output.write_bytes(dxil)
            else:
                self.assertEqual(args[0], "dxv")
                self.assertEqual(args[1].read_bytes(), dxil)

        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as compiler:
            self.assertTrue(compile_request(source, key, self.tools, self.cache, self.session))
        self.assertEqual([call.args[0][0] for call in compiler.call_args_list], ["xshader", "dxc", "dxc", "dxv"])
        dxc_calls = [call for call in compiler.call_args_list if call.args[0][0] == "dxc"]
        self.assertEqual([call.kwargs["timeout"] for call in dxc_calls], [15, O0_TIMEOUT])
        self.assertNotIn("-O0", dxc_calls[0].args[0])
        self.assertIn("-O0", dxc_calls[1].args[0])
        self.assertEqual((self.session / (key + ".cso")).read_bytes(), dxil)

    def test_large_pixel_candidate_precedes_slow_marker_and_validates_once(self):
        """Cold large ps_6_6 requests validate the candidate before publication."""
        program = b"Xbox pixel shader"
        key = hashlib.sha256(program).hexdigest()
        source = self.requests / (key + ".dxbc")
        source.write_bytes(program)
        dxil = portable_program(0, b"pixel")
        store = Mock()
        store.key.return_value = "c" * 64
        store.root = self.cache / "content"
        store.root.mkdir()
        store.fetch.return_value = False
        store.is_slow.return_value = True

        def run(args, *, timeout):
            if args[0] == "xshader":
                args[args.index("--hlsl-out") + 1].write_bytes(b"x" * (256 << 10))
                args[args.index("--profile-out") + 1].write_text("ps_6_6")
            elif args[0] == "dxc":
                self.assertIn("-disable-lifetime-markers", args)
                self.assertNotIn("-O0", args)
                self.assertEqual(timeout, 15)
                args[args.index("-Fo") + 1].write_bytes(dxil)
            else:
                self.assertEqual(args[0], "dxv")
                self.assertFalse((self.cache / (key + ".cso")).exists())
                self.assertEqual(args[1].read_bytes(), dxil)

        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as compiler, \
                patch("project_tuxbox.runtime_shaders.DxilStore.for_cache", return_value=store):
            self.assertTrue(compile_request(source, key, self.tools, self.cache, self.session))
            self.assertFalse(compile_request(source, key, self.tools, self.cache, self.session))
        self.assertEqual([call.args[0][0] for call in compiler.call_args_list], ["xshader", "dxc", "dxv"])
        store.is_slow.assert_not_called()
        store.mark_slow.assert_not_called()
        store.save.assert_called_once()
        self.assertEqual((self.session / (key + ".cso")).read_bytes(), dxil)

    def test_large_pixel_candidate_failures_clean_output_and_preserve_baseline_policy(self):
        """Rejected candidates retry the baseline with its original slow-marker policy."""
        hlsl, output = self.cache / "shader.hlsl", self.cache / "out.cso"
        hlsl.write_bytes(b"x" * (256 << 10))
        dxil = portable_program(0, b"baseline")
        for failure in ("flag", "timeout", "dxv", "stage", "container", "missing"):
            for slow in (False, True):
                with self.subTest(failure=failure, slow=slow):
                    output.unlink(missing_ok=True)
                    store = Mock()
                    store.is_slow.return_value = slow
                    candidate = False
                    def run(args, *, timeout):
                        nonlocal candidate
                        if args[0] == "dxc":
                            candidate = "-disable-lifetime-markers" in args
                            if candidate:
                                self.assertEqual(timeout, 15)
                                store.is_slow.assert_not_called()
                                if failure != "missing":
                                    output.write_bytes(portable_program(1, b"wrong") if failure == "stage" else b"partial")
                                if failure == "flag":
                                    raise ToolFailure("dxc", 1, "unknown argument")
                                if failure == "timeout":
                                    raise subprocess.TimeoutExpired("dxc", timeout)
                            else:
                                self.assertFalse(output.exists())
                                self.assertEqual("-O0" in args, slow)
                                self.assertEqual(timeout, O0_TIMEOUT if slow else 15)
                                output.write_bytes(dxil)
                        elif candidate and failure == "dxv":
                            raise ToolFailure("dxv", 1, "invalid DXIL")
                    with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run):
                        self.assertEqual(compile_dxil("dxc", "dxv", "ps_6_6", hlsl, output, store, "request", 0), dxil)
                    store.is_slow.assert_called_once_with("request", "ps_6_6")
                    store.mark_slow.assert_not_called()

    def test_large_pixel_baseline_timeout_still_marks_slow(self):
        """A baseline timeout alone persists the slow marker and retries at O0."""
        hlsl, output = self.cache / "shader.hlsl", self.cache / "out.cso"
        hlsl.write_bytes(b"x" * (256 << 10))
        store = Mock()
        store.is_slow.return_value = False
        dxil = portable_program(0, b"fallback")
        def run(args, *, timeout):
            if args[0] == "dxc":
                self.assertFalse(output.exists())
                if "-O0" not in args:
                    store.mark_slow.assert_not_called()
                    output.write_bytes(b"partial")
                    raise subprocess.TimeoutExpired("dxc", timeout)
                self.assertEqual(timeout, O0_TIMEOUT)
                output.write_bytes(dxil)
        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as compiler:
            self.assertEqual(compile_dxil("dxc", "dxv", "ps_6_6", hlsl, output, store, "request", 0), dxil)
        store.mark_slow.assert_called_once_with("request", "ps_6_6")
        self.assertEqual([call.args[0][0] for call in compiler.call_args_list], ["dxc", "dxc", "dxc", "dxv"])

    def test_large_pixel_startup_errors_propagate(self):
        """Tool startup errors retain their existing retryable exception behavior."""
        hlsl, output = self.cache / "shader.hlsl", self.cache / "out.cso"
        hlsl.write_bytes(b"x" * (256 << 10))
        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=FileNotFoundError("dxc")) as compiler:
            with self.assertRaises(FileNotFoundError):
                compile_dxil("dxc", "dxv", "ps_6_6", hlsl, output, None, "request", 0)
        self.assertEqual(compiler.call_count, 1)

    def test_large_pixel_policy_leaves_other_profiles_and_small_sources_unchanged(self):
        """Of these, only compute programs with a wave size turn lifetime markers off."""
        hlsl, output = self.cache / "shader.hlsl", self.cache / "out.cso"
        for target, stage, size in (("ps_6_6", 0, (256 << 10) - 1), ("ps_6_0", 0, 256 << 10),
                                    ("cs_6_6", 5, 256 << 10), ("gs_6_6", 2, 256 << 10)):
            with self.subTest(target=target, size=size):
                hlsl.write_bytes(b"x" * size)
                dxil = portable_program(stage, b"baseline")
                def run(args, *, timeout):
                    if args[0] == "dxc":
                        self.assertEqual("-disable-lifetime-markers" in args, target == "cs_6_6")
                        self.assertNotIn("-O0", args)
                        output.write_bytes(dxil)
                with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as compiler:
                    self.assertEqual(compile_dxil("dxc", "dxv", target, hlsl, output, None, "request", stage), dxil)
                self.assertEqual([call.args[0][0] for call in compiler.call_args_list], ["dxc", "dxv"])

    def test_dispatched_programs_use_the_candidate_at_any_size(self):
        """A block dispatcher takes the candidate flag, and -O0 when that fails."""
        hlsl, output = self.cache / "shader.hlsl", self.cache / "out.cso"
        hlsl.write_bytes(b"uint tb_pc = 0u; [loop] while (tb_pc != 7u) {}")
        for target, stage in (("ps_6_6", 0), ("ps_6_0", 0), ("vs_6_6", 1), ("cs_6_0", 5)):
            for failure in (None, "flag", "timeout"):
                with self.subTest(target=target, failure=failure):
                    output.unlink(missing_ok=True)
                    store = Mock()
                    dxil = portable_program(stage, b"dispatched")
                    def run(args, *, timeout):
                        if args[0] != "dxc":
                            return
                        candidate = "-disable-lifetime-markers" in args
                        self.assertNotEqual(candidate, "-O0" in args)
                        self.assertEqual(timeout, 15 if candidate else O0_TIMEOUT)
                        output.write_bytes(dxil)
                        if candidate and failure == "flag":
                            raise ToolFailure("dxc", 1, "unknown argument")
                        if candidate and failure == "timeout":
                            raise subprocess.TimeoutExpired("dxc", timeout)
                    with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as compiler:
                        self.assertEqual(compile_dxil("dxc", "dxv", target, hlsl, output, store, "request", stage), dxil)
                    tools = ["dxc", "dxv"] if failure is None else ["dxc", "dxc", "dxv"]
                    self.assertEqual([call.args[0][0] for call in compiler.call_args_list], tools)
                    store.is_slow.assert_not_called()
                    store.mark_slow.assert_not_called()

    def test_cs_6_6_optimises_without_lifetime_markers(self):
        program = b"Xbox compute shader"
        key = hashlib.sha256(program).hexdigest()
        source = self.requests / (key + ".dxbc")
        source.write_bytes(program)
        dxil = portable_program(5, b"compute")

        def run(args, *, timeout):
            if args[0] == "xshader":
                args[args.index("--hlsl-out") + 1].write_text("compute shader")
                args[args.index("--profile-out") + 1].write_text("cs_6_6")
            elif args[0] == "dxc":
                self.assertIn("-O1", args)
                self.assertIn("-disable-lifetime-markers", args)
                self.assertEqual(timeout, COMPUTE_TIMEOUT)
                args[args.index("-Fo") + 1].write_bytes(dxil)
            else:
                self.assertEqual(args[0], "dxv")

        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as compiler:
            self.assertTrue(compile_request(source, key, self.tools, self.cache, self.session))
        self.assertEqual([call.args[0][0] for call in compiler.call_args_list], ["xshader", "dxc", "dxv"])

    def test_wave32_request_has_its_own_translation_and_cache_entry(self):
        """The narrow-wave variant never replaces the ordinary translation."""
        program = b"Xbox wave64 compute shader"
        source_hash = hashlib.sha256(program).hexdigest()
        flags = []

        def run(args, *, timeout):
            if args[0] == "xshader":
                flags.append("--wave32-host" in args)
                self.assertEqual(args[1].read_bytes(), program)
                args[args.index("--hlsl-out") + 1].write_text("narrow" if flags[-1] else "ordinary")
                args[args.index("--profile-out") + 1].write_text("cs_6_6")
            elif args[0] == "dxc":
                args[args.index("-Fo") + 1].write_bytes(portable_program(5, args[-1].read_bytes()))

        for key, suffix in ((source_hash, ".dxbc"), (source_hash + "_w32", ".wave32")):
            source = self.requests / (key + suffix)
            source.write_bytes(program)
            with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run):
                self.assertTrue(compile_request(source, key, self.tools, self.cache, self.session))
        self.assertEqual(flags, [False, True])
        self.assertEqual((self.cache / (source_hash + ".cso")).read_bytes(), portable_program(5, b"ordinary"))
        self.assertEqual((self.session / (source_hash + "_w32.cso")).read_bytes(), portable_program(5, b"narrow"))
        changed = self.requests / (source_hash + "_w32.wave32")
        changed.write_bytes(program + b"changed")
        with self.assertRaisesRegex(ValueError, "digest"):
            compile_request(changed, source_hash + "_w32", self.tools, self.cache, self.session)

    def test_cs_6_6_falls_back_to_no_optimisation(self):
        hlsl, output = self.cache / "shader.hlsl", self.cache / "out.cso"
        hlsl.write_bytes(b"compute shader")
        dxil = portable_program(5, b"fallback")
        for failure in ("timeout", "dxc", "dxv", "missing", "slow"):
            with self.subTest(failure=failure):
                output.unlink(missing_ok=True)
                store = Mock()
                store.is_slow.return_value = failure == "slow"
                optimised = False

                def run(args, *, timeout):
                    nonlocal optimised
                    if args[0] == "dxc":
                        optimised = "-O0" not in args
                        self.assertEqual(timeout, COMPUTE_TIMEOUT if optimised else O0_TIMEOUT)
                        if not optimised:
                            self.assertFalse(output.exists())
                            output.write_bytes(dxil)
                            return
                        if failure != "missing":
                            output.write_bytes(b"partial")
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired("dxc", timeout)
                        if failure == "dxc":
                            raise ToolFailure("dxc", 1, "internal error")
                    elif optimised and failure == "dxv":
                        raise ToolFailure("dxv", 1, "invalid DXIL")

                with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as compiler:
                    self.assertEqual(compile_dxil("dxc", "dxv", "cs_6_6", hlsl, output, store, "request", 5), dxil)
                compiles = [call for call in compiler.call_args_list if call.args[0][0] == "dxc"]
                self.assertEqual(len(compiles), 1 if failure == "slow" else 2)
                self.assertEqual(store.mark_slow.called, failure == "timeout")

    def test_scarlett_replays_exact_recorded_requests_before_launch(self):
        directory = self.session.parent
        title = directory / "game_scarlett_master.exe"
        title.write_bytes(b"one title build")
        title.with_name("shaders.rpf").touch()
        title.with_name("d3d12_xs.dll").touch()
        host = SimpleNamespace(backend="proton", env={"WINEPREFIX": str(directory), "TUXBOX_SHADER_PREP_UI": "0"})
        archive = scarlett_requests(title, directory / SOURCE_CACHE.parent)
        archive.mkdir(parents=True)
        key, source = self.request(portable_program(0, b"r0.z"))
        unsupported = self.requests / (hashlib.sha256(b"unsupported Xbox shader").hexdigest() + ".dxbc")
        unsupported.write_bytes(b"unsupported Xbox shader")

        def run(args, *, timeout):
            if args[0] == "xshader":
                program = args[1].read_bytes()
                if program == b"unsupported Xbox shader":
                    raise ToolFailure("xshader", 65, "unsupported")
                self.assertEqual(program, b"Xbox geometry shader")
                self.assertEqual(args[args.index("--pixel-order") + 1].read_bytes(), portable_program(0, b"r0.z"))
                args[args.index("--hlsl-out") + 1].touch()
                args[args.index("--profile-out") + 1].write_text("gs_6_0")
            if args[0] == "dxc":
                args[args.index("-Fo") + 1].write_bytes(portable_program(2, b"gs"))

        old_cache = self.cache / "old"
        old_cache.mkdir()

        def remember(request, inputs):
            archive_request(archive, request, inputs)

        with (
            patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as compiler,
            patch("project_tuxbox.scarlett_prep.snapshot_compiler_cache", return_value=(self.tools, self.cache)),
            patch("project_tuxbox.scarlett_prep.report_progress") as prep_report,
        ):
            self.assertTrue(compile_request(source, key, self.tools, old_cache, self.session, remember=remember))
            with self.assertRaises(ShaderRefused):
                compile_request(unsupported, unsupported.stem, self.tools, old_cache, self.session, remember=remember)
            compiler.reset_mock()

            self.assertTrue(prepare_for_launch(title, host))
            self.assertEqual(compiler.call_count, 5)
            self.assertEqual((self.cache / f"{key}.cso").read_bytes(), portable_program(2, b"gs"))
            self.assertTrue((self.cache / f"{unsupported.stem}.unsupported").is_file())
            self.assertTrue(prepare_for_launch(title, host))
            self.assertEqual(compiler.call_count, 5)
            import json
            prefix = "ProjectScarlett preparation aggregate: "
            reports = [json.loads(call.args[1][len(prefix):])
                       for call in prep_report.call_args_list if call.args[1].startswith(prefix)]
            self.assertEqual(len(reports), 2)
            self.assertEqual(reports[0]["totals"], {"compiled": 1, "cached": 0, "unsupported": 1, "failed": 0})
            self.assertEqual(reports[0]["refusal_buckets"], {"other_refusal": 1})
            self.assertEqual(reports[1]["totals"], {"compiled": 0, "cached": 1, "unsupported": 1, "failed": 0})
            self.assertEqual(reports[1]["refusal_buckets"], {"other_refusal": 1})
            self.assertEqual(reports[1]["request_types"], {".dxbc": 1, ".link": 1})
            self.assertNotIn(str(directory), json.dumps(reports))
            title.write_bytes(b"a different title build")
            self.assertTrue(prepare_for_launch(title, host))
            self.assertEqual(compiler.call_count, 5)

    def test_worker_removes_failed_link_and_leaves_unpublished_directories_alone(self):
        from threading import Event
        key, completed = self.request(portable_program(0, b"r0.z"))
        pending = self.requests / (key + ".tmp")
        pending.mkdir()
        stop = Event()
        with patch("project_tuxbox.runtime_shaders.compiler_tools", side_effect=ValueError("compiler unavailable")):
            serve(self.session, stop, lambda line: stop.set() if 'compiler unavailable' in line else None)
        self.assertTrue((self.session / (key + ".failed")).is_file())
        self.assertFalse(completed.exists())
        self.assertTrue(pending.is_dir())

    def test_running_worker_keeps_compiler_after_release_replacement(self):
        from threading import Event
        release = self.cache / "xshader"
        replacement = self.cache / "new"
        dxc = self.cache / "dxc"
        for binary, text in [(release, "original"), (replacement, "rebuilt"), (dxc, "toolchain")]:
            binary.write_text(f"#!/bin/sh\necho {text}\n")
            binary.chmod(0o700)
        stop = Event()
        calls = []

        def request(data):
            key = hashlib.sha256(data).hexdigest()
            (self.requests / (key + ".dxbc")).write_bytes(data)

        def compile_one(source, key, tools, cache, session):
            calls.append((subprocess.check_output([tools[0]]).strip(), cache.name))
            if len(calls) == 1:
                replacement.replace(release)
                request(b"second")
            else:
                stop.set()
            return True

        with patch("project_tuxbox.runtime_shaders.compiler_tools", return_value=(release, dxc, dxc)), \
                patch("project_tuxbox.runtime_shaders.compile_request", side_effect=compile_one):
            request(b"first")
            serve(self.session, stop, lambda line: None)
            stop.clear()
            request(b"third")
            serve(self.session, stop, lambda line: None)
        self.assertEqual([entry[0] for entry in calls], [b"original", b"original", b"rebuilt"])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertNotEqual(calls[1][1], calls[2][1])
