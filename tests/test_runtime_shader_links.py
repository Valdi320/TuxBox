import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from project_tuxbox.runtime_shaders import ShaderRefused, archive_request, compile_request, serve, snapshot_compiler_cache
from project_tuxbox.scarlett_prep import scarlett_requests
from project_tuxbox.shader_prep import SOURCE_CACHE, ToolFailure, prepare_for_launch


def portable_program(stage, body):
    bitcode = b"BC\xc0\xde" + body
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
        result = portable_program(0, b"main")
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
        self.assertEqual([call.kwargs["timeout"] for call in dxc_calls], [15, 15])
        self.assertNotIn("-O0", dxc_calls[0].args[0])
        self.assertIn("-O0", dxc_calls[1].args[0])
        self.assertEqual((self.session / (key + ".cso")).read_bytes(), dxil)

    def test_cs_6_6_skips_the_optimiser(self):
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
                self.assertIn("-O0", args)
                self.assertEqual(timeout, 15)
                args[args.index("-Fo") + 1].write_bytes(dxil)
            else:
                self.assertEqual(args[0], "dxv")

        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as compiler:
            self.assertTrue(compile_request(source, key, self.tools, self.cache, self.session))
        self.assertEqual([call.args[0][0] for call in compiler.call_args_list], ["xshader", "dxc", "dxv"])

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
            self.assertEqual(reports[1]["refusal_buckets"], {"cached_reason_unavailable": 1})
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
