import hashlib
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.compare_scarlett_shaders import compare, compile_one, main
from project_tuxbox.runtime_shaders import ShaderRefused


@unittest.skipUnless(sys.platform == "linux", "Uses Proton request file verification")
class CompareScarlett(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.requests = self.root / "requests"
        self.requests.mkdir()
        self.tools = [self.root / n for n in ("baseline", "candidate", "dxc", "dxv")]
        for path in self.tools:
            path.write_text(path.name)

    def request(self, data):
        path = self.requests / (hashlib.sha256(data).hexdigest() + ".dxbc")
        path.write_bytes(data)
        return path

    def test_transition_matrix_keeps_failures_separate(self):
        transitions = {
            b"a": ("refused", "compiled"),
            b"b": ("compiled", "refused"),
            b"c": ("compiled", "failed"),
            b"d": ("failed", "compiled"),
            b"e": ("compiled", "compiled"),
            b"f": ("refused", "refused"),
        }
        sources = [self.request(data) for data in transitions]

        def fake(source, tools):
            status = transitions[source.read_bytes()][int(tools[0].parent.name == "candidate")]
            return {"status": status}

        output = self.root / "result.json"
        args = [str(self.requests)]
        for name, path in zip(("baseline", "candidate", "dxc", "dxv", "json"), [*self.tools, output]):
            args += ["--" + name, str(path)]
        with patch("tools.compare_scarlett_shaders.compile_one", side_effect=fake) as invoke:
            self.assertEqual(main(args), 1)
            self.assertEqual(invoke.call_count, 12)
        result = json.loads(output.read_text())
        self.assertEqual(result["before"], {"compiled": 3, "refused": 2, "failed": 1})
        self.assertEqual(result["after"], {"compiled": 3, "refused": 2, "failed": 1})
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(result["regressed"], 1)
        self.assertEqual(result["candidate_failures_after_success"], 1)
        self.assertEqual(result["inconclusive"], 2)
        self.assertEqual(result["requests"], 6)
        self.assertEqual(len(list(self.requests.iterdir())), 6)
        self.assertEqual(result["tool_sha256"]["baseline"], hashlib.sha256(b"baseline").hexdigest())

    def test_expected_status_gate_and_strict_failures(self):
        source = self.request(b"input")
        expected = self.root / "expected.json"
        expected.write_text(json.dumps({source.name: {"status": "compiled"}}))
        args = [str(self.requests)]
        for name, path in zip(("baseline", "candidate", "dxc", "dxv"), self.tools):
            args += ["--" + name, str(path)]
        with patch("tools.compare_scarlett_shaders.compile_one", return_value={"status": "failed"}):
            result = self.root / "mismatch.json"
            self.assertEqual(main(args + ["--json", str(result), "--expect-candidate", str(expected)]), 1)
            self.assertEqual(json.loads(result.read_text())["expectation_mismatches"],
                             [{"request": source.name, "expected": "compiled", "actual": "failed"}])
            self.assertEqual(main(args + ["--json", str(self.root / "strict.json"), "--require-no-failures"]), 1)
            expected.write_text(json.dumps({source.name: {"status": "failed"}}))
            self.assertEqual(main(args + ["--json", str(self.root / "expected-error.json"),
                                          "--expect-candidate", str(expected)]), 0)
        expected.write_text("{}")
        with self.assertRaises(SystemExit), patch("tools.compare_scarlett_shaders.compile_one") as invoke:
            main(args + ["--json", str(self.root / "missing.json"), "--expect-candidate", str(expected)])
        invoke.assert_not_called()

    def test_real_runtime_pipeline_validates_and_does_not_reuse_cache(self):
        source = self.request(b"synthetic input")
        bitcode = b"BC\xc0\xde"
        program = struct.pack("<II4sIII", 0x50060, 7, b"DXIL", 0x100, 16, len(bitcode)) + bitcode
        chunk = struct.pack("<4sI", b"DXIL", len(program)) + program
        result = b"DXBC" + bytes(16) + struct.pack("<IIII", 1, 36 + len(chunk), 1, 36) + chunk

        def run(args, *, timeout):
            if args[0].name == "baseline":
                self.assertEqual(args[1].read_bytes(), b"synthetic input")
                args[args.index("--hlsl-out") + 1].write_text("void main() {}")
                args[args.index("--profile-out") + 1].write_text("cs_6_0")
            elif args[0].name == "dxc":
                args[args.index("-Fo") + 1].write_bytes(result)
            else:
                self.assertEqual(args[0].name, "dxv")
                self.assertEqual(args[1].read_bytes(), result)

        tools = (self.tools[0], *self.tools[2:])
        with patch("project_tuxbox.runtime_shaders.run_tool", side_effect=run) as invoke:
            for _ in range(2):
                actual = compile_one(source, tools)
                self.assertEqual(actual, {"status": "compiled", "dxil_sha256": hashlib.sha256(result).hexdigest()})
            self.assertEqual(invoke.call_count, 6)
            source.write_bytes(b"wrong digest")
            self.assertEqual(compile_one(source, tools)["status"], "failed")
            self.assertEqual(invoke.call_count, 6)
        self.assertEqual(list(self.requests.iterdir()), [source])

    def test_refusal_and_tool_error_are_not_success(self):
        source = self.request(b"input")
        with patch("tools.compare_scarlett_shaders.compile_request", side_effect=ShaderRefused("unsupported", cached=False)):
            self.assertEqual(compile_one(source, self.tools)["status"], "refused")
        with patch("tools.compare_scarlett_shaders.compile_request", side_effect=OSError("tool failed")):
            self.assertEqual(compile_one(source, self.tools)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
