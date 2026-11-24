//! Raw buffer transfers stay one dword per HLSL `Load`/`Store`.
//!
//! The MUBUF address is 32-bit (`vaddr + soffset + offset`), so a title can produce an offset
//! that wraps, such as 0xFFFFFFFC. Under vkd3d-proton on NVIDIA, a multi-dword raw load
//! (`Load2`..`Load4`) is lowered through `VK_NV_raw_access_chains`; one 16-byte load at
//! 0xFFFFFFFC lost the device (Xid 109) on an RTX 5080 with Proton Experimental 11.0-20261112.
//! Four 4-byte loads at the same offsets completed and returned zero for the out-of-range word.
//! Merging the per-dword transfers would reintroduce that fault, so this test pins the form.
use super::Emitter;
use crate::rdna2::disassemble;
use crate::{ShaderExtract, Stage};

fn compute_extract(words: &[u32]) -> ShaderExtract {
    let isa: Vec<u8> = words.iter().flat_map(|w| w.to_le_bytes()).collect();
    ShaderExtract {
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
        isa_preamble: [0, 64, 1, 1],
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
    }
}

#[test]
fn raw_buffer_transfers_are_emitted_one_dword_at_a_time() {
    // vaddr v2 (OFFEN), vdata v[4:7], srsrc s[8:11], soffset 0.
    let cases = [
        ([0xe038_1000, 0x8002_0402], "buffer_load_dwordx4", "Load"),
        ([0xe078_1000, 0x8002_0402], "buffer_store_dwordx4", "Store"),
    ];
    for (words, mnemonic, op) in cases {
        let ex = compute_extract(&words);
        let insns = disassemble(&ex.isa);
        assert_eq!(insns[0].mnemonic, mnemonic);
        let mut em = Emitter::new(&ex, insns.clone());
        em.raw_transfer_on(&insns[0], "raw_ubuf0".into(), true).unwrap();
        assert_eq!(em.body.matches(&format!("raw_ubuf0.{op}(")).count(), 4, "{mnemonic}:\n{}", em.body);
        for wide in ["2(", "3(", "4("] {
            assert!(!em.body.contains(&format!("{op}{wide}")), "{mnemonic} merged:\n{}", em.body);
        }
    }
}

/// Locally constructed linear SRV, followed by both indexed address forms.
/// Encodings produced by llvm-mc -mcpu=gfx1030.
fn indexed_fixture() -> ShaderExtract {
    let mut ex = compute_extract(&[
        0xbe8b_03ff, 0x1101_4fac, // format
        0xbe8a_03c1, // count
        0xbe88_030c, // low address
        0x8809_ff0d, 0x0001_0000, // high address | byte stride
        0xe038_2000, 0x8002_0402, // IDXEN
        0xe038_300c, 0x8002_0402, // IDXEN + OFFEN, immediate 12
        0xbf81_0000,
    ]);
    ex.resources.push(crate::psv::PsvResource {
        res_type: 4, space: 0, lower: 0, upper: 0,
    });
    ex
}

#[test]
fn byte_index_proof_rejects_unknown_stride_and_clobbers() {
    let ex = indexed_fixture();
    let ops = disassemble(&ex.isa);
    assert!(super::raw_index::local_byte_descriptor(&ops[..4], 8));
    assert!(super::raw_index::local_byte_descriptor(&ops[..5], 8));
    assert!(!super::raw_index::local_byte_descriptor(&ops[1..4], 8));
    assert!(!super::raw_index::local_byte_descriptor(&ops[..4], 12));
    let mut wrong = ops[..4].to_vec();
    wrong[3].src[1] = crate::rdna2::Opnd::Literal(0x40000);
    assert!(!super::raw_index::local_byte_descriptor(&wrong, 8));
    wrong = ops[..4].to_vec();
    wrong.push(ops[1].clone()); // clobbers count
    assert!(!super::raw_index::local_byte_descriptor(&wrong, 8));
}

#[test]
fn proven_byte_index_emits_addresses_without_widening_loads() {
    let ex = indexed_fixture();
    let translated = super::translate(&ex).unwrap();
    assert_eq!(translated.hlsl.matches("raw_sbuf0.Load(").count(), 8);
    // Captured by the ordinary test harness; available to the GPU control
    // with --nocapture, without exposing a test-only API in the library.
    println!("BEGIN_HLSL\n{}\nEND_HLSL", translated.hlsl);
    let ops = disassemble(&ex.isa);
    let mut em = Emitter::new(&ex, ops.clone());
    em.resolve_resources().unwrap();
    assert_eq!(em.raw_byte_index.len(), 2);
    assert!(em.raw_byte_index.contains(&ops[4].offset));
    assert!(em.raw_byte_index.contains(&ops[5].offset));
    em.emit_mubuf(4, &ops[4]).unwrap();
    em.emit_mubuf(5, &ops[5]).unwrap();
    assert!(em.body.contains("= v2 + 0x0u + 0u;"), "{}", em.body);
    assert!(em.body.contains("= (v2 + v3) + 0x0u + 12u;"), "{}", em.body);
    assert_eq!(em.body.matches("raw_sbuf0.Load(").count(), 8);
    assert!(!em.body.contains("raw_sbuf0.Load4("));
    assert!(em.regs.contains("v3"));
    em.raw_byte_index.clear();
    assert!(em.emit_mubuf(4, &ops[4]).unwrap_err().contains("stride"));
}

#[test]
fn indexed_descriptor_proof_does_not_cross_a_basic_block() {
    let mut ex = indexed_fixture();
    // Insert an unconditional branch between construction and the load.
    let mut words: Vec<u32> = ex.isa.chunks_exact(4)
        .map(|w| u32::from_le_bytes(w.try_into().unwrap())).collect();
    words.insert(6, 0xbf82_0000);
    ex.isa = words.iter().flat_map(|w| w.to_le_bytes()).collect();
    let ops = disassemble(&ex.isa);
    let mut em = Emitter::new(&ex, ops.clone());
    em.resolve_resources().unwrap();
    assert!(em.raw_byte_index.is_empty());
}
