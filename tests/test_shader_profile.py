"""Bounded aggregate statistics across multiple simultaneous shader requests."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import sleep
import unittest
from unittest.mock import patch

from project_tuxbox.runtime_shaders import ShaderRefused
from project_tuxbox.shader_profile import ShaderProfile, timed, count_reuse, gnu_time
from project_tuxbox.shader_prep import run_tool, ToolFailure


class ProfileTests(unittest.TestCase):
    def test_concurrent_requests_have_independent_profiles(self):
        first, second = ShaderProfile(), ShaderProfile()

        def run():
            timed("xshader", lambda: None)
            count_reuse("content")
            return True

        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(profile.request, run, ShaderRefused)
                    for profile in (first, second) for _ in range(100)]
            self.assertTrue(all(job.result() for job in jobs))
        # Neither an unprofiled request nor a previous context leaks into these.
        run()
        for profile in (first, second):
            result = profile.summary()
            self.assertEqual(result["requests"], {"compiled": 100})
            self.assertEqual(result["reuse"], {"content": 100})
            self.assertEqual(result["stages"]["xshader"]["calls"], 100)
            self.assertEqual(result["stages"]["request_total"]["calls"], 100)
            self.assertGreaterEqual(result["stages"]["request_total"]["total_ms"],
                                    result["stages"]["xshader"]["total_ms"])


@unittest.skipUnless(sys.platform == "linux", "Linux child resource accounting")
class ProcessMemoryTests(unittest.TestCase):
    def request(self, profile, code, timeout=5, stage="dxc_default"):
        return profile.request(
            lambda: timed(stage, run_tool, [sys.executable, "-c", code], timeout=timeout),
            ShaderRefused)

    def test_peak_memory_is_measured_per_tool_not_across_children(self):
        profile = ShaderProfile()
        if not gnu_time():
            self.skipTest("GNU time not installed")
        self.request(profile, "import time; data = bytearray(48 * 1024**2); time.sleep(.15)")
        self.request(profile, "import time; data = bytearray(2 * 1024**2); time.sleep(.15)",
                     stage="dxv")
        memory = profile.summary()["process_memory"]
        large, small = memory["dxc_default"], memory["dxv"]
        self.assertEqual((large["attempts"], large["measured"]), (1, 1))
        self.assertGreaterEqual(large["max_peak_rss_kib"], 48 * 1024)
        self.assertGreater(large["max_peak_rss_kib"], small["max_peak_rss_kib"] + 30 * 1024)

    def test_failed_and_timed_out_tools_preserve_exception_and_measurement(self):
        if not gnu_time():
            self.skipTest("GNU time not installed")
        profile = ShaderProfile()
        with self.assertRaises(ToolFailure) as result:
            self.request(profile, "import sys; print('expected error', file=sys.stderr); sys.exit(65)")
        self.assertEqual(result.exception.returncode, 65)
        self.assertIn("expected error", str(result.exception))
        with self.assertRaises(subprocess.TimeoutExpired):
            self.request(profile, "import time; time.sleep(10)", timeout=.12)
        memory = profile.summary()["process_memory"]["dxc_default"]
        self.assertEqual(memory["attempts"], 2)
        self.assertGreaterEqual(memory["measured"], 1)
        self.assertEqual(profile.summary()["stages"]["dxc_default"]["outcomes"],
                         {"error": 1, "timeout": 1})

    def test_unavailable_timer_does_not_change_tool_result(self):
        profile = ShaderProfile()
        with patch("project_tuxbox.shader_profile.gnu_time", return_value=None):
            self.request(profile, "pass")
        memory = profile.summary()["process_memory"]["dxc_default"]
        self.assertEqual(memory, {"attempts": 1, "measured": 0,
                                  "max_peak_rss_kib": None})

    def test_parallel_processes_do_not_share_peak_rss(self):
        if not gnu_time():
            self.skipTest("GNU time not installed")
        large, small = ShaderProfile(), ShaderProfile()
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(self.request, large,
                              "import time; data = bytearray(64 * 1024**2); time.sleep(.2)")
            two = pool.submit(self.request, small,
                              "import time; data = bytearray(2 * 1024**2); time.sleep(.2)")
            one.result()
            two.result()
        high = large.summary()["process_memory"]["dxc_default"]
        low = small.summary()["process_memory"]["dxc_default"]
        self.assertEqual(high["attempts"], 1)
        self.assertEqual(low["attempts"], 1)
        self.assertGreater(high["max_peak_rss_kib"], low["max_peak_rss_kib"] + 45 * 1024)

    def test_disabled_profile_does_not_start_timer(self):
        with patch("project_tuxbox.shader_profile.gnu_time") as probe:
            run_tool([sys.executable, "-c", "pass"], timeout=5)
        probe.assert_not_called()

    def test_tool_startup_failure_counts_unsampled_attempt(self):
        profile = ShaderProfile()
        with self.assertRaises(FileNotFoundError):
            profile.request(lambda: timed("xshader", run_tool,
                                          ["/nonexistent-tuxbox-test-tool"], timeout=1),
                            ShaderRefused)
        self.assertEqual(profile.summary()["process_memory"]["xshader"],
                         {"attempts": 1, "measured": 0,
                          "max_peak_rss_kib": None})

    def test_short_process_and_signal_status_are_measured(self):
        if not gnu_time():
            self.skipTest("GNU time not installed")
        profile = ShaderProfile()
        # A large allocation in the Python parent must not become the short
        # child's peak just because Linux reports inherited RSS on wait4.
        parent = bytearray(96 * 1024**2)
        self.request(profile, "pass")
        self.assertLess(profile.summary()["process_memory"]["dxc_default"]["max_peak_rss_kib"],
                        len(parent) // 1024)
        with self.assertRaises(ToolFailure) as result:
            self.request(profile, "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual(result.exception.returncode, -15)
        with self.assertRaises(ToolFailure) as result:
            self.request(profile, "raise SystemExit(143)")
        self.assertEqual(result.exception.returncode, 143)


    def test_disappearing_optional_timer_keeps_compiler_available(self):
        profile = ShaderProfile()
        with patch("project_tuxbox.shader_profile.gnu_time", return_value="/nonexistent-tuxbox-time"):
            self.request(profile, "pass")
        self.assertEqual(profile.summary()["process_memory"]["dxc_default"]["measured"], 0)

    def test_timeout_terminates_tool_descendants(self):
        if not gnu_time():
            self.skipTest("GNU time not installed")
        profile = ShaderProfile()
        with TemporaryDirectory() as tmp:
            launched, survived = Path(tmp) / "launched", Path(tmp) / "survived"
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(survived)!r}).touch()"
            code = (f"import subprocess, sys, time; from pathlib import Path; "
                    f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                    f"Path({str(launched)!r}).touch(); time.sleep(10)")
            with self.assertRaises(subprocess.TimeoutExpired):
                self.request(profile, code, timeout=.5)
            self.assertTrue(launched.exists())
            sleep(.7)
            self.assertFalse(survived.exists())
        self.assertEqual(profile.summary()["process_memory"]["dxc_default"]["measured"], 0)
