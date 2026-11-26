from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from examples.lanes import shader_fixture
from project_tuxbox.shader_prep import SOURCE_CACHE
from project_tuxbox.verify import BASE_SUITES, LANES_SCENARIOS, lanes_compute_root


def parts(data):
    count = struct.unpack_from("<I", data, 28)[0]
    found = {}
    for start in struct.unpack_from(f"<{count}I", data, 32):
        size = struct.unpack_from("<I", data, start + 4)[0]
        found[data[start:start + 4]] = data[start + 8:start + 8 + size]
    return found


def fssm(dxil):
    at = dxil.index(b"XBCH")
    compressed = struct.unpack_from("<Q", dxil, at + 8)[0]
    return zlib.decompress(dxil[at + 24:at + 24 + compressed])


def rows(program, offset_field, count_field):
    start = struct.unpack_from("<Q", program, offset_field)[0]
    count = struct.unpack_from("<I", program, count_field)[0]
    return [list(struct.unpack_from("<5I", program, 16 + start + 20 * n)) for n in range(count)]


class ComputeRootFixtureTests(unittest.TestCase):
    def test_programs_are_rootless_and_name_an_undeclared_uav(self):
        for offset in (0x40, 0xA0):
            data = shader_fixture.root_shader(offset)
            self.assertEqual(struct.unpack_from("<I", data, 24)[0], len(data))
            found = parts(data)
            self.assertNotIn(b"RTS0", found)
            self.assertEqual(struct.unpack_from("<I", found[b"DXIL"], 0)[0] >> 16, 5)
            program = fssm(found[b"DXIL"])
            self.assertEqual(struct.unpack_from("<I", program, 8)[0], len(program) - 16)
            # No user-data rows, so the runtime cannot confirm a reflected root.
            self.assertEqual(rows(program, 0xe8, 0xf8), [])
            self.assertEqual(rows(program, 0xf0, 0xfc), [[0x303, offset // 4, 3, 1, 0x1_0504]])
            psv = found[b"PSV0"]
            self.assertEqual(struct.unpack_from("<3I", psv, 4 + 36), (64, 1, 1))
            table = psv[4 + struct.unpack_from("<I", psv, 0)[0]:]
            self.assertEqual(struct.unpack_from("<6I", table), (1, 16, 7, 2, 3, 3))
        self.assertNotEqual(shader_fixture.root_shader(0x40), shader_fixture.root_shader(0xA0))

    def test_header_carries_both_programs_and_the_expected_value(self):
        text = shader_fixture.header()
        for name, offset in (("root_shader", 0x40), ("root_refused_shader", 0xA0)):
            data = shader_fixture.root_shader(offset)
            values = re.search(rf"char {name}\[\] = \{{([0-9,]+)\}};", text).group(1)
            self.assertEqual(bytes(int(value) for value in values.split(",")), data)
            self.assertIn(f'{name}_digest[] = L"{hashlib.sha256(data).hexdigest()}"', text)
        self.assertIn("root_shader_expected = 0x7ab1e002;", text)


class ComputeRootGateTests(unittest.TestCase):
    STAND_IN = "substituted null compute pipeline for an untranslated shader (1 so far)\n"
    REFUSAL = "unmapped descriptor at s5 + 0xa0"

    def run_gate(self, output, compiled, refused):
        rootless = [hashlib.sha256(shader_fixture.root_shader(offset)).hexdigest() for offset in (0x40, 0xA0)]
        with TemporaryDirectory() as prefix:
            sources = Path(prefix) / SOURCE_CACHE
            runtime = sources.parent / "runtime/fingerprint"
            runtime.mkdir(parents=True)
            sources.mkdir()
            stale = [sources / f"{rootless[0]}.cso", runtime / f"{rootless[1]}.cso"]
            kept = sources / "title.cso"
            for path in (*stale, kept):
                path.write_bytes(b"")
            env = {"WINEPREFIX": prefix, "TUXBOX_SHADER_SESSION": "1", "TUXBOX_LOG": "stderr"}
            seen = {}

            @contextmanager
            def session(cache, session_env, report):
                self.assertEqual(cache, sources.parent)
                seen["env"] = dict(session_env)
                yield
                for key in compiled:
                    (runtime / f"{key}.cso").write_bytes(b"")
                    report(f"Compiled Xbox shader {key}")
                for key, reason in refused:
                    (runtime / f"{key}.unsupported").write_bytes(b"")
                    report(f"Xbox shader {key}: xshader failed (65): {reason}")

            def check(name, binary, arguments, match, log_check=None, **_):
                seen["name"], seen["arguments"] = name, arguments
                seen["stale"] = [path.exists() for path in stale]
                seen["problem"] = log_check
                return {}

            with mock.patch("project_tuxbox.runtime_shaders.shader_session", session):
                lanes_compute_root(SimpleNamespace(env=env), check, ["--adapter", "10de:2c02"], "10de:2c02", ())
            seen["left"] = sorted(path.name for path in sources.parent.rglob("*") if path.is_file())
            seen["env_after"] = env
            seen["problem"] = seen["problem"](output)
            return seen, rootless

    def test_root_translations_pass_and_clean_their_entries(self):
        seen, _ = self.run_gate(self.STAND_IN, ["a" * 64], [("b" * 64, self.REFUSAL)])
        self.assertEqual(seen["name"], "lanes-compute-root")
        self.assertEqual(seen["arguments"][:2], ["--selftest", "compute-root"])
        self.assertEqual(seen["stale"], [False, False])
        self.assertNotIn("TUXBOX_SHADER_SESSION", seen["env"])
        self.assertIsNone(seen["problem"])
        self.assertEqual(seen["left"], ["title.cso"])
        self.assertEqual(seen["env_after"]["TUXBOX_SHADER_SESSION"], "1")
        self.assertEqual(seen["env_after"]["TUXBOX_LOG"], "stderr")

    def test_rootless_fallback_fails(self):
        rootless = self.run_gate(self.STAND_IN, [], [])[1]
        refusal = [("b" * 64, self.REFUSAL)]
        self.assertIn("without its root", self.run_gate(self.STAND_IN, ["a" * 64, rootless[1]], refusal)[0]["problem"])
        # A root-less request the worker refused was still asked for.
        self.assertIn("without its root",
                      self.run_gate(self.STAND_IN, ["a" * 64], [*refusal, (rootless[0], "x")])[0]["problem"])

    def test_missing_translation_refusal_or_stand_in_fails(self):
        refusal = [("b" * 64, self.REFUSAL)]
        self.assertIn("no program", self.run_gate(self.STAND_IN, [], refusal)[0]["problem"])
        self.assertIn("did not refuse", self.run_gate(self.STAND_IN, ["a" * 64], [])[0]["problem"])
        self.assertIn("did not give", self.run_gate("", ["a" * 64], refusal)[0]["problem"])
        two = self.STAND_IN + self.STAND_IN.replace("(1 so far)", "(2 so far)")
        self.assertIn("can bind", self.run_gate(two, ["a" * 64], refusal)[0]["problem"])

    def test_scenario_runs_only_under_the_worker(self):
        self.assertNotIn("compute-root", LANES_SCENARIOS)
        self.assertIn("lanes-compute-root", BASE_SUITES)


if __name__ == "__main__":
    unittest.main()
