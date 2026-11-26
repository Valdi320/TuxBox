import unittest
from tools.check_translated_mesh_gpu import CASES, program, validate

class TranslatedMeshControlTests(unittest.TestCase):
    def test_cross_wave_triangle_and_invalid_index(self):
        self.assertIn("v_mov_b32 v27, 0x4210440", program(67))
        self.assertIn("v_cmp_eq_u32 vcc, 127, v26", program(128, True))
        self.assertIn("ds_swizzle_b32 v26, v25 offset:0x41f", program(128, True))
        self.assertIn("v_mov_b32 v27, 0x300400", program(3, bad_index=True))

    def test_every_case_and_adapter_required(self):
        fields = {f"{case}_{clip}{suffix}": "10de:2704" if suffix else "64"
                  for case in CASES for clip in ("full", "clipped") for suffix in ("", "_adapter")}
        self.assertTrue(validate(0, fields, "10de:2704"))
        self.assertFalse(validate(1, fields, "10de:2704"))
        fields["swizzle-67_full"] = "63"
        self.assertFalse(validate(0, fields, "10de:2704"))
        fields["swizzle-67_full"] = "64"
        fields.pop("plain-3_full_adapter")
        self.assertFalse(validate(0, fields, "10de:2704"))
