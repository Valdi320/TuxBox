import unittest
from tools.check_translated_mesh_gpu import CASES, program, validate

class TranslatedMeshControlTests(unittest.TestCase):
    def test_cross_wave_triangle_and_invalid_index(self):
        self.assertIn("v_mov_b32 v27, 0x4210440", program(67))
        self.assertIn("v_cmp_eq_u32 vcc, 127, v26", program(128, True))
        self.assertIn("ds_swizzle_b32 v26, v25 offset:0x41f", program(128, True))
        self.assertIn("v_mov_b32 v27, 0x300400", program(3, bad_index=True))

    def test_guarded_half_position(self):
        for count in (3, 67, 128):
            src = program(count, half=True)
            self.assertIn("v_cmpx_gt_u32 s20, v1", src)
            self.assertIn("v_cvt_f16_f32 v7, v21", src)
            self.assertIn("v_cvt_f32_f16 v7, v7", src)
            self.assertIn("exp pos0 v7,v22,v23,v24 done", src)
            self.assertNotIn("v_cvt_f16_f32", program(count))

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
