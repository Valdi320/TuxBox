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

from examples.lanes import raster_fixture
from project_tuxbox.shader_prep import SOURCE_CACHE
from project_tuxbox import verify as verify_module
from project_tuxbox.verify import BASE_SUITES, LANES_SCENARIOS, lanes_raster_tables


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


class RasterFixtureTests(unittest.TestCase):
    def test_programs_are_rootless_and_name_their_descriptor(self):
        for data, stage in ((raster_fixture.vertex(), 1), (raster_fixture.pixel(), 0)):
            self.assertEqual(struct.unpack_from("<I", data, 24)[0], len(data))
            found = parts(data)
            self.assertNotIn(b"RTS0", found)
            self.assertEqual(struct.unpack_from("<I", found[b"DXIL"], 0)[0] >> 16, stage)
            self.assertEqual(found[b"PSV0"][28], stage)
        pixel = parts(raster_fixture.pixel())
        program = fssm(pixel[b"DXIL"])
        self.assertEqual(struct.unpack_from("<I", program, 8)[0], len(program) - 16)
        # The FSSM 1.1 user-data and used-resource tables the runtime reads.
        self.assertEqual(rows(program, 0xe8, 0xf8), [[3, 16, 1, 3, 0x109]])
        self.assertEqual(rows(program, 0xf0, 0xfc), [[0x300, 16, 3, 1, 0x1_0904]])
        psv = pixel[b"PSV0"]
        table = psv[4 + struct.unpack_from("<I", psv, 0)[0]:]
        self.assertEqual(struct.unpack_from("<6I", table), (1, 16, 4, 2, 3, 3))
        self.assertEqual(rows(fssm(parts(raster_fixture.vertex())[b"DXIL"]), 0xf0, 0xfc), [])

    def test_header_carries_both_programs_and_the_expected_value(self):
        vertex, pixel = raster_fixture.vertex(), raster_fixture.pixel()
        text = raster_fixture.header(vertex, pixel)
        for name, data in (("raster_vertex", vertex), ("raster_pixel", pixel)):
            values = re.search(rf"{name}\[\] = \{{([0-9,]+)\}};", text).group(1)
            self.assertEqual(bytes(int(value) for value in values.split(",")), data)
        self.assertIn("raster_expected = 0x7ab1e001, raster_decoy = 0xd0000000;", text)
        values = re.search(r"raster_sampler_pixel\[\] = \{([0-9,]+)\};", text).group(1)
        self.assertEqual(bytes(int(value) for value in values.split(",")), raster_fixture.sampler_pixel())
        self.assertIn("raster_sampler_texels[] = {1.0f, 3.0f};", text)
        self.assertIn("raster_sampler_linear = 1.5f, raster_sampler_point = 1.0f;", text)
        for name, data in (("mesh_sampler_mesh", raster_fixture.mesh()),
                           ("mesh_sampler_pixel", raster_fixture.mesh_sampler_pixel())):
            values = re.search(rf"{name}\[\] = \{{([0-9,]+)\}};", text).group(1)
            self.assertEqual(bytes(int(value) for value in values.split(",")), data)

    def test_sampler_program_declares_the_roots_register_and_builds_a_literal_state(self):
        data = raster_fixture.sampler_pixel()
        found = parts(data)
        self.assertNotIn(b"RTS0", found)
        self.assertEqual(struct.unpack_from("<I", found[b"DXIL"], 0)[0] >> 16, 0)
        program = fssm(found[b"DXIL"])
        self.assertEqual(rows(program, 0xe8, 0xf8), [[3, 0, 1, 0, 0x109]])
        self.assertEqual(rows(program, 0xf0, 0xfc), [[0x300, 0, 0, 0, 0x1_0908], [0x401, 0, 0, 0, 0]])
        psv = found[b"PSV0"]
        table = psv[4 + struct.unpack_from("<I", psv, 0)[0]:]
        self.assertEqual(struct.unpack_from("<10I", table), (2, 16, 3, 1, 0, 0, 1, 0, 0, 0))
        # Linear filtering between the texel centres; point filtering takes the first texel.
        left, right = raster_fixture.SAMPLER_TEXELS
        self.assertEqual(raster_fixture.SAMPLER_LINEAR, left + (right - left) * (0.375 * 2 - 0.5))
        self.assertEqual(raster_fixture.SAMPLER_POINT, left)


    def test_mesh_programs_name_a_second_sampler_and_a_moved_root_constant(self):
        pixel = parts(raster_fixture.mesh_sampler_pixel())
        program = fssm(pixel[b"DXIL"])
        self.assertEqual(rows(program, 0xf0, 0xfc),
                         [[0x300, 0, 0, 0, 0x1_0908], [0x401, 0, 0, 0, 0], [0x401, 0, 1, 0, 0]])
        psv = pixel[b"PSV0"]
        table = psv[4 + struct.unpack_from("<I", psv, 0)[0]:]
        self.assertEqual(struct.unpack_from("<14I", table), (3, 16, 3, 1, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1))
        # The rows moved the code past the default offset; the program is unchanged.
        self.assertEqual(fssm(parts(raster_fixture.sampler_pixel())[b"DXIL"])[0x290:],
                         program[16 + struct.unpack_from("<Q", program, 16 + 0x68)[0]:])
        mesh = parts(raster_fixture.mesh())
        self.assertNotIn(b"RTS0", mesh)
        self.assertEqual(struct.unpack_from("<I", mesh[b"DXIL"], 0)[0] >> 16, 13)
        self.assertEqual(mesh[b"PSV0"][28], 13)
        self.assertEqual(mesh[b"PSV0"][31], 2)
        self.assertEqual(struct.unpack_from("<HH", mesh[b"PSV0"], 16), (128, 128))
        self.assertEqual(struct.unpack_from("<3I", mesh[b"PSV0"], 40), (128, 1, 1))
        self.assertGreater(len(raster_fixture.MESH_CODE), 1)
        program = fssm(mesh[b"DXIL"])
        self.assertEqual(rows(program, 0xe8, 0xf8), [[0, 0, 1, 501, 0x200]])
        self.assertEqual(rows(program, 0xf0, 0xfc), [[2, 0, 501, 0, 0x2_0004]])


def trace(slots):
    return ('multi-table raster roots after 1 pipelines: {"reflected": 1}\n'
            f"created raster pipeline with {slots} reflected FSSM descriptor slots\n")


class RasterGateTests(unittest.TestCase):
    TRACE = trace(3)

    def run_gate(self, output, compiled, scenario="raster-tables"):
        rootless = [hashlib.sha256(data).hexdigest()
                    for data in (raster_fixture.vertex(), raster_fixture.pixel(), raster_fixture.sampler_pixel(),
                                 raster_fixture.mesh_sampler_pixel())]
        mesh_key = hashlib.sha256(raster_fixture.mesh()).hexdigest()
        with TemporaryDirectory() as prefix:
            sources = Path(prefix) / SOURCE_CACHE
            runtime = sources.parent / "runtime/fingerprint"
            runtime.mkdir(parents=True)
            sources.mkdir()
            stale = [sources / f"{rootless[1]}.cso", runtime / f"{rootless[0]}.unsupported"]
            kept = sources / "title.cso"
            for path in (*stale, kept):
                path.write_bytes(b"")
            env = {"WINEPREFIX": prefix, "TUXBOX_RASTER_ROOT_FALLBACK": "1", "TUXBOX_LOG": "stderr", "TUXBOX_NULL_PIPELINES": "1"}
            seen = {}

            @contextmanager
            def session(cache, session_env, report):
                self.assertEqual(cache, sources.parent)
                seen["env"] = dict(session_env)
                yield
                for key in compiled:
                    (runtime / f"{key}.cso").write_bytes(b"")
                    report(f"Compiled Xbox shader {key}")

            calls = {}
            mesh_cso = sources / f"{mesh_key}.cso"

            def check(name, binary, arguments, match, log_check=None, **_):
                calls[name] = {"arguments": arguments, "stale": [path.exists() for path in stale],
                               "problem": log_check, "mesh": mesh_cso.exists()}
                return {}

            with mock.patch("project_tuxbox.runtime_shaders.shader_session", session), \
                    mock.patch("project_tuxbox.shader_prep.run_tool", side_effect=AssertionError("desktop mesh preparation is forbidden")):
                lanes_raster_tables(SimpleNamespace(env=env), check, ["--adapter", "10de:2c02"], "10de:2c02", ())
            seen["calls"] = list(calls)
            seen.update(calls[f"lanes-{scenario}"])
            seen["left"] = sorted(path.name for path in sources.parent.rglob("*") if path.is_file())
            seen["env_after"] = env
            # The log is read after the worker reported its compiles.
            seen["problem"] = seen["problem"](output)
            return seen, rootless

    def test_reflected_draw_passes_and_cleans_its_entries(self):
        seen, _ = self.run_gate(self.TRACE, ["a" * 64, "b" * 64])
        self.assertEqual(seen["calls"], ["lanes-raster-tables", "lanes-raster-samplers", "lanes-mesh-samplers", "lanes-mesh-sampler-single"])
        self.assertEqual(seen["arguments"][:2], ["--selftest", "raster-tables"])
        self.assertEqual(seen["stale"], [False, False])
        self.assertNotIn("TUXBOX_RASTER_ROOT_FALLBACK", seen["env"])
        self.assertEqual(seen["env"]["TUXBOX_TRACE_RASTER_ROOTS"], "1")
        self.assertEqual(seen["env"]["TUXBOX_NULL_PIPELINES"], "0")
        self.assertEqual(seen["env_after"]["TUXBOX_NULL_PIPELINES"], "1")
        self.assertIsNone(seen["problem"])
        self.assertEqual(seen["left"], ["title.cso"])
        self.assertEqual(seen["env_after"]["TUXBOX_RASTER_ROOT_FALLBACK"], "1")
        self.assertEqual(seen["env_after"]["TUXBOX_LOG"], "stderr")
        self.assertNotIn("TUXBOX_TRACE_RASTER_ROOTS", seen["env_after"])

    def test_trace_escaped_by_debugstr_passes(self):
        # Wine's +debugstr channel prints the logged map with its quotes escaped.
        escaped = self.TRACE.replace('"', '\\"')
        self.assertIsNone(self.run_gate(escaped, [])[0]["problem"])

    def test_guest_root_and_stand_in_fail(self):
        unresolved = 'multi-table raster roots after 1 pipelines: {"root shape not reflected": 1}\n'
        self.assertIn("did not reflect", self.run_gate(unresolved, [])[0]["problem"])
        self.assertIn("stand-in", self.run_gate(self.TRACE + "substituted null graphics pipeline\n", [])[0]["problem"])
        rootless = self.run_gate(self.TRACE, [])[1]
        self.assertIn("without its root", self.run_gate(self.TRACE, rootless)[0]["problem"])

    def test_sampler_draw_runs_after_and_names_its_slots(self):
        seen, _ = self.run_gate(trace(verify_module.RASTER_SAMPLER_SLOTS), [], "raster-samplers")
        self.assertEqual(seen["arguments"][:2], ["--selftest", "raster-samplers"])
        self.assertEqual(seen["stale"], [False, False])
        self.assertIsNone(seen["problem"])
        # The two-table trace names another pipeline.
        self.assertIn("did not create", self.run_gate(self.TRACE, [], "raster-samplers")[0]["problem"])

    def test_mesh_draw_requires_both_runtime_translations(self):
        mesh, pixel = "a"*64, "b"*64
        mesh_trace = ("created Xbox mesh pipeline (680-byte stream, amplification=false)\n"
                      f"created raster pipeline with {verify_module.MESH_SAMPLER_SLOTS} reflected FSSM descriptor slots\n"
                      f"prepared mesh shader loaded, source SHA-256 {mesh}\n"
                      f"prepared pixel shader loaded, source SHA-256 {pixel}\n")
        seen, rootless = self.run_gate(mesh_trace, [mesh, pixel], "mesh-samplers")
        self.assertEqual(seen["arguments"][:2], ["--selftest", "mesh-samplers"])
        self.assertIsNone(seen["problem"])
        self.assertFalse(seen["mesh"])
        self.assertEqual(seen["calls"], ["lanes-raster-tables", "lanes-raster-samplers", "lanes-mesh-samplers", "lanes-mesh-sampler-single"])
        self.assertEqual(seen["left"], ["title.cso"])
        self.assertIn("mesh pipeline", self.run_gate(trace(1), [], "mesh-samplers")[0]["problem"])
        self.assertIn("stand-in", self.run_gate(mesh_trace + "substituted null raster stream pipeline\n", [],
                                                "mesh-samplers")[0]["problem"])
        self.assertIn("without its root", self.run_gate(mesh_trace, [rootless[3]], "mesh-samplers")[0]["problem"])
        self.assertIn("missing worker receipt for loaded mesh", self.run_gate(mesh_trace, [pixel], "mesh-samplers")[0]["problem"])
        self.assertIn("missing worker receipt for loaded pixel", self.run_gate(mesh_trace, [mesh], "mesh-samplers")[0]["problem"])
        seen, _ = self.run_gate(mesh_trace, [mesh, pixel], "mesh-sampler-single")
        self.assertIsNone(seen["problem"])
        self.assertFalse(seen["mesh"])

    def test_scenario_runs_only_under_the_worker(self):
        self.assertNotIn("raster-tables", LANES_SCENARIOS)
        self.assertNotIn("raster-samplers", LANES_SCENARIOS)
        self.assertNotIn("mesh-samplers", LANES_SCENARIOS)
        self.assertIn("lanes-raster-tables", BASE_SUITES)


if __name__ == "__main__":
    unittest.main()
