// Synthetic native reduction fixture, shared with the translator regression.
use super::{ShaderExtract, Stage};

pub fn shader(width: u32) -> (ShaderExtract, Vec<u32>, usize) {
    let mut block = vec![
        if width == 32 { 0xbe80_3dc1u32 } else { 0xbe80_25c1 },
        0xd501_0002, 0x0002_0080, // v_cndmask v2, 0, v0, s0
        0x4a04_04e9, 0xde54_c102,
        0x4a04_04e9, 0x9360_1202,
        0x4a04_04e9, 0x0009_2402,
        0x4a04_04fa, 0xff09_4002,
        0xd778_0003, 0x0201_0102, // v_permlanex16 v3, v2, 0, 0
        0x4a04_0503,
    ];
    if width == 64 {
        block.extend([0xd760_0006, 0x0001_4102, 0x4a04_0406]);
    }
    block.extend([0xd760_0007, 0x0001_0102,
                  if width == 32 { 0xbefe_0300 } else { 0xbefe_0400 }]);
    let mut words = block.clone();
    let cmp = words.len();
    // Compare the first sum to zero; skip the second reduction if equal.
    // Otherwise increment the active lane inputs before the second sum so
    // the readback can distinguish an executed reduction from a skipped one.
    words.extend([0xbf06_8007, 0xbf85_0000 | (block.len() as u32 + 1), 0x4a00_0081]);
    words.extend(&block);
    words.push(0xbf81_0000);
    let isa: Vec<u8> = words.iter().flat_map(|word| word.to_le_bytes()).collect();
    let ex = ShaderExtract {
        stage: Stage::Compute,
        shader_model: (6, 0),
        required_features: None,
        bitcode_stripped: true,
        fspx_config: None,
        ps09_pixel_inputs: None,
        fssm: Vec::new(),
        entry: Some("main".into()),
        isa_data: isa.clone(),
        isa,
        isa_offset_in_fssm: 0,
        psv_threads: None,
        isa_preamble: [0, width, 1, 1],
        has_input_sig: false,
        has_output_sig: false,
        has_psv0: false,
        has_root_sig: false,
        input_sig: Vec::new(),
        output_sig: Vec::new(),
        resources: Vec::new(),
        gs_info: None,
        mesh_info: None,
        primitive_sig: Vec::new(),
        root_sig: None,
    };
    (ex, words, cmp)
}
