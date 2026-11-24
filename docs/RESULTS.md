# Measured homebrew results

Each column represents one run on its named host, provider and hardware. Each receipt records the guest output, exit status and binary hashes.

| Gate | [ge-nvidia](../evidence/ge-nvidia.json) | [valdi-bazzite-rtx4080](../evidence/valdi-bazzite-rtx4080.json) | [valve-amd](../evidence/valve-amd.json) | [valve-nvidia](../evidence/valve-nvidia.json) | [wine-nvidia](../evidence/wine-nvidia.json) | Physical Windows x64 |
|---|---|---|---|---|---|---|
| Upper route and guest output | Pass | Pass | Pass | Pass | Pass | Pending |
| Lower route and collision | Pass | Pass | Pass | Pass | Pass | Pending |
| Invalid input exit status | Pass | Pass | Pass | Pass | Pass | Pending |
| Xbox DXC utilities and binary blob lifetime | Pass | Pass | Pass | Pass | Pass | Pending |
| Native Scarlett XG texture layouts with Rust XMem | Pass | — | Pass | Pass | Pass | Pending |
| Xbox memory allocation and working set | Pass | Pass | Pass | Pass | Pass | Pending |
| Desktop D3D12 pixel readback | Pass | Pass | Pass | Pass | Pass | Pending |
| Xbox root-signature import and compute readback | Pass | Pass | Pass | Pass | Pass | Pending |
| Xbox command-list clear and ordered copy readback | Pass | Pass | Pass | Pass | Fail | Pending |
| Guest command rejection, fence isolation and list recovery | Pass | Pass | Pass | Pass | Fail | Pending |
| Xbox PresentX and two screen-verified frames | Fail | Pass | Fail | Pass | Fail | Pending |
| Lanes gameplay replay (earlier guest with desktop graphics) | Pass | — | Pass | Pass | Pass | Pending |
| 8,388,608-move CPU replay | Pass | — | Pass | Pass | Pass | Pending |
| Xbox DXC caller allocator and factory ordinals | — | Pass | — | — | — | Pending |
| Scarlett DXC compute shader and compiler diagnostics | — | Pass | — | — | — | Pending |
| xdxr-link | — | Pass | — | — | — | Pending |
| Root signature 1.0 and 1.1 serialization, deserialization and errors | — | Pass | — | — | — | Pending |
| D3D12 and DXGI identifier data by name and ordinal | — | Pass | — | — | — | Pending |
| xg-dcc | — | Pass | — | — | — | Pending |
| xg-library | — | Pass | — | — | — | Pending |
| TuxBox XG texture layouts and element addresses | — | Pass | — | — | — | Pending |
| TuxBox XG layouts and addresses across 78 formats | — | Pass | — | — | — | Pending |
| Game Runtime initialization, task delivery and cancellation | — | Pass | — | — | — | Pending |
| Opus PCM, decoder pre-skip and command completion | — | Pass | — | — | — | Pending |
| Xbox storage file reads, zlib and queue completion | — | Pass | — | — | — | Pending |
| CPU title and private heap creation | — | Pass | — | — | — | Pending |
| CPU heap attribute profiles | — | Pass | — | — | — | Pending |
| CPU virtual-memory allocation profiles | — | Pass | — | — | — | Pending |
| CPU scattered physical-page mapping | — | Pass | — | — | — | Pending |
| Guest write-watch dirty-page query | — | Pass | — | — | — | Pending |
| Guest allocator callback routing | — | Pass | — | — | — | Pending |
| Memory allocation tag encoding | — | Pass | — | — | — | Pending |
| xmem-http | — | Pass | — | — | — | Pending |
| CPU virtual memory and image queries | — | Pass | — | — | — | Pending |
| xmem-partitions | — | Pass | — | — | — | Pending |
| CPU page allocation, remapping and release | — | Pass | — | — | — | Pending |
| Blob, debug interface, four DXGIX entry points and Series CPU memory clear | — | Pass | — | — | — | Pending |
| Xbox capability records compared with the selected adapter | — | Pass | — | — | — | Pending |
| Placed resources and allocation offsets in a shared memory heap | — | Pass | — | — | — | Pending |
| Virtual-address BC1 cubes and R8 textures across page changes | — | Pass | — | — | — | Pending |
| Whole-buffer and multi-mip texture copies through Xbox command-list slot 84 | — | Pass | — | — | — | Pending |
| Scissored triangle through Xbox graphics pipeline and draw slots | — | Pass | — | — | — | Pending |
| Mesh and amplification shader dispatch on a mesh-tier adapter | — | Pass | — | — | — | Pending |
| Frame origins, timeout tokens, completed presentation events and display statistics | — | Pass | — | — | — | Pending |
| Shader reads and writes through tables, including a late-copied texture view | — | Pass | — | — | — | Pending |
| Direct, compute and copy queue synchronization | — | Pass | — | — | — | Pending |
| GPU timestamps, occlusion readback and descriptor heap handles | — | Pass | — | — | — | Pending |
| Indirect dispatch and texture cache flushes between compute writes | — | Pass | — | — | — | Pending |
| Xbox Device2 graphics pipeline stream, ownership and masked draw | — | Pass | — | — | — | Pending |
| Xbox 0x20004 and default factories, compute queues and process debug flags | — | Pass | — | — | — | Pending |
| RGB10A2 display allocation and two guarded GPU readbacks | — | Pass | — | — | — | Pending |
| Two RGB10A2 frames sampled from the desktop window | — | Pass | — | — | — | Pending |
| 4K RGB10 display scaled to a TuxBox window | — | Pass | — | — | — | Pending |
| 4K RGB10 display fitted to the title window’s monitor | — | Pass | — | — | — | Pending |
| 4K RGB10 display scaled inside a smaller title window | — | Pass | — | — | — | Pending |
| Lanes Xbox imports, course, board readback and gameplay replay | — | Pass | — | — | — | Pending |

Rebuild this table from the repository root with `python tuxbox.py status > docs/RESULTS.md`. A dash means that the gate was omitted from that run. [Current Lanes replay](../examples/lanes/README.md#deterministic-replay) · [Xbox DLL checks](LIBRARIES.md#run-the-provider-checks) · [Retail results and priorities](PROGRESS.md)
