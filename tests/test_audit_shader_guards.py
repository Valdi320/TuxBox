import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.audit_shader_guards import audit, guard_sites, probe


class GuardAuditTests(unittest.TestCase):
    def test_sites_distinguish_comments_and_dispatchers(self):
        data = b"""// ++tb_guard < 65536u
        /* while (tb_pc != 7u && tb_guard++ < 1000000u) */
        } while (cond && ++tb_guard < 65536u);
        while (tb_pc != 7u && tb_guard++ < 1000000u) {
        while (tb_pc != 9u) {
        """
        self.assertEqual(guard_sites(data), dict(structured=1, guarded_dispatch=1, other_dispatch=1))

    def test_same_snapshot_and_privacy(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"private input"
            key = hashlib.sha256(data).hexdigest() + "_w32.wave32"
            source = root / key
            source.write_bytes(data)
            old, new = root / "old", root / "new"
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            def fake(emitter, snap, timeout):
                self.assertEqual(snap.name, key)
                self.assertEqual(snap.read_bytes(), data)
                if emitter.parent.name == "baseline":
                    source.write_bytes(b"changed")
                    return dict(status="refused")
                return dict(status="compute", structured=1, guarded_dispatch=0, other_dispatch=0, hlsl_bytes=9)
            with patch("tools.audit_shader_guards.probe", side_effect=fake):
                report = audit([source], old, new, 1, 1)
            self.assertEqual(report["newly_admitted_compute"], 1)
            self.assertEqual(report["newly_admitted_guarded"], 1)
            self.assertEqual(report["inconclusive"], 0)
            self.assertNotIn(key, json.dumps(report))
            self.assertNotIn(str(root), json.dumps(report))

    def test_invalid_archive_never_runs_emitter(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / ("a" * 64 + "_w32.wave32")
            source.write_bytes(b"wrong digest")
            old, new = root / "old", root / "new"
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            with patch("tools.audit_shader_guards.probe") as emit:
                report = audit([source], old, new, 1, 1)
            emit.assert_not_called()
            self.assertEqual(report["inconclusive"], 1)

    def test_missing_emission_cannot_count_as_success(self):
        from subprocess import CompletedProcess
        with patch("tools.audit_shader_guards.subprocess.run", return_value=CompletedProcess([], 0)):
            self.assertEqual(probe("xshader", Path("a.dxbc"), 1), dict(status="failed"))
