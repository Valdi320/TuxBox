import struct
import unittest
from tools.check_mask_ballots_gpu import wave64_mode, model, difference, seed_for


class BallotControlTests(unittest.TestCase):
    def test_select_capability_not_vendor(self):
        info = "GPU0:\n vendorID = 0x10de\n deviceID = 0x2704\n minSubgroupSize = 32\n maxSubgroupSize = 32\n"
        self.assertEqual(wave64_mode("10de:2704", info), "paired")
        self.assertEqual(wave64_mode("10de:2704", info.replace("maxSubgroupSize = 32", "maxSubgroupSize = 64")), "native")
        with self.assertRaises(ValueError):
            wave64_mode("1002:1234", info)
        with self.assertRaises(ValueError):
            wave64_mode("10de:2704", info.replace("maxSubgroupSize = 32", ""))

    def test_paired_model_still_uses_entire_guest_wave(self):
        values = [1001] * 64
        mask = (1 << 64) - 1
        seed = seed_for(mask, values)
        want = seed.copy()
        for lane, (count, rank, first) in enumerate(model("compare", [True] * 64, values, 64)):
            want[160 + lane] = count | rank << 8 | 32 << 16
            want[224 + lane] = first
        data = struct.pack("<384I", *want)
        self.assertIsNone(difference("compare", seed, mask, values, data, 32))
        self.assertIsNotNone(difference("compare", seed, mask, values, data, 64))
        want[192] ^= 1
        self.assertIsNotNone(difference("compare", seed, mask, values, struct.pack("<384I", *want), 32))
