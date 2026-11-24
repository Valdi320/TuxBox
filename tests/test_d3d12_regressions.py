import unittest
from tools.check_d3d12_regressions import validate,DIGEST

class D3D12RegressionTests(unittest.TestCase):
    def test_adapter_and_exit_are_checked(self):
        line="d3d12-indirect PASS pci=10de:2704 list=59 words=64/64"
        self.assertTrue(validate("indirect","10de:2704",0,line)[0])
        self.assertFalse(validate("indirect","1002:1234",0,line)[0])
        self.assertFalse(validate("indirect","10de:2704",1,line)[0])
        self.assertFalse(validate("indirect","10de:2704",0,line+"\n"+line)[0])
        self.assertFalse(validate("indirect-scarlett","10de:2704",0,line)[0])
    def test_null_checkpoint_must_match(self):
        line="d3d12-null-usage PASS pci=10de:2704 words=64/64 recorded=64 valid=2"
        logs=[line]
        for interval,count in ((1,1),(2,63)):
            logs.append(f"TuxBox d3d12_xs: null usage interval recorded={count} display_bound=0 tracked=1 overflow=0 overflow_display_bound=0 compute={count} raster=0 interval={interval} trigger=recording_checkpoint")
            logs.append(f"TuxBox d3d12_xs: null usage rank kind=compute CS={DIGEST} recorded={count} display_bound=0 interval={interval} trigger=recording_checkpoint")
        output="\n".join(logs)
        self.assertTrue(validate("null-usage","10de:2704",0,output)[0])
        self.assertFalse(validate("null-usage","10de:2704",0,output.replace("compute=63","compute=64"))[0])
        self.assertFalse(validate("null-usage","10de:2704",0,line)[0])
