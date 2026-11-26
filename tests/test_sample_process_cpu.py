"""Memory sampling keeps missing data separate from actual zero use."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.sample_process_cpu import memory_sample


class ProcessMemoryTests(unittest.TestCase):
    def test_reads_real_units_and_calculates_used_swap(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            meminfo = root / "meminfo"
            meminfo.write_text("MemTotal: 98304 kB\nMemAvailable: 8192 kB\nSwapTotal: 32768 kB\nSwapFree: 128 kB\n")
            (root / "status").write_text("VmRSS: 5120 kB\nVmHWM: 8192 kB\nVmSwap: 0 kB\n")
            self.assertEqual(memory_sample(root, meminfo), {
                "host_available_kib": 8192, "host_total_kib": 98304,
                "host_swap_total_kib": 32768, "host_swap_free_kib": 128,
                "host_swap_used_kib": 32640, "process_rss_kib": 5120,
                "process_peak_rss_kib": 8192, "process_swap_kib": 0,
            })

    def test_partial_or_malformed_data_is_not_zero(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            meminfo = root / "meminfo"
            meminfo.write_text("MemTotal: bad kB\nMemAvailable: -1 kB\nSwapTotal: 10 kB\nSwapFree: 20 kB\n")
            (root / "status").write_text("VmRSS: 2 MB\nVmSwap: 4 kB\n")
            result = memory_sample(root, meminfo)
            for key in ("host_total_kib", "host_available_kib", "host_swap_used_kib",
                        "process_rss_kib", "process_peak_rss_kib"):
                self.assertIsNone(result[key])
            self.assertEqual(result["process_swap_kib"], 4)
            self.assertTrue(all(v is None for v in memory_sample(root / "absent", root / "absent").values()))
