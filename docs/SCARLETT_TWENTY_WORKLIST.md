# Prioritized Scarlett worklist — 2026-11-24

Experimental integration of the nine pending contributions, not an upstream merge request.
The ranking is qualitative and is not a measured contribution to game compatibility.

| # | Objective | Current closure gate |
|---|---|---|
| 1 | Dynamic resource descriptors | PR89 finite CBV subset verified; arbitrary offsets and other classes remain open |
| 2 | Divergent wave reconvergence | Open; requires participation-correct model |
| 3 | Silent numerical defects | PR85 differential packed chains; coverage incomplete |
| 4 | ExecuteIndirect/DriverHintX | PR87 compute ABI control passes on both layouts; other signatures not covered |
| 5 | Null usage ranking | PR83 and PR88 forced compute checkpoint control; raster and title not verified |
| 6 | Compute PSO failures | Exact failed title paths still open |
| 7 | Raster linkage | Exact mixed producer/consumer reproduction still open |
| 8 | Minimization | PR85 prefix replay; general delta minimization open |
| 9 | Scalar differential tests | PR60 integrated; extend physical GPU tests |
| 10 | Vector differential tests | PR85 packed family; other families open |
| 11 | Wave32/64 reduction controls | Existing original guards revalidated on PR89: 102 cases, 39168 words; not arbitrary divergence |
| 12 | Mesh | Existing GPU mesh control passes; additional translator support open |
| 13 | GPU memory access | Placement/heap guards pass; specific faults need reproductions |
| 14 | Remap/reuse | PR86 and placement controls; targeted imported buffer remap test remains |
| 15 | Memory retention | PR62 integrated; physical VRAM/long title measurements open |
| 16 | HLSL optimization | PR85 equivalent saturation; broader optimization open |
| 17 | NVIDIA regression automation | PR88 nine synthetic controls, four revalidated jointly |
| 18 | AMD controls | PCI-selectable runner; AMD execution unverified |
| 19 | First bad frame/pass | PR61 integrated; pass-level capture/replay open |
| 20 | Integrated validation | Nine PRs jointly tested and provider rebuilt; a mandatory CI gate and actual retail coverage remain open |

The latest upstream documentation counts 6487 prepared requests of 16025.
These are not directly comparable to the older Bruno archive.

See the joint [receipt](../evidence/scarlett-joint-twenty-20261124.json).
