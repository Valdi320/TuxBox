use super::*;

fn insn(mnemonic: &str, enc: Enc, dst: Vec<Opnd>, src: Vec<Opnd>) -> Insn {
    Insn { offset: 0, dwords: 1, enc, op: 0, raw: vec![0], mnemonic: mnemonic.into(), dst, src,
        aux: Default::default() }
}

/// `nop` padding, then GETPC s[0:1]; ADD s0, lo; ADDC s1, hi; SETPC s[0:1] at
/// `at`, and a `nop` at every later offset up to `len` dwords.
fn long_jump(at: usize, low: Opnd, high: i64, len: usize) -> Vec<Insn> {
    let nop = || insn("s_nop", Enc::Sopp, vec![], vec![]);
    let mut ops: Vec<Insn> = (0..at).map(|_| nop()).collect();
    ops.push(insn("s_getpc_b64", Enc::Sop1, vec![Opnd::Sgpr(0)], vec![Opnd::Sgpr(0)]));
    ops.push(insn("s_add_u32", Enc::Sop2, vec![Opnd::Sgpr(0)], vec![Opnd::Sgpr(0), low]));
    ops.push(insn("s_addc_u32", Enc::Sop2, vec![Opnd::Sgpr(1)], vec![Opnd::Sgpr(1), Opnd::ConstInt(high)]));
    ops.push(insn("s_setpc_b64", Enc::Sop1, vec![Opnd::Sgpr(0)], vec![Opnd::Sgpr(0)]));
    while ops.len() < len {
        ops.push(nop());
    }
    for (i, op) in ops.iter_mut().enumerate() {
        op.offset = i * 4;
    }
    ops
}

fn branch_target(op: &Insn) -> Option<i64> {
    (op.enc == Enc::Sopp && op.op == 2).then(|| op.offset as i64 + 4 + 4 * op.aux.imm)
}

#[test]
fn long_jumps_with_a_carried_high_word_become_branches() {
    // GETPC at 0x0 returns 0x4; +0x20 lands on dword 9.
    let mut ops = long_jump(0, Opnd::Literal(0x20), 0, 12);
    lower_local_pc_transfers(&mut ops).unwrap();
    assert_eq!(branch_target(&ops[3]), Some(0x24));
    // Backward: GETPC at 0x20 returns 0x24; -0x24 (high word -1) lands on dword 0.
    let mut ops = long_jump(8, Opnd::Literal(0u32.wrapping_sub(0x24)), -1, 14);
    lower_local_pc_transfers(&mut ops).unwrap();
    assert_eq!(branch_target(&ops[11]), Some(0));
}

#[test]
fn unproven_long_jumps_stay_indirect() {
    // A target outside the program, or a high word that is not a sign or zero
    // extension, leaves the transfer untouched.
    for (low, high) in [(Opnd::Literal(0x400), 0), (Opnd::Literal(0x20), 1)] {
        let mut ops = long_jump(0, low, high, 12);
        lower_local_pc_transfers(&mut ops).unwrap();
        assert_eq!(ops[3].enc, Enc::Sop1);
        assert_eq!(ops[3].mnemonic, "s_setpc_b64");
    }
}

/// The waterfall loop of a ProjectScarlett wave64 compute program: each lane
/// reads v5 at the lane its v8 names.
fn waterfall_words(after: &[u32]) -> Vec<Insn> {
    let mut words = vec![
        0xbe86_047e,              // s_mov_b64 s[6:7], exec
        0x7e20_0508,              // loop: v_readfirstlane_b32 s16, v8
        0x7da4_1010,              // v_cmpx_eq_u32 vcc, s16, v8
        0xd760_0010, 0x0000_2105, // v_readlane_b32 s16, v5, s16
        0x7e16_0210,              // v_mov_b32 v11, s16
        0xbe86_3a06,              // s_andn2_wrexec_b64 s[6:7], s[6:7]
        0xbf89_fff9,              // s_cbranch_execnz loop
        0xbefe_0404,              // s_mov_b64 exec, s[4:5]
    ];
    words.extend_from_slice(after);
    words.push(0xbf81_0000); // s_endpgm
    disassemble(&words.iter().flat_map(|w| w.to_le_bytes()).collect::<Vec<_>>())
}

#[test]
fn waterfall_lane_reads_become_one_group_gather() {
    let mut ops = waterfall_words(&[]);
    assert_eq!(lower_waterfall_gathers(&mut ops), 1);
    assert_eq!(ops[1].mnemonic, GROUP_GATHER);
    assert_eq!(ops[1].src, [Opnd::Vgpr(5), Opnd::Vgpr(8)]);
    assert_eq!(ops[1].dst, [Opnd::Vgpr(11)]);
    assert!(ops[2..7].iter().all(|op| op.mnemonic == "v_nop"), "{:?}", ops.iter().map(|o| &o.mnemonic).collect::<Vec<_>>());
    assert_eq!(ops[7].mnemonic, "s_mov_b64");
}

#[test]
fn waterfall_scalar_leftovers_must_be_dead() {
    // s_add_u32 s1, s16, 1 reads the last index the loop left in s16.
    let mut ops = waterfall_words(&[0x8001_8110]);
    assert_eq!(lower_waterfall_gathers(&mut ops), 0);
    // A write first makes it dead: s_mov_b32 s16, 0 then the same read.
    let mut ops = waterfall_words(&[0xbe90_0380, 0x8001_8110]);
    assert_eq!(lower_waterfall_gathers(&mut ops), 1);
}

#[test]
fn mixed_spill_proof_reports_reason_without_changing_acceptance() {
    let words: [u32; 13] = [0x7f000287, 0x7e080380, 0xbe8c0393,
                 0xd7610080, 0x0201000c, 0xd760000d, 0x02010180,
                 0x7f000297, 0x7e0a0380, 0x7e0c020d,
                 0xf800080f, 0x80060504, 0xbf810000];
    let decode = |words: &[u32]| {
        let bytes: Vec<_> = words.iter().flat_map(|w| w.to_le_bytes()).collect();
        crate::rdna2::disassemble(&bytes)
    };
    assert_eq!(disjoint_spill_lifetimes(&decode(&words), 128), Ok(()));
    let mut bad = words.to_vec();
    bad.remove(7);
    assert_eq!(disjoint_spill_lifetimes(&decode(&bad), 128), Err("missing_full_redefinition"));
    let mut bad = words.to_vec();
    bad.insert(5, 0x7f000297);
    assert_eq!(disjoint_spill_lifetimes(&decode(&bad), 128), Err("overlapping_vector_access"));
    let mut bad = words.to_vec();
    bad.insert(7, 0xbf840001);
    assert_eq!(disjoint_spill_lifetimes(&decode(&bad), 128), Err("control_flow_or_special"));
}
