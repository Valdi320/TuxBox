import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from tools.diagnose_scarlett_run import analyze, main

STAMP="[TuxBox 2026-11-24T07:37:07+00:00] "
D3D="01e0:warn:debugstr:OutputDebugStringA \"TuxBox d3d12_xs: "


class DiagnosisTests(unittest.TestCase):
    def report(self, lines):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"run.log"
            data=("\n".join(lines)+"\n").encode()
            path.write_bytes(data)
            report=analyze(path)
            self.assertEqual(report["log_sha256"],hashlib.sha256(data).hexdigest())
            return report

    def test_incomplete_is_not_a_crash_and_keeps_actual_evidence(self):
        key="a"*64
        report=self.report([STAMP+"Game log: /private/user/run",
            STAMP+"Starting game",D3D+"created host D3D12 device (adapter LUID 00:00)",
            D3D+"PresentX presented first frame (3840x2160)",
            D3D+"slot 86 pipeline stream (688 bytes) failed: 0x8007007e",
            "00e4:warn:seh:dispatch_exception RPC_S_SERVER_UNAVAILABLE exception (code=6ba) raised",
            STAMP+f"Xbox shader {key}: xshader failed (65): test",
            STAMP+f"Xbox shader {key}: xshader failed (65): test",
            STAMP+f"Compiled Xbox shader {key}"])
        session=report["sessions"][0]
        self.assertEqual(session["outcome"],"incomplete_log_no_exit_record")
        self.assertEqual(session["last_milestone"],"first_present")
        self.assertEqual(session["last_observed_shader_status"],{"compiled":1})
        self.assertEqual(session["shader_events"],{"refused":2,"compiled":1})
        self.assertEqual(session["findings"][0]["last"]["line"],5)
        self.assertIn("which module is not established",session["findings"][0]["last"]["meaning"])
        self.assertNotIn("/private/user",json.dumps(report))
        self.assertNotIn(key,json.dumps(report))

    def test_sessions_and_exit_codes(self):
        sessions=self.report([STAMP+"Game log: one",STAMP+"Starting game",
            STAMP+"Game exited with 3 after 97.0s", STAMP+"Game log: two",
            STAMP+"Starting game",STAMP+"Game exited with 0 after 1.2s",
            STAMP+"Game log: three",STAMP+"Launch interrupted",
            STAMP+"Game log: four",STAMP+"Launch failed: private path"])["sessions"]
        self.assertEqual([s["outcome"] for s in sessions],["nonzero_exit","normal_exit","interrupted","launch_failure"])
        self.assertEqual(sessions[0]["terminal"]["detail"],{"code":3,"seconds":97.0})
        self.assertEqual(sessions[1]["first_line"],4)

    def test_terminal_exception_and_survey_are_not_equivalent(self):
        s=self.report([D3D+"survey: stepped over title assert RVA 0x344196 (hit 1)",
            D3D+"continued past recoverable title assert RVA 0x4ffd879 (hit 1)",
            D3D+"survey: unsupported D3D12.x vtable slot 25 from title RVA 0x1234 (hit 1)",
            "wine: Unhandled exception 0xc0000005 in thread 0040 at address 1234",
            D3D+"compute pipeline creation failed (0x80004005, 12 bytes, unknown)",
            "warn: VK_ERROR_DEVICE_LOST"])["sessions"][0]
        self.assertEqual(s["outcome"],"unhandled_exception_observed_no_exit_record")
        self.assertEqual(s["findings"][0]["last"]["detail"]["rva"],"0x344196")
        self.assertEqual(s["findings"][1]["last"]["detail"]["rva"],"0x4ffd879")
        self.assertEqual(s["findings"][2]["last"]["detail"]["slot"],25)
        self.assertEqual(s["investigation_order"][0]["last"]["kind"],"device_loss_report")
        self.assertEqual(s["investigation_order"][1]["last"]["code"],"0xc0000005")

    def test_missing_import_and_absolute_fault_address_are_distinct(self):
        report=self.report(['0040:err:module:import_dll Library dxil.dll (which is needed by L"/private/title.exe") not found',
                            "wine: Unhandled exception 0xc0000005 in thread 0040 at address 0000000140001234 (thread 0040), starting debugger..."])
        s=report["sessions"][0]
        self.assertEqual(s["findings"][0]["last"]["detail"],{"library":"dxil.dll"})
        self.assertEqual(s["findings"][1]["last"]["detail"],{"absolute_address":"0x140001234","wine_thread":"0x40"})
        self.assertNotIn("/private",json.dumps(report))

    def test_timestamp_clock_reverse_and_unknown_provider_time(self):
        s=self.report([STAMP+"Game log: x",D3D+"PresentX presented first frame (10x10)",
                       "[TuxBox 2026-11-24T07:00:00+00:00] Starting game"])["sessions"][0]
        self.assertTrue(s["timestamp_order_reversed"])
        self.assertIsNone(s["milestones"][0]["utc"])

    def test_long_line_does_not_create_false_faults(self):
        report=self.report([STAMP+"Game log: x","x"*65536+"wine: Unhandled exception 0xc0000005",
                            STAMP+"Game exited with 0 after 1.0s"])
        self.assertEqual(report["oversize_lines_skipped"],1)
        self.assertEqual(report["sessions"][0]["findings"],[])
        self.assertEqual(report["sessions"][0]["terminal"]["line"],3)

    def test_link_and_stencil_keys_do_not_collide(self):
        k="a"*64
        s=self.report([STAMP+f"Compiled Xbox shader {k}",
            STAMP+f"Xbox shader {k}_rt0: xshader failed (65): test",
            STAMP+f"Xbox shader {k}_{'b'*64}_{'c'*64}: xshader failed (1): test"])["sessions"][0]
        self.assertEqual(s["unique_shader_requests"],3)
        self.assertEqual(s["last_observed_shader_status"],{"compiled":1,"refused":1,"failed":1})
