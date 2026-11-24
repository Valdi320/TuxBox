from contextlib import nullcontext, redirect_stderr
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from project_tuxbox.runtime_shaders import ShaderRefused
from project_tuxbox.scarlett_prep import prepare_scarlett


class ScarlettPreparation(unittest.TestCase):
    def test_ranks_new_refusals_and_keeps_cached_separate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "game_scarlett_master.exe"
            exe.touch()
            exe.with_name("d3d12_xs.dll").touch()
            requests = root / "requests"
            requests.mkdir()
            for n in range(1, 8):
                (requests / (str(n) * 64 + ".dxbc")).touch()

            def compile_one(source, *args):
                n = int(source.stem[0])
                if n == 1:
                    return True
                if n == 2:
                    return False
                if n in (3, 4):
                    raise ShaderRefused(
                        f"xshader failed (65): s_load at 0x{n}0: dynamic soffset unsupported",
                        cached=False,
                    )
                if n == 5:
                    raise ShaderRefused("cached unsupported HLSL translation", cached=True)
                if n == 6:
                    raise ShaderRefused("xshader failed (65): unverified mesh shader bindings", cached=False)
                raise ValueError("invalid digest")

            stderr = io.StringIO()
            host = SimpleNamespace(env={"WINEPREFIX": str(root)})
            with patch("project_tuxbox.scarlett_prep.scarlett_requests", return_value=requests), \
                    patch("project_tuxbox.scarlett_prep.snapshot_compiler_cache", return_value=(None, None)), \
                    patch("project_tuxbox.scarlett_prep.desktop_progress", return_value=nullcontext(None)), \
                    patch("project_tuxbox.scarlett_prep.compile_request", side_effect=compile_one) as compile_mock, \
                    redirect_stderr(stderr):
                self.assertTrue(prepare_scarlett(exe, host))
            log = stderr.getvalue()
            self.assertEqual(compile_mock.call_count, 7)
            self.assertIn("1 compiled, 1 cached, 4 unsupported, 1 failed", log)
            self.assertIn("3 new, 1 cached (reason unavailable)", log)
            self.assertIn("2 x s_load at 0x_: dynamic soffset unsupported", log)
            self.assertIn("1 x unverified mesh shader bindings", log)
            self.assertNotIn("x cached unsupported HLSL", log)
            self.assertLess(log.index("2 x s_load"), log.index("1 x unverified"))
