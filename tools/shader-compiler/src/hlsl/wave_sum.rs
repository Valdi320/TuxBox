//! Fixed-width DPP sums, extrema and prefix scans. Keep intermediate
//! register writes and the restored EXEC observable after each reduction.
use super::{Emitter, Enc, Insn, Opnd};

pub(super) const DPP_REDUCE: u32 = 0x7ff1;
pub(super) const READ_LANE: u32 = 0x7ff2;

// Reductions may have a completed vector load and a second predicate.
// The scalar AND intersects that predicate with the saved EXEC before the
// CNDMASK sets excluded lanes to the reduction's identity.
fn masked_prologue(insns: &[Insn], first: usize, identity: i64) -> Option<(usize, Opnd)> {
    let select = insns.get(first.checked_sub(1)?)?;
    let [Opnd::ConstInt(value), Opnd::Vgpr(_), mask @ Opnd::Sgpr(_)] = select.src[..] else {
        return None;
    };
    if value != identity {
        return None;
    }
    let mut mask_sources = None;
    for (index, op) in insns[..first - 1].iter().enumerate().rev() {
        match op.mnemonic.as_str() {
            "s_waitcnt" | "v_nop" => (),
            "s_and_b32" | "s_and_b64" if mask_sources.is_none() && op.dst == [mask] => {
                mask_sources = Some((op.mnemonic.as_str(), &op.src));
            }
            save @ ("s_or_saveexec_b32" | "s_or_saveexec_b64") => {
                let [saved @ Opnd::Sgpr(_)] = op.dst[..] else {
                    return None;
                };
                let matches = match mask_sources {
                    Some((and, sources)) => {
                        (save.ends_with("32") == and.ends_with("32")) && sources.contains(&saved)
                    }
                    None => saved == mask,
                };
                return matches.then_some((index, mask));
            }
            _ => return None,
        }
    }
    None
}

fn masked_identity(operation: &str) -> Option<i64> {
    match operation {
        "v_or_b32" | "v_max_u32" => Some(0),
        "v_and_b32" | "v_min_u32" => Some(-1),
        _ => None,
    }
}

pub(super) fn lower(insns: &mut [Insn], first: usize) -> Result<(u32, usize), &'static str> {
    let start = first.checked_sub(2).ok_or("missing reduction prologue")?;
    let tail = &insns[start..];
    let operation = &insns[first].mnemonic;
    let floating = matches!(operation.as_str(), "v_min_f32" | "v_max_f32");
    // The floating form inserts the neutral value between SAVEEXEC and CNDMASK.
    let identity = masked_identity(operation);
    let masked = identity
        .map(|neutral| {
            masked_prologue(insns, first, neutral).ok_or("unrecognized masked DPP prologue")
        })
        .transpose()?;
    let save = if let Some((index, _)) = masked {
        &insns[index]
    } else if floating {
        let prologue = start.checked_sub(1).ok_or("missing extrema prologue")?;
        &insns[prologue]
    } else {
        &tail[0]
    };
    let width = match save.mnemonic.as_str() {
        "s_or_saveexec_b32" => 32,
        "s_or_saveexec_b64" => 64,
        _ => return Err("unrecognized DPP reduction prologue"),
    };
    let last_read = if width == 32 { 8 } else { 10 };
    let block = tail
        .get(..last_read + 2)
        .ok_or("truncated wave reduction")?;
    let [Opnd::Sgpr(saved)] = save.dst[..] else {
        return Err("invalid saved EXEC register");
    };
    let [Opnd::Vgpr(acc)] = block[1].dst[..] else {
        return Err("invalid accumulator");
    };
    let neutral = if floating {
        let identity = if operation == "v_min_f32" {
            f32::INFINITY
        } else {
            f32::NEG_INFINITY
        };
        if tail[0].mnemonic != "v_mov_b32" || tail[0].src != [Opnd::Literal(identity.to_bits())] {
            return Err("unexpected extrema identity");
        }
        let [reg @ Opnd::Vgpr(_)] = tail[0].dst[..] else {
            return Err("invalid identity register");
        };
        reg
    } else {
        Opnd::ConstInt(identity.unwrap_or(0))
    };
    if save.src != [Opnd::ConstInt(-1)]
        || block[1].mnemonic != "v_cndmask_b32"
        || !matches!(block[1].src[..], [identity, Opnd::Vgpr(_), mask] if mask == masked.map_or(Opnd::Sgpr(saved), |(_, mask)| mask) && identity == neutral)
    {
        return Err("invalid masked reduction input");
    }

    // The first three DPP8 shuffles accumulate pairs, quads and octets.
    // DPP16 row_mirror combines the two octets in each 16-lane row.
    let controls = [0xde54c1, 0x936012, 0x000924, 0xff0940];
    for (i, (&ctrl, ins)) in controls.iter().zip(&block[2..6]).enumerate() {
        let selector = if i == 3 { 250 } else { 233 };
        if &ins.mnemonic != operation
            || ins.dst != [Opnd::Vgpr(acc)]
            || ins.src != [Opnd::Raw(selector), Opnd::Vgpr(acc)]
            || ins.raw.get(1) != Some(&(ctrl << 8 | u32::from(acc)))
        {
            return Err("unrecognized DPP reduction shuffle");
        }
    }
    let perm = &block[6];
    let [tmp @ Opnd::Vgpr(_)] = perm.dst[..] else {
        return Err("invalid cross-row destination");
    };
    let vr = Opnd::Vgpr(acc);
    if perm.mnemonic != "v_permlanex16_b32"
        || perm.src != [vr, Opnd::ConstInt(0), Opnd::ConstInt(0)]
        || &block[7].mnemonic != operation
        || block[7].dst != [vr]
        || block[7].src != [tmp, vr]
    {
        return Err("unrecognized cross-row add");
    }
    let lane_read = |ins: &Insn, lane| match ins.dst[..] {
        [Opnd::Vgpr(d @ 0..=105)]
            if ins.mnemonic == "v_readlane_b32"
                && ins.src.first() == Some(&vr)
                && ins.src.get(1) == Some(&Opnd::ConstInt(lane))
                && ins.raw.as_slice()
                    == [
                        0xd760_0000 | u32::from(d),
                        ((128 + lane as u32) << 9) | 256 | u32::from(acc),
                    ] =>
        {
            Ok(d)
        }
        _ => Err("invalid integer sum lane read"),
    };
    if width == 64 {
        let high = lane_read(&block[8], 32)?;
        if &block[9].mnemonic != operation
            || block[9].dst != [vr]
            || block[9].src != [Opnd::Sgpr(high), vr]
        {
            return Err("missing upper-half add");
        }
    }
    lane_read(&block[last_read], 0)?;
    let restore = &block[last_read + 1];
    if restore.mnemonic
        != (if width == 32 {
            "s_mov_b32"
        } else {
            "s_mov_b64"
        })
        || restore.dst != [Opnd::ExecLo]
        || restore.src != [Opnd::Sgpr(saved)]
    {
        return Err("integer sum does not restore EXEC");
    }

    for (step, ins) in insns[first..first + 4].iter_mut().enumerate() {
        ins.op = DPP_REDUCE;
        ins.src = vec![vr];
        ins.aux.imm = step as i64;
    }
    // READLANE's encoded destination names an SGPR. Mark just the actual
    // broadcasts here so the independent scalar lane-spill pass ignores them.
    for i in (8..=last_read).step_by(2) {
        let ins = &mut insns[start + i];
        let Opnd::Vgpr(dst) = ins.dst[0] else {
            return Err("invalid lane result");
        };
        ins.op = READ_LANE;
        ins.dst[0] = Opnd::Sgpr(dst);
        ins.src.truncate(2);
    }
    Ok((width, start + last_read + 2))
}

/// A forward SCC branch can skip a whole reduction without divergence when
/// its comparison uses only immediates or the immediately preceding verified
/// reduction's scalar result. Require the entire proof window to be free of
/// other control-flow edges: a lane skipping the comparison, entering its
/// body, or revisiting it on a loop could otherwise break uniformity.
fn uniform_forward_guard(
    insns: &[Insn],
    branch_index: usize,
    reduction_end: usize,
    verified_reads: &[usize],
) -> bool {
    let branch = &insns[branch_index];
    let pc = branch.offset as i64;
    let target = pc + 4 + 4 * branch.aux.imm;
    if !matches!(branch.op, 4 | 5) || target <= reduction_end as i64 {
        return false;
    }
    let Some(compare_index) = branch_index.checked_sub(1) else { return false };
    let compare = &insns[compare_index];
    if compare.enc != Enc::Sopc
        || !matches!(compare.mnemonic.as_str(),
            "s_cmp_eq_u32" | "s_cmp_lg_u32" | "s_cmp_gt_u32" | "s_cmp_ge_u32"
            | "s_cmp_lt_u32" | "s_cmp_le_u32" | "s_cmp_eq_i32" | "s_cmp_lg_i32"
            | "s_cmp_gt_i32" | "s_cmp_ge_i32" | "s_cmp_lt_i32" | "s_cmp_le_i32")
        || compare.src.len() != 2
    {
        return false;
    }

    // A final READ_LANE of an already checked reduction broadcasts the same
    // scalar to every host invocation. No intervening writes are permitted.
    let previous_read = verified_reads.last().copied().filter(|&read| {
        read + 1 < compare_index
            && insns[read].op == READ_LANE
            && insns[read].enc == Enc::Vop3
            && insns[read + 1].mnemonic.starts_with("s_mov_b")
            && insns[read + 1].dst == [Opnd::ExecLo]
            && insns[read + 2..compare_index].iter().all(|i| {
                matches!(i.mnemonic.as_str(), "s_nop" | "s_waitcnt")
            })
    });
    let mut proof_start = compare.offset as i64;
    for src in &compare.src {
        match src {
            Opnd::ConstInt(_) | Opnd::Literal(_) => (),
            Opnd::Sgpr(_) => {
                let Some(read) = previous_read else { return false };
                if insns[read].dst.as_slice() != [*src] {
                    return false;
                }
                proof_start = insns[read].offset as i64;
            }
            _ => return false,
        }
    }

    for (index, op) in insns.iter().enumerate() {
        if index == branch_index {
            continue;
        }
        // Indirect edges do not have a statically bounded target.
        if matches!(op.mnemonic.as_str(),
            "s_setpc_b64" | "s_swappc_b64" | "s_call_b64" | "s_rfe_b64" | "s_trap")
        {
            return false;
        }
        if op.enc == Enc::Sopp && Emitter::branch_cond(op.op).is_some() {
            let from = op.offset as i64;
            let to = from + 4 + 4 * op.aux.imm;
            if from.min(to) < target && from.max(to) > proof_start {
                return false;
            }
        } else if op.enc == Enc::Sopp && op.op == 1 && (op.offset as i64) < target {
            return false;
        }
    }
    true
}

/// The branch emitter predicates regions per host invocation. HLSL lane reads
/// require the source invocation to participate, including lane zero when guest
/// EXEC has masked it. A branch around or into the region can violate that.
pub(super) fn check_control_flow(insns: &[Insn], width: u32) -> Result<(), String> {
    let mut verified_reads = Vec::new();
    for (first, ins) in insns.iter().enumerate() {
        if ins.op != DPP_REDUCE || ins.aux.imm != 0 {
            continue;
        }
        let prologue_len = if matches!(ins.mnemonic.as_str(), "v_min_f32" | "v_max_f32") {
            3
        } else {
            2
        };
        let start = if let Some(neutral) = masked_identity(&ins.mnemonic) {
            masked_prologue(insns, first, neutral)
                .ok_or("unrecognized masked DPP prologue")?
                .0
        } else {
            first - prologue_len
        };
        let start = insns[start].offset as i64;
        let last_read = first + if width == 32 { 6 } else { 8 };
        // The readlane is the last collective; the following restore is
        // part of the protected region too.
        let restore = first + if width == 32 { 7 } else { 9 };
        let end = insns[restore].offset as i64;
        for (branch_index, branch) in insns.iter().enumerate().filter(|(_, i)| i.enc == Enc::Sopp) {
            let pc = branch.offset as i64;
            if branch.op == 1 && pc < start {
                return Err(format!("wave reduction at {start:#x} after an early exit"));
            }
            if Emitter::branch_cond(branch.op).is_some() {
                let target = pc + 4 + 4 * branch.aux.imm;
                if ((pc < start && target > start) || (pc > end && target <= end))
                    && !(pc < start && uniform_forward_guard(insns, branch_index, end as usize, &verified_reads))
                {
                    return Err(format!(
                        "wave reduction at {start:#x} may execute in divergent control flow"
                    ));
                }
            }
        }
        verified_reads.push(last_read);
    }
    Ok(())
}

// Only a complete, single guest wave64 may use group rendezvous. Every
// branch must be proven group-uniform and every collective must be one of the
// exact recognized integer reductions. All other programs keep the native path.
pub(super) fn portable_single_wave(insns: &[Insn]) -> bool {
    let mut covered = vec![false; insns.len()];
    let mut masks = Vec::new();
    let mut reads = Vec::new();
    for (first, i) in insns.iter().enumerate() {
        if i.op != DPP_REDUCE || i.aux.imm != 0 {
            continue;
        }
        let Some(start) = first.checked_sub(2) else { return false };
        let Some(block) = insns.get(start..start + 12) else { return false };
        let [Opnd::Sgpr(saved)] = block[0].dst[..] else { return false };
        if saved >= 105 || saved % 2 != 0 || block[0].mnemonic != "s_or_saveexec_b64"
            || block[0].src != [Opnd::ConstInt(-1)]
            || block[1].mnemonic != "v_cndmask_b32"
            || !block[2..6].iter().enumerate().all(|(step, i)| {
                i.op == DPP_REDUCE && i.aux.imm == step as i64 && i.mnemonic == "v_add_nc_u32"
            })
            || block[6].mnemonic != "v_permlanex16_b32"
            || block[6].raw.first().is_none_or(|w| w & 0x7800 != 0)
            || block[7].mnemonic != "v_add_nc_u32"
            || block[9].mnemonic != "v_add_nc_u32"
            || block[11].mnemonic != "s_mov_b64"
            || block[11].dst != [Opnd::ExecLo]
            || block[11].src != [Opnd::Sgpr(saved)]
            || covered[start..start + 12].iter().any(|&v| v)
        {
            return false;
        }
        for (idx, lane) in [(8, 32), (10, 0)] {
            let read = &block[idx];
            if read.op != READ_LANE || read.enc != Enc::Vop3
                || read.src.get(1) != Some(&Opnd::ConstInt(lane))
                || !matches!(read.dst[..], [Opnd::Sgpr(n)] if n != saved && n != saved + 1)
            {
                return false;
            }
        }
        masks.extend([saved, saved + 1]);
        reads.push(start + 10);
        covered[start..start + 12].fill(true);
    }
    if reads.is_empty() {
        return false;
    }
    for (idx, i) in insns.iter().enumerate() {
        if covered[idx] {
            continue;
        }
        // The generic emitter represents an EXEC bit per invocation, not a
        // full scalar mask. Do not let a saved mask escape the reduction.
        if masks.iter().any(|&n| super::reads_sgpr(i, n)) {
            return false;
        }
        if i.enc == Enc::Sopp {
            if Emitter::branch_cond(i.op).is_some() {
                let prior: Vec<_> = reads.iter().copied().filter(|&r| r < idx).collect();
                if !uniform_forward_guard(insns, idx, i.offset, &prior) {
                    return false;
                }
                continue;
            }
            if matches!(i.mnemonic.as_str(), "s_nop" | "s_waitcnt")
                || (i.mnemonic == "s_endpgm" && idx + 1 == insns.len())
            {
                continue;
            }
            return false;
        }
        // No other wave/quad instructions, EXEC transitions, or LDS accesses.
        // Keep a deliberately small whitelist of ordinary ALU and buffer ops.
        let lane_local = matches!(i.mnemonic.as_str(),
            "v_mov_b32" | "v_add_nc_u32" | "v_sub_nc_u32" | "v_subrev_nc_u32"
            | "v_and_b32" | "v_or_b32" | "v_xor_b32" | "v_lshlrev_b32"
            | "v_lshrrev_b32" | "v_mul_lo_u32" | "v_add_f32" | "v_mul_f32"
            | "s_mov_b32" | "s_movk_i32" | "s_add_u32" | "s_sub_u32"
            | "s_cmp_eq_u32" | "s_cmp_lg_u32" | "s_cmp_gt_u32" | "s_cmp_ge_u32"
            | "s_cmp_lt_u32" | "s_cmp_le_u32" | "s_cmp_eq_i32" | "s_cmp_lg_i32"
            | "s_cmp_gt_i32" | "s_cmp_ge_i32" | "s_cmp_lt_i32" | "s_cmp_le_i32")
            && i.src.iter().chain(&i.dst).all(|o| matches!(o, Opnd::Vgpr(_)
                | Opnd::Sgpr(_) | Opnd::ConstInt(_) | Opnd::ConstF32(_) | Opnd::Literal(_) | Opnd::Scc));
        let buffer = matches!(i.enc, Enc::Mubuf | Enc::Smem)
            && (i.mnemonic.starts_with("buffer_load_dword")
                || i.mnemonic.starts_with("buffer_store_dword")
                || i.mnemonic.starts_with("s_load_dword")
                || i.mnemonic.starts_with("s_buffer_load_dword"))
            && i.src.iter().chain(&i.dst).all(|o| matches!(o, Opnd::Vgpr(_)
                | Opnd::Sgpr(_) | Opnd::ConstInt(_) | Opnd::Literal(_)));
        if !lane_local && !buffer {
            return false;
        }
    }
    true
}

// A second barrier prevents a fast host subgroup from overwriting a slot
// before the other subgroup has consumed it. The verifier above ensures
// that every invocation reaches these barriers in the same order.
pub(super) const PORTABLE_DECLARATIONS: &str = r#"
groupshared uint tb_reduction_lanes[64];
uint tb_reduction_read(uint lane, uint value, uint source) {
    tb_reduction_lanes[lane] = value;
    GroupMemoryBarrierWithGroupSync();
    uint result = tb_reduction_lanes[source];
    GroupMemoryBarrierWithGroupSync();
    return result;
}
"#;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ShaderExtract, Stage};
    mod fixture {
        include!(concat!(env!("CARGO_MANIFEST_DIR"), "/examples/support/wave_guard_fixture.rs"));
    }

    fn insn(mnemonic: &str, enc: Enc, op: u32, dst: Vec<Opnd>, src: Vec<Opnd>) -> Insn {
        Insn {
            offset: 0,
            dwords: 1,
            enc,
            op,
            raw: vec![0],
            mnemonic: mnemonic.into(),
            dst,
            src,
            aux: Default::default(),
        }
    }

    fn nop() -> Insn {
        insn("s_nop", Enc::Sopp, 0, vec![], vec![])
    }

    fn branch(op: u32) -> Insn {
        insn("branch", Enc::Sopp, op, vec![], vec![])
    }

    fn compare(src: Vec<Opnd>) -> Insn {
        insn("s_cmp_eq_u32", Enc::Sopc, 6, vec![], src)
    }

    // Synthetic input to the control-flow pass, after the exact DPP
    // sequence has been recognized by lower(). Include both wave widths.
    fn reduction(width: u32, result: u8) -> Vec<Insn> {
        let mut ops = vec![nop(); if width == 32 { 10 } else { 12 }];
        ops[0] = insn("s_or_saveexec_b64", Enc::Sop1, 0,
                     vec![Opnd::Sgpr(0)], vec![Opnd::ConstInt(-1)]);
        ops[2] = insn("v_add_nc_u32", Enc::Vop2, DPP_REDUCE,
                     vec![Opnd::Vgpr(1)], vec![Opnd::Vgpr(1)]);
        let read = ops.len() - 2;
        ops[read] = insn("v_readlane_b32", Enc::Vop3, READ_LANE,
                         vec![Opnd::Sgpr(result)], vec![Opnd::Vgpr(1), Opnd::ConstInt(0)]);
        ops[read + 1] = insn("s_mov_b64", Enc::Sop1, 0,
                             vec![Opnd::ExecLo], vec![Opnd::Sgpr(0)]);
        ops
    }

    fn offsets(ops: &mut [Insn]) {
        for (i, op) in ops.iter_mut().enumerate() {
            op.offset = i * 4;
        }
    }

    fn edge(ops: &mut [Insn], from: usize, to: usize, op: u32) {
        ops[from] = branch(op);
        ops[from].aux.imm = to as i64 - from as i64 - 1;
        offsets(ops);
    }

    #[test]
    fn translates_two_native_reductions_with_a_uniform_scc_guard() {
        for width in [32, 64] {
            // GFX1030 instructions assembled with llvm-mc, with the four DPP
            // control words from the exact reduction recognized by lower().
            let (mut ex, mut words, cmp) = fixture::shader(width);
            let translated = super::super::translate(&ex).unwrap();
            assert_eq!(translated.required_wave_size, (width == 32).then_some(32));
            if width == 64 {
                assert!(translated.hlsl.contains("tb_reduction_read("));
                assert!(!translated.hlsl.contains("WaveReadLaneAt("));
                assert!(!translated.hlsl.contains("[WaveSize("));
            } else {
                assert!(translated.hlsl.contains("WaveReadLaneAt("));
            }
            // Replace the proven scalar with an unproven entry register.
            words[cmp] = 0xbf06_8008;
            ex.isa = words.iter().flat_map(|word| word.to_le_bytes()).collect();
            ex.isa_data = ex.isa.clone();
            let refusal = super::super::translate(&ex).err().unwrap();
            assert!(refusal.contains("divergent control flow"), "{refusal}");
        }
    }

    #[test]
    fn portable_wave64_requires_a_single_group_and_no_other_wave_operations() {
        let (mut ex, _, _) = fixture::shader(64);
        let mut insns = crate::rdna2::disassemble(&ex.isa);
        assert_eq!(super::super::lower_wave_reductions(&mut insns).unwrap(), Some(64));
        assert!(portable_single_wave(&insns));
        let compare = insns.iter().position(|i| i.mnemonic == "s_cmp_eq_u32").unwrap();
        let outside = compare + 2;
        let mut changed = insns.clone();
        changed[outside].mnemonic = "v_readfirstlane_b32".into();
        assert!(!portable_single_wave(&changed));
        changed = insns.clone();
        changed[outside].src[0] = Opnd::Sgpr(0); // saved EXEC must not escape
        assert!(!portable_single_wave(&changed));
        changed = insns.clone();
        changed[compare].src[0] = Opnd::Sgpr(8);
        assert!(!portable_single_wave(&changed));
        changed = insns.clone();
        changed[8].dst[0] = Opnd::Sgpr(0); // overwrites saved EXEC
        assert!(!portable_single_wave(&changed));

        ex.isa_preamble = [0, 128, 1, 1];
        let native = super::super::translate(&ex).unwrap();
        assert_eq!(native.required_wave_size, Some(64));
        assert!(native.hlsl.contains("[WaveSize(64)]"));
        assert!(!native.hlsl.contains("tb_reduction_read("));
    }

    #[test]
    fn uniform_literal_guard_must_skip_the_entire_reduction() {
        for width in [32, 64] {
            let mut ops = vec![compare(vec![Opnd::ConstInt(1), Opnd::Literal(0)]), branch(4)];
            ops.extend(reduction(width, 7));
            let after = ops.len();
            ops.push(nop());
            edge(&mut ops, 1, after, 4);
            assert!(check_control_flow(&ops, width).is_ok());
            // The compare may instead use a lane-dependent scalar.
            ops[0].src[0] = Opnd::Sgpr(9);
            assert!(check_control_flow(&ops, width).is_err());
            ops[0].src[0] = Opnd::ConstInt(1);
            edge(&mut ops, 1, after - 1, 4);
            assert!(check_control_flow(&ops, width).is_err());
            edge(&mut ops, 1, after, 8); // EXEC is a per-invocation mask
            assert!(check_control_flow(&ops, width).is_err());
        }
    }

    #[test]
    fn prior_reduction_result_is_uniform_only_without_intervening_writes() {
        for width in [32, 64] {
            let mut ops = reduction(width, 7);
            let spacer = ops.len();
            ops.push(nop());
            ops.push(compare(vec![Opnd::Sgpr(7), Opnd::ConstInt(0)]));
            let guard = ops.len();
            ops.push(branch(5));
            ops.extend(reduction(width, 8));
            let after = ops.len();
            ops.push(nop());
            edge(&mut ops, guard, after, 5);
            assert!(check_control_flow(&ops, width).is_ok());
            ops[spacer] = insn("s_mov_b32", Enc::Sop1, 0,
                               vec![Opnd::Sgpr(7)], vec![Opnd::Sgpr(9)]);
            offsets(&mut ops);
            assert!(check_control_flow(&ops, width).is_err());
            ops[spacer] = nop();
            offsets(&mut ops);
            ops[guard - 1].src[0] = Opnd::Sgpr(8);
            assert!(check_control_flow(&ops, width).is_err());
        }
    }

    #[test]
    fn alternate_entry_and_loop_edges_invalidate_uniform_guard() {
        let mut ops = vec![nop(), compare(vec![Opnd::ConstInt(0), Opnd::ConstInt(0)]), branch(5)];
        ops.extend(reduction(64, 7));
        let after = ops.len();
        ops.push(nop());
        ops.push(nop());
        edge(&mut ops, 2, after, 5);
        assert!(check_control_flow(&ops, 64).is_ok());
        // Another branch can enter after the comparison.
        edge(&mut ops, 0, 2, 8);
        assert!(check_control_flow(&ops, 64).is_err());
        // A branch landing at the comparison, however, reconverges before SCC.
        edge(&mut ops, 0, 1, 8);
        assert!(check_control_flow(&ops, 64).is_ok());
        edge(&mut ops, after + 1, 1, 8);
        assert!(check_control_flow(&ops, 64).is_err());
        ops[after + 1] = nop();
        ops[0] = insn("s_endpgm", Enc::Sopp, 1, vec![], vec![]);
        offsets(&mut ops);
        assert!(check_control_flow(&ops, 64).is_err());
    }
}
