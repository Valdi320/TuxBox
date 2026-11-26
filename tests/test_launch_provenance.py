"""A checkout can launch a title staged with a different provider."""
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import unittest

from project_tuxbox.launch_provenance import report_provider


class ProviderProvenanceTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.exe = self.root / "game" / "Lanes.exe"
        self.exe.parent.mkdir()
        self.built = self.root / "build" / "d3d12_xs.dll"
        self.built.parent.mkdir()
        self.staged = self.exe.with_name("D3D12_XS.dll")
        self.messages = []

    def report(self, required=False):
        return report_provider(self.exe, self.messages.append, require_current=required, built=self.built)

    def test_equal_binaries_match_even_with_case_difference(self):
        self.staged.write_bytes(b"provider A")
        self.built.write_bytes(b"provider A")
        self.report(True)
        self.assertIn(hashlib.sha256(b"provider A").hexdigest(), " ".join(self.messages))
        self.assertIn("match", " ".join(self.messages).lower())

    def test_stale_same_size_binary_warns_and_strict_refuses(self):
        self.staged.write_bytes(b"provider A")
        self.built.write_bytes(b"provider B")
        self.report()
        self.assertIn("mismatch", " ".join(self.messages).lower())
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.report(True)
        self.assertEqual(self.staged.read_bytes(), b"provider A")

    def test_missing_build_and_missing_staged_provider_are_not_matches(self):
        self.staged.write_bytes(b"provider A")
        self.report()
        with self.assertRaisesRegex(ValueError, "build"):
            self.report(True)
        self.staged.unlink()
        self.built.write_bytes(b"provider B")
        self.report()
        with self.assertRaisesRegex(ValueError, "staged"):
            self.report(True)

    def test_ambiguous_case_variants_fail_closed(self):
        self.staged.write_bytes(b"provider A")
        self.exe.with_name("d3d12_xs.dll").write_bytes(b"provider A")
        self.built.write_bytes(b"provider A")
        if len(list(self.exe.parent.iterdir())) != 2:
            self.skipTest("case-insensitive filesystem")
        self.report()
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.report(True)

    def test_strict_launch_stops_before_prepare_or_guest(self):
        from argparse import Namespace
        from contextlib import contextmanager
        from unittest.mock import Mock, patch
        import tuxbox
        self.staged.write_bytes(b"provider A")
        self.built.write_bytes(b"provider B")
        log = Mock()
        @contextmanager
        def logging(*args):
            yield log
        args = Namespace(log_file=None, require_current_provider=True, prepare_only=False)
        host = Mock(backend="native", version="test", offline=False)
        def compare(exe, report, *, require_current):
            return report_provider(exe, report, require_current=require_current, built=self.built)
        with patch("tuxbox.game_log", logging), patch("tuxbox.report_provider", compare), \
             patch("tuxbox.prepare_for_launch") as prepare:
            with self.assertRaisesRegex(ValueError, "mismatch"):
                tuxbox.run_game(args, self.exe, host, self.exe.parent)
            prepare.assert_not_called()
            host.run_game.assert_not_called()
