use super::*;
use crate::rdna2::disassemble;

/// Assemble a pixel program with saved coverage, WQM, X/Y swaps, and a masked export.
fn axis_words() -> Vec<u32> {
    vec![
        0xbe88047e, 0xbefe0a7e, 0x7e040f02, 0x7e060f03,
        0x34060685, 0x4a080702, 0x4a0808ff, 99,
        0x7e0a02fa, 0xff0cb104, 0x4a0c0905, 0x7e0e02fa,
        0xff0c4e06, 0x4a140f06, 0x87fe087e, 0xf800180f,
        0x040a0705, 0xbf810000,
    ]
}

/// Build a nonheap pixel extraction whose ISA is supplied as instruction words.
fn pixel(words: &[u32]) -> crate::ShaderExtract {
    let isa: Vec<_> = words.iter().flat_map(|w| w.to_le_bytes()).collect();
    crate::ShaderExtract {
        stage: crate::Stage::Pixel, shader_model: (5, 0), required_features: None,
        bitcode_stripped: true, fspx_config: None, ps09_pixel_inputs: None,
        fssm: Vec::new(), entry: Some("main".into()), isa_data: isa.clone(), isa,
        isa_offset_in_fssm: 0, psv_threads: None, flat_inputs: Vec::new(),
        flat_outputs: Vec::new(), isa_preamble: [0, 64, 1, 1], has_input_sig: false,
        has_output_sig: false, has_psv0: false, has_root_sig: false,
        input_sig: Vec::new(), output_sig: vec![super::super::SigElement {
            name: "SV_Target".into(), index: 0, sysval: 64, comp_type: 1,
            register: 0, mask: 15, rw_mask: 15,
        }], resources: Vec::new(),
        gs_info: None, mesh_info: None, primitive_sig: Vec::new(), root_sig: None,
    }
}

/// Decode test words and apply the DPP lowering used by shader translation.
fn lowered(words: &[u32]) -> Vec<Insn> {
    let mut code = disassemble(&pixel(words).isa);
    super::super::lower_wave_reductions(&mut code).unwrap();
    code
}

/// Axis swaps in a proven straight prefix lower to quad reads without a fixed wave width.
#[test]
fn straight_nonheap_axis_swaps_translate_without_wave_width() {
    let translated = super::super::translate(&pixel(&axis_words())).unwrap();
    assert_eq!(translated.profile, "ps_6_6");
    assert_eq!(translated.required_wave_size, None);
    let h = &translated.hlsl;
    assert!(h.contains("QuadReadAcrossX(v4)"), "{h}");
    assert!(h.contains("QuadReadAcrossY(v6)"), "{h}");
    assert!(!h.contains("WaveReadLaneAt("), "{h}");
    assert!(!h.contains("QuadReadAcrossDiagonal("), "{h}");
    assert!(!h.contains("WaveActiveBallot("), "{h}");
    assert!(h.contains("l = 1u, t"), "{h}");
    assert!(h.contains("scc = ((t"), "{h}");
}

/// NOP setup and a forward branch over the masked export retain prefix eligibility.
#[test]
fn straight_prefix_accepts_nop_setup_and_forward_masked_tail() {
    let mut words = axis_words();
    words.insert(0, 0x7e000000); // v_nop padding before entry setup.
    // The branch after the last swap skips only the masked export.
    words.insert(16, 0xbf880002);
    assert_eq!(straight_prefix(&lowered(&words)), Some(2));
    let translated = super::super::translate(&pixel(&words)).unwrap();
    assert_eq!(translated.required_wave_size, None);
    assert_eq!(translated.profile, "ps_6_6");
    assert!(translated.hlsl.contains("QuadReadAcrossX(v4)"));
    assert!(translated.hlsl.contains("QuadReadAcrossY(v6)"));
}

/// Prefix analysis rejects unsafe control flow, EXEC changes, and other quad exchanges.
#[test]
fn straight_prefix_refuses_unsafe_nonheap_translation() {
    for replacement in [
        0xbf840002, // SCC branch guarding part of the prefix.
        0xbf810000, // Early end before either exchange.
        0x87fe087e, // Mask EXEC with saved coverage before either exchange.
    ] {
        let mut words = axis_words();
        words[2] = replacement;
        assert_eq!(straight_prefix(&lowered(&words)), None);
        assert!(super::super::translate(&pixel(&words)).is_err());
    }
    let mut words = axis_words();
    words[9] = 0xff0c0004; // Broadcast is outside the admitted axis permutations.
    assert_eq!(straight_prefix(&lowered(&words)), None);
    assert!(super::super::translate(&pixel(&words)).is_err());
    let mut words = axis_words();
    words.insert(17, 0xbf82fff0); // Backedge into the prefix at byte offset 8.
    assert_eq!(straight_prefix(&lowered(&words)), None);
    assert!(super::super::translate(&pixel(&words)).is_err());
}

/// FI, bank and row masks, and selectors determine axis-prefix eligibility.
#[test]
fn straight_prefix_requires_fi_full_masks_and_axis_controls() {
    for control in [
        0xff08_b104, // FI=0; bound control remains enabled.
        0xff04_b104, // FI=1 also works with bound control disabled.
        0xfe0c_b104, // One bank disabled.
        0xef0c_b104, // One row disabled.
        0xff0c_e404, // Identity permutation.
        0xff0c_1b04, // Diagonal permutation.
        0xff0c_4e04, // Y is admitted independently of X.
    ] {
        let mut words = axis_words();
        words[9] = control;
        let admitted = matches!(control, 0xff04_b104 | 0xff0c_4e04);
        assert_eq!(straight_prefix(&lowered(&words)).is_some(), admitted, "{control:08x}");
        let translated = super::super::translate(&pixel(&words));
        if admitted {
            assert_eq!(translated.unwrap().required_wave_size, None);
        } else {
            assert!(translated.is_err(), "neighboring pixel DPP remains refused: {control:08x}");
        }
    }
}

/// Raw instruction fields and lowered operands must agree for prefix eligibility.
#[test]
fn straight_prefix_checks_raw_and_lowered_move_provenance() {
    let code = lowered(&axis_words());
    let at = code.iter().position(is_quad_exchange).unwrap();
    for (word, mask) in [(0, 1), (0, 1 << 9), (1, 1 << 17), (1, 1 << 20),
                         (1, 1 << 21), (1, 1 << 22), (1, 1 << 23),
                         (1, 1), (1, 1 << 8)] {
        let mut changed = code.clone();
        changed[at].raw[word] ^= mask;
        assert_eq!(straight_prefix(&changed), None, "word={word} mask={mask:x}");
    }
    let mut changed = code.clone();
    changed[at].src[0] = Opnd::Vgpr(99);
    assert_eq!(straight_prefix(&changed), None);
    let mut changed = code.clone();
    changed[at].dst[0] = Opnd::Vgpr(99);
    assert_eq!(straight_prefix(&changed), None);
    let mut changed = code.clone();
    changed[at].dwords = 1;
    assert_eq!(straight_prefix(&changed), None);
    let mut changed = code;
    changed[at].raw.pop();
    assert_eq!(straight_prefix(&changed), None);
}

/// An independent bpermute still imposes its wave-width requirement.
#[test]
fn straight_prefix_preserves_independent_wave_width() {
    let mut words = axis_words();
    // DS bpermute v11, v0, v4 independently requires a wave32 mapping.
    words.splice(14..14, [0xdacc_0000, 0x0b00_0400]);
    assert_eq!(straight_prefix(&lowered(&words)), Some(1));
    let error = super::super::translate(&pixel(&words)).err().unwrap();
    assert_eq!(error, "wave operation requires complete 32-lane compute waves");
}

/// Prefix eligibility requires entry WQM before vector work and excludes implicit EXEC writes.
#[test]
fn straight_prefix_checks_entry_and_implicit_exec_writes() {
    let code = lowered(&axis_words());
    assert_eq!(straight_prefix(&code), Some(1));
    for mnemonic in ["v_cmpx_eq_u32", "s_and_saveexec_b64", "s_and_wrexec_b64"] {
        let mut changed = code.clone();
        changed[3].mnemonic = mnemonic.into();
        assert_eq!(straight_prefix(&changed), None);
    }
    let mut late_entry = code.clone();
    late_entry.swap(1, 2);
    assert_eq!(straight_prefix(&late_entry), None);
    for mnemonic in ["s_setpc_b64", "s_swappc_b64", "s_rfe_b64", "s_call_b64"] {
        let mut indirect = code.clone();
        indirect.last_mut().unwrap().mnemonic = mnemonic.into();
        assert_eq!(straight_prefix(&indirect), None);
    }
}

#[test]
fn distinguishes_saved_coverage_from_scalar_bits() {
    // save EXEC, mask it with VCC, subtract it from the saved coverage,
    // skip an empty result, expand to whole quads, restore working EXEC.
    let words: [u32; 6] = [
        0xbe9a047e, 0xbe8c246a, 0x8a9a7e1a, 0xbf840002, 0xbe8e0a1a, 0x87fe0e0c,
    ];
    let bytes: Vec<_> = words.into_iter().flat_map(u32::to_le_bytes).collect();
    let mut code = disassemble(&bytes);
    assert!(guards_expansion(&code, 2));
    assert!(saved_exec(&code, 4));
    code[0].src[0] = Opnd::Sgpr(30);
    assert!(!guards_expansion(&code, 2));
    assert!(!saved_exec(&code, 4));
}

#[test]
fn pixel_embedded_table_offset_is_bounded_and_aligned() {
    // gfx1030: getpc; mask and scale a scalar offset; load; export uint.
    let words = [0xbe881f00, 0x870a830c, 0x8f0a820a, 0xf4000404,
                 0x14000020, 0x7e080210, 0xf800080f, 0x04040404, 0xbf810000];
    let mut ex = pixel(&words);
    ex.isa_data.extend([101u32, 217, 333, 449].iter().flat_map(|x| x.to_le_bytes()));
    let translated = super::super::translate(&ex).unwrap();
    assert!(translated.hlsl.contains("tb_code["));
    assert!(translated.hlsl.contains("s8 + s10 + 0x20u - 36u"));
    println!("BEGIN_HLSL\n{}\nEND_HLSL", translated.hlsl);
    let mut bad = ex.clone();
    bad.isa_data.truncate(48);
    assert!(super::super::translate(&bad).is_err());
    let mut bad = ex.clone();
    bad.isa[8..12].copy_from_slice(&0x8f0a810au32.to_le_bytes()); // scale by 2, not 4
    bad.isa_data[..36].copy_from_slice(&bad.isa);
    assert!(super::super::translate(&bad).is_err());
}
