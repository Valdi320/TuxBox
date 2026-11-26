# Synthetic translated mesh control

`tools/check_translated_mesh_gpu.py` (also `check_translator_gpu.py --only translated-mesh`) assembles a handwritten gfx1030 NGG shared-count program, builds FSSM/PSV/RTS0/OSGN containers, invokes xshader and DXC/DXV, and executes the generated desktop mesh shader with a constant-writing portable pixel shader. Valid counts 3, 67, 128 cover low and high logical wave lanes; 0, 129 and out-of-range indices must preserve all seed words. Plain and XOR1-swizzle variants draw with full and clipped scissor. Each case must report the selected GPU and exactly 64 words.

This checks the actual emitted mesh code and geometry, not the Xbox command-list ABI, resource binding, texture formats or retail program coverage. The GPU runner stages a provider as usual, but the guest creates a desktop D3D12 device.
