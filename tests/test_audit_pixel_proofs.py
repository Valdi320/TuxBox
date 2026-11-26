from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json
import unittest
from unittest.mock import patch

from tools.audit_pixel_proofs import audit, classify, probe


class PixelProofAudit(unittest.TestCase):
    def test_only_final_target_family_and_fixed_markers(self):
        message = ("TB_PIXEL_PROOF attrs ambiguous_layout\n"
                   "TB_PIXEL_PROOF spill vector_operation\n"
                   "TB_PIXEL_PROOF spill secret_123\n"
                   "v128 is used both as a lane spill area and as a vector at 0x40")
        self.assertEqual(classify(65, message), ("spill", ("vector_operation",)))
        self.assertEqual(classify(0, message), ("translated", ()))
        self.assertEqual(classify(1, message), ("failed", ()))
        self.assertEqual(classify(65, "PS attr11 has no signature register"), ("attrs", ("unattributed",)))
        self.assertEqual(classify(65, "TB_PIXEL_PROOF attrs packed_half_layout\n32-bit read of half-float PS attr9"),
                         ("attrs", ("packed_half_layout",)))
        self.assertEqual(classify(65, "TB_PIXEL_PROOF attrs no_layout\nunrelated refusal"), ("other_refusal", ()))

    def test_aggregate_no_private_keys(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            emitter = root / "private-tool"
            emitter.write_bytes(b"tool")
            key = "a" * 64 + "_w32.wave32"
            with patch("tools.audit_pixel_proofs.probe", side_effect=[("spill", ("vector_operation",)), ("attrs", ("no_layout",)), ("translated", ())]):
                report = audit([root / key, root / key, root / key], emitter, 1, 1)
            self.assertEqual(report["reasons"], {"spill": {"vector_operation": 1}, "attrs": {"no_layout": 1}})
            self.assertNotIn(key, json.dumps(report))
            self.assertNotIn(str(root), json.dumps(report))

    def test_bad_digest_never_runs_emitter(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / ("a" * 64 + "_w32.wave32")
            source.write_bytes(b"bad")
            with patch("tools.audit_pixel_proofs.subprocess.run") as run:
                self.assertEqual(probe("tool", source, 1), ("failed", ()))
            run.assert_not_called()
