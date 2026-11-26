//! Lane reads, lane spills, local jumps and wave allocations.
use super::*;

/// Translator temporaries from this index up are lane-spill slots (declared up front).
pub(in crate::hlsl) const LANE_TEMP_BASE: u16 = 10_000;

/// SGPR spills through vector lanes.
///
/// When a shader runs out of scalar registers the console compiler parks scalars (observed:
/// texture/sampler descriptor dwords) in individual lanes of an otherwise unused VGPR with
/// `v_writelane_b32 vN, sX, <lane>` and brings them back with `v_readlane_b32 sY, vN, <lane>`
/// (VOP3 0x361 / 0x360; fixture `fortnite_lobby_desc.dxbc`). The translator models one lane
/// per VGPR, so each (VGPR, constant lane) slot is lowered to a function-scope translator
/// temporary and the two instructions become plain `s_mov_b32`. The descriptor dataflow
/// tracks those temporaries like registers (keys 1000+), so descriptors survive a spill.
///
/// Refused (explicit error): a non-constant lane index, a read of a slot that was never
/// written, or a spill VGPR whose ordinary vector lifetime cannot be separated.
/// A read of one lane of the caller's 64-lane block, exchanged like `ds_swizzle_b32`.
pub(in crate::hlsl) const GROUP_READ: &str = "tb_group_readlane";

/// Each lane's read of one lane of its 64-lane block, the lane chosen per lane
/// (see `lower_waterfall_gathers`), exchanged like a group read.
pub(in crate::hlsl) const GROUP_GATHER: &str = "tb_group_gather";

pub(in crate::hlsl) fn exchanges(i: &Insn) -> bool {
    i.mnemonic == "ds_swizzle_b32" || i.mnemonic == GROUP_READ || i.mnemonic == GROUP_GATHER
}

/// Waterfall loops in wave64 compute programs. The console compiler reads a
/// VGPR at a lane that differs per lane by serving one index value per pass:
///
/// ```text
///   loop: v_readfirstlane_b32 sL, vI
///         v_cmpx_eq_u32 vcc, sL, vI          ; EXEC: the lanes with that index
///         v_readlane_b32 sD, vS, sL          ; one or more reads at that lane,
///         v_mov_b32 vX, sD                   ;   each copied to a VGPR
///         s_andn2_wrexec_b64 sM, sM          ; retire them
///         s_cbranch_execnz loop
///         s_mov_b64 exec, sR                 ; restore EXEC
/// ```
///
/// Every lane active on entry ends with vX = vS[vI]. Each read becomes one
/// group gather at the loop head, and the rest of the loop becomes no-ops, so
/// the exchange runs once. The loop's scalar leftovers (sL, sD, VCC, sM) must be
/// dead afterwards; nothing else may enter or leave the loop.
pub(in crate::hlsl) fn lower_waterfall_gathers(insns: &mut [Insn]) -> usize {
    use std::collections::{BTreeMap, BTreeSet};
    let targets: BTreeSet<i64> = insns.iter()
        .filter(|i| i.enc == Enc::Sopp && Emitter::branch_cond(i.op).is_some())
        .map(|i| i.offset as i64 + 4 + 4 * i.aux.imm)
        .collect();
    let loops: BTreeMap<usize, Waterfall> = (0..insns.len())
        .filter_map(|head| waterfall(insns, head, &targets).map(|w| (head, w)))
        .collect();
    let accepted: Vec<(usize, Waterfall)> = loops.iter()
        .filter(|(_, w)| dead_after(insns, w, &loops))
        .map(|(&head, w)| (head, w.clone()))
        .collect();
    for (head, w) in &accepted {
        for slot in *head..=w.branch {
            let op = &mut insns[slot];
            op.mnemonic = "v_nop".into();
            op.enc = Enc::Vop1;
            op.src.clear();
            op.dst = vec![Opnd::Vgpr(0)];
        }
        for (k, &(source, dest)) in w.reads.iter().enumerate() {
            let op = &mut insns[head + k];
            op.mnemonic = GROUP_GATHER.into();
            op.src = vec![Opnd::Vgpr(source), Opnd::Vgpr(w.index)];
            op.dst = vec![Opnd::Vgpr(dest)];
        }
    }
    accepted.len()
}

#[derive(Clone)]
struct Waterfall {
    index: u8,
    reads: Vec<(u8, u8)>,
    /// The scalar registers the loop leaves behind.
    scalars: Vec<Opnd>,
    branch: usize,
}

/// A waterfall loop starting at `head`, by shape alone.
fn waterfall(insns: &[Insn], head: usize, targets: &std::collections::BTreeSet<i64>) -> Option<Waterfall> {
    let first = &insns[head];
    let (Some(&Opnd::Sgpr(lane)), Some(&Opnd::Vgpr(index))) = (first.dst.first(), first.src.first()) else { return None };
    if first.mnemonic != "v_readfirstlane_b32" || lane > 105 { return None; }
    let cmp = insns.get(head + 1)?;
    if cmp.mnemonic != "v_cmpx_eq_u32" || cmp.src[..] != [Opnd::Sgpr(lane), Opnd::Vgpr(index)] { return None; }
    let mut k = head + 2;
    let mut scalars = vec![Opnd::Sgpr(lane), Opnd::VccLo, Opnd::VccHi];
    let mut reads = Vec::new();
    while insns.get(k)?.mnemonic == "v_readlane_b32" {
        let read = &insns[k];
        let copy = insns.get(k + 1)?;
        // READLANE's encoded destination names an SGPR. It may be the index
        // register itself: the next pass reloads that.
        let (Some(&(Opnd::Vgpr(d) | Opnd::Sgpr(d))), Some(&Opnd::Vgpr(source))) = (read.dst.first(), read.src.first()) else { return None };
        if read.src.get(1) != Some(&Opnd::Sgpr(lane)) || d > 105
            || copy.mnemonic != "v_mov_b32" || copy.enc != Enc::Vop1 || copy.src[..] != [Opnd::Sgpr(d)]
        {
            return None;
        }
        let Some(&Opnd::Vgpr(dest)) = copy.dst.first() else { return None };
        // A copy may not feed a later read of the same pass, nor clobber the index.
        if dest == index || reads.iter().any(|&(_, earlier)| earlier == source) { return None; }
        reads.push((source, dest));
        scalars.push(Opnd::Sgpr(d));
        k += 2;
    }
    if reads.is_empty() { return None; }
    let (retire, branch, restore) = (insns.get(k)?, insns.get(k + 1)?, insns.get(k + 2)?);
    let Some(&Opnd::Sgpr(mask)) = retire.dst.first() else { return None };
    if retire.mnemonic != "s_andn2_wrexec_b64" || retire.src[..] != [Opnd::Sgpr(mask)] || mask > 104
        || branch.mnemonic != "s_cbranch_execnz"
        || branch.offset as i64 + 4 + 4 * branch.aux.imm != first.offset as i64
        || restore.mnemonic != "s_mov_b64" || restore.dst[..] != [Opnd::ExecLo]
        || !matches!(restore.src[..], [Opnd::Sgpr(r)] if r != mask && r != mask + 1)
    {
        return None;
    }
    scalars.extend([Opnd::Sgpr(mask), Opnd::Sgpr(mask + 1)]);
    // Only the back edge enters the loop; nothing inside branches out.
    let inner = (first.offset as i64 + 1)..=(branch.offset as i64);
    if targets.iter().any(|t| inner.contains(t)) { return None; }
    Some(Waterfall { index, reads, scalars, branch: k + 1 })
}

/// Whether the loop's scalar results (and the SCC its retire sets) are
/// overwritten before any later read, implicit VCC and SCC reads included.
/// Another waterfall loop overwrites its own registers before reading them.
fn dead_after(
    insns: &[Insn],
    w: &Waterfall,
    loops: &std::collections::BTreeMap<usize, Waterfall>,
) -> bool {
    let mut live = w.scalars.clone();
    let mut scc = true;
    let mut at = w.branch + 2; // after the EXEC restore
    while let Some(op) = insns.get(at) {
        if let Some(next) = loops.get(&at) {
            // It sets SCC and VCC, and its index register first.
            let lane = next.scalars[0];
            live.retain(|o| *o != lane && !matches!(o, Opnd::VccLo | Opnd::VccHi));
            scc = false;
            if live.is_empty() { return true; }
            at = next.branch + 2;
            continue;
        }
        let m = op.mnemonic.as_str();
        // A join only adds paths that never ran the loop; a branch would need
        // both of its successors followed, so the scan stops there.
        if op.enc == Enc::Sopp && Emitter::branch_cond(op.op).is_some() {
            return false;
        }
        // VOP2 forms read VCC implicitly; a VOP3 form names its mask.
        let reads_vcc = op.enc == Enc::Vop2 && (m.starts_with("v_cndmask") || m.contains("_co_ci") || m.contains("_subb") || m.contains("_addc"));
        if op.src.iter().any(|o| live.contains(o)) || reads_vcc && live.contains(&Opnd::VccLo) {
            return false;
        }
        if scc && ["s_cselect", "s_addc", "s_subb", "s_cmov", "s_cbranch_scc"].iter().any(|p| m.starts_with(p)) {
            return false;
        }
        if op.enc == Enc::Sopc || op.enc == Enc::Sop2 && ["s_add", "s_sub", "s_and", "s_or", "s_xor", "s_lshl", "s_lshr", "s_min", "s_max"].iter().any(|p| m.starts_with(p)) {
            scc = false;
        }
        // A 64-bit or VCC write names only its low register.
        let pair = m.ends_with("_b64") || m.ends_with("_u64") || m.ends_with("_i64") || op.enc == Enc::Vopc;
        let written = |o: &Opnd| op.dst.contains(o) || pair && match *o {
            Opnd::VccHi => op.dst.contains(&Opnd::VccLo),
            Opnd::Sgpr(n) => n > 0 && op.dst.contains(&Opnd::Sgpr(n - 1)),
            _ => false,
        };
        live.retain(|o| !written(o));
        if live.is_empty() && !scc || m == "s_endpgm" { return true; }
        at += 1;
    }
    true
}

/// A pixel is a wave of its own, so the first lane of a mask that covers it is its own.
/// `s_ff1` names that lane for `v_readlane`; anything else that uses the lane is left alone.
pub(in crate::hlsl) fn lower_first_lane_reads(insns: &mut [Insn]) {
    for i in 0..insns.len() {
        if insns[i].mnemonic != "v_readlane_b32" { continue; }
        let Some(&Opnd::Sgpr(lane)) = insns[i].src.get(1) else { continue };
        let Some(first) = (i.saturating_sub(64)..i).rev()
            .find(|&j| insns[j].dst.contains(&Opnd::Sgpr(lane)) || insns[j].enc == Enc::Sopp && insns[j].mnemonic.contains("branch"))
        else { continue };
        if !insns[first].mnemonic.starts_with("s_ff1_i32_b") { continue; }
        // The destination field names an SGPR (decoded generically as a VGPR).
        let Some(&(Opnd::Vgpr(result) | Opnd::Sgpr(result))) = insns[i].dst.first() else { continue };
        let reads = |j: usize| j != i && insns[j].src.contains(&Opnd::Sgpr(lane));
        if (first + 1..i).any(reads) { continue; }
        if result != lane {
            let end = (i + 1..insns.len()).find(|&j| insns[j].dst.contains(&Opnd::Sgpr(lane)) || insns[j].mnemonic.contains("branch"));
            if end.is_none_or(|end| (i + 1..=end).any(reads) || insns[end].mnemonic.contains("branch")) { continue; }
        }
        insns[first].mnemonic = "s_mov_b32".into();
        insns[first].op = 3;
        insns[first].src = vec![Opnd::ConstInt(0)];
        let ins = &mut insns[i];
        ins.enc = Enc::Vop1;
        ins.op = 2;
        ins.mnemonic = "v_readfirstlane_b32".into();
        ins.dst = vec![Opnd::Sgpr(result)];
        ins.src.truncate(1);
    }
}

/// Admit a mixed spill/vector register only in a straight-line, fixed-EXEC
/// program whose ordinary operands have exact, single-register footprints.
/// In particular, a vector read after the spill requires a new whole-word
/// definition, and no vector definition can overlap the spill lifetime.
/// Other encodings (including implicit wide operands) retain the refusal.
fn disjoint_spill_lifetimes(insns: &[Insn], v: u8) -> bool {
    let lane = |i: &Insn| i.enc == Enc::Vop3 && matches!(i.op, 0x360 | 0x361);
    let touches = |i: &Insn| i.dst.contains(&Opnd::Vgpr(v)) || i.src.contains(&Opnd::Vgpr(v));
    let spill = |i: &Insn| lane(i) && (if i.op == 0x361 {
        i.dst.first() == Some(&Opnd::Vgpr(v))
    } else { i.src.first() == Some(&Opnd::Vgpr(v)) });
    let Some(first) = insns.iter().position(spill) else { return false; };
    let Some(last) = insns.iter().rposition(spill) else { return false; };
    let mut redefined = false;
    for (n, i) in insns.iter().enumerate() {
        if lane(i) {
            // A writelane must have a scalar value; a readlane only reads
            // the named vector, and its encoded destination is scalar.
            if i.op == 0x361 && !matches!(i.src.first(), Some(Opnd::Sgpr(0..=105) | Opnd::ConstInt(_) | Opnd::Literal(_))) {
                return false;
            }
            if i.op == 0x360 && !matches!(i.dst.as_slice(), [Opnd::Vgpr(0..=105) | Opnd::Sgpr(0..=105)]) {
                return false;
            }
            continue;
        }
        let plain_move = i.enc == Enc::Vop1 && i.mnemonic == "v_mov_b32" && i.raw.len() == 1
            && matches!(i.dst.as_slice(), [Opnd::Vgpr(_)]) && i.src.len() == 1;
        let scalar_move = i.enc == Enc::Sop1 && i.mnemonic == "s_mov_b32"
            && matches!(i.dst.as_slice(), [Opnd::Sgpr(0..=105)])
            && !i.src.iter().any(|o| matches!(o, Opnd::Vgpr(_)));
        let export = i.enc == Enc::Exp;
        let nop = matches!(i.mnemonic.as_str(), "v_nop" | "s_nop" | "s_waitcnt" | "s_endpgm");
        if !(plain_move || scalar_move || export || nop) { return false; }
        if nop || !touches(i) { continue; }
        if n < first { continue; }
        if n <= last { return false; }
        if i.src.contains(&Opnd::Vgpr(v)) && !redefined { return false; }
        if plain_move && i.dst == [Opnd::Vgpr(v)] { redefined = true; }
    }
    true
}

pub(in crate::hlsl) fn lower_lane_spills(insns: &mut [Insn], wave32: bool, group_reads: bool) -> Result<(), String> {
    const READLANE: u32 = 0x360;
    const WRITELANE: u32 = 0x361;
    let is_lane = |i: &Insn| i.enc == Enc::Vop3 && (i.op == READLANE || i.op == WRITELANE);
    if !insns.iter().any(is_lane) {
        return Ok(());
    }
    let mut slots: BTreeMap<(u8, i64), Opnd> = BTreeMap::new();
    let mut spill_vregs: BTreeSet<u8> = BTreeSet::new();
    let written_vregs: BTreeSet<_> = insns.iter()
        .filter(|ins| wave32 && ins.enc == Enc::Vop3 && ins.op == WRITELANE)
        .filter_map(|ins| match ins.dst.first() {
            Some(Opnd::Vgpr(v)) => Some(*v),
            _ => None,
        })
        .collect();
    let any_written: BTreeSet<_> = insns.iter()
        .filter(|ins| ins.enc == Enc::Vop3 && ins.op == WRITELANE)
        .filter_map(|ins| match ins.dst.first() {
            Some(Opnd::Vgpr(v)) => Some(*v),
            _ => None,
        })
        .collect();
    let disjoint: BTreeSet<_> = any_written.iter().copied()
        .filter(|&v| insns.iter().any(|i| !is_lane(i) && i.mnemonic != "v_nop"
            && i.dst.iter().chain(&i.src).any(|o| *o == Opnd::Vgpr(v))))
        .filter(|&v| disjoint_spill_lifetimes(insns, v)).collect();
    for ins in insns.iter_mut() {
        if !is_lane(ins) {
            continue;
        }
        let at = ins.offset;
        if wave32 && ins.op == READLANE {
            if let ([Opnd::Vgpr(d) | Opnd::Sgpr(d)], [Opnd::Vgpr(v), lane, ..]) =
                (ins.dst.as_slice(), ins.src.as_slice())
            {
                if !written_vregs.contains(v) {
                    ins.dst = vec![Opnd::Sgpr(*d)];
                    ins.src = vec![Opnd::Vgpr(*v), *lane];
                    ins.enc = Enc::Sop1;
                    ins.mnemonic = ray_resolve::READ_LANE.into();
                    continue;
                }
            }
        }
        // A uniform Sgpr lane operand is only expressible through the whole-wave
        // group read; anything else stays refused. Spill areas (vregs written
        // by v_writelane) are still slotted per constant lane, so they remain
        // refused for dynamic lanes.
        if !wave32 && group_reads && ins.op == READLANE {
            let converted = match (ins.src.first(), ins.src.get(1), ins.dst.first()) {
                (Some(Opnd::Vgpr(v)), Some(Opnd::Sgpr(_)),
                 Some(Opnd::Vgpr(_)) | Some(Opnd::Sgpr(_)))
                    if !any_written.contains(v) =>
                {
                    let v = *v;
                    let d = match ins.dst.first() {
                        Some(Opnd::Vgpr(n)) | Some(Opnd::Sgpr(n)) => *n,
                        _ => unreachable!(),
                    };
                    ins.dst = vec![Opnd::Sgpr(d)];
                    ins.src = vec![Opnd::Vgpr(v), ins.src[1].clone()];
                    ins.enc = Enc::Sop1;
                    ins.mnemonic = GROUP_READ.into();
                    true
                }
                _ => false,
            };
            if converted {
                continue;
            }
        }
        let lane = match ins.src.get(1) {
            Some(Opnd::ConstInt(l)) if (0..64).contains(l) => *l,
            other => return Err(format!("lane op at {at:#x}: non-constant lane operand {other:?}")),
        };
        if ins.op == WRITELANE {
            let Some(Opnd::Vgpr(v)) = ins.dst.first().cloned() else {
                return Err(format!("v_writelane at {at:#x}: destination is not a VGPR"));
            };
            let next = slots.len();
            let slot = match slots.get(&(v, lane)) {
                Some(s) => *s,
                None => {
                    let s = Opnd::Temp(LANE_TEMP_BASE + next as u16);
                    slots.insert((v, lane), s);
                    s
                }
            };
            spill_vregs.insert(v);
            let value = ins.src.first().cloned().ok_or_else(|| format!("v_writelane at {at:#x}: no source"))?;
            ins.dst = vec![slot];
            ins.src = vec![value];
        } else {
            // The destination field of v_readlane names an SGPR (decoded generically as a VGPR).
            let d = match ins.dst.first() {
                Some(Opnd::Vgpr(n)) | Some(Opnd::Sgpr(n)) => *n,
                other => return Err(format!("v_readlane at {at:#x}: unexpected destination {other:?}")),
            };
            let Some(Opnd::Vgpr(v)) = ins.src.first().cloned() else {
                return Err(format!("v_readlane at {at:#x}: source is not a VGPR"));
            };
            if group_reads && !any_written.contains(&v) {
                ins.dst = vec![Opnd::Sgpr(d)];
                ins.src = vec![Opnd::Vgpr(v), Opnd::ConstInt(lane)];
                ins.enc = Enc::Sop1;
                ins.mnemonic = GROUP_READ.into();
                continue;
            }
            let Some(&slot) = slots.get(&(v, lane)) else {
                return Err(format!("v_readlane at {at:#x}: v{v} lane {lane} read before any v_writelane"));
            };
            ins.dst = vec![Opnd::Sgpr(d)];
            ins.src = vec![slot];
        }
        ins.enc = Enc::Sop1;
        ins.op = 0x03;
        ins.mnemonic = "s_mov_b32".into();
    }
    for ins in insns.iter() {
        if ins.mnemonic == "v_nop" {
            continue; // the two header words at the start of every program decode as v_nop
        }
        for o in ins.dst.iter().chain(ins.src.iter()) {
            if let Opnd::Vgpr(v) = o {
                if spill_vregs.contains(v) && !disjoint.contains(v) {
                    return Err(format!("v{v} is used both as a lane spill area and as a vector at {:#x}", ins.offset));
                }
            }
        }
    }
    Ok(())
}

/// Resolve local PC transfers when GETPC and a constant add form one basic block.
pub(in crate::hlsl) fn lower_local_pc_transfers(insns: &mut [Insn]) -> Result<(), String> {
    if !insns
        .iter()
        .any(|i| matches!(i.mnemonic.as_str(), "s_setpc_b64" | "s_swappc_b64"))
    {
        return Ok(());
    }
    let mut entries: BTreeSet<i64> = insns
        .iter()
        .filter(|i| i.enc == Enc::Sopp && Emitter::branch_cond(i.op).is_some())
        .map(|i| i.offset as i64 + 4 + 4 * i.aux.imm)
        .collect();
    let mut local = Vec::new();
    for (i, seq) in insns.windows(3).enumerate() {
        let [pc, add, jump] = seq else { unreachable!() };
        if !matches!(jump.mnemonic.as_str(), "s_setpc_b64" | "s_swappc_b64")
            || pc.mnemonic != "s_getpc_b64"
            || add.mnemonic != "s_add_u32"
            || add.dst.first() != pc.dst.first()
            || add.src.first() != pc.dst.first()
            || jump.src.first() != pc.dst.first()
        {
            continue;
        }
        let offset = match add.src.get(1) {
            Some(Opnd::ConstInt(k)) => *k,
            Some(Opnd::Literal(k)) => i64::from(*k as i32),
            _ => continue,
        };
        let target = pc.offset as i64 + 4 + offset;
        if !insns.iter().any(|x| x.offset as i64 == target) {
            return Err(format!(
                "PC transfer at {:#x} targets outside the decoded instructions",
                jump.offset
            ));
        }
        entries.insert(target);
        local.push((i, target, 2));
    }
    // Long jumps carry the add into the high word: GETPC, S_ADD_U32 lo, S_ADDC_U32 hi
    // with 0 or -1, SETPC. The 64-bit offset is (high << 32) + lo. A sequence whose
    // target or entry cannot be proven is left as it was.
    for (i, seq) in insns.windows(4).enumerate() {
        let [pc, add, carry, jump] = seq else { unreachable!() };
        let Some(&Opnd::Sgpr(base)) = pc.dst.first() else { continue };
        if !matches!(jump.mnemonic.as_str(), "s_setpc_b64" | "s_swappc_b64")
            || pc.mnemonic != "s_getpc_b64"
            || add.mnemonic != "s_add_u32"
            || carry.mnemonic != "s_addc_u32"
            || add.dst.first() != pc.dst.first()
            || add.src.first() != pc.dst.first()
            || carry.dst.first() != Some(&Opnd::Sgpr(base.wrapping_add(1)))
            || carry.src.first() != Some(&Opnd::Sgpr(base.wrapping_add(1)))
            || jump.src.first() != pc.dst.first()
        {
            continue;
        }
        let low = match add.src.get(1) {
            Some(Opnd::ConstInt(k)) => *k as u32,
            Some(Opnd::Literal(k)) => *k,
            _ => continue,
        };
        let high = match carry.src.get(1) {
            Some(Opnd::ConstInt(k @ (0 | -1))) => *k,
            _ => continue,
        };
        let target = pc.offset as i64 + 4 + (high << 32) + i64::from(low);
        let entered = [add, carry, jump].iter().any(|x| entries.contains(&(x.offset as i64)));
        if entered || !insns.iter().any(|x| x.offset as i64 == target) {
            continue;
        }
        entries.insert(target);
        local.push((i, target, 3));
    }
    for (i, target, last) in local {
        if (i + 1..=i + last).any(|j| entries.contains(&(insns[j].offset as i64))) {
            return Err("branch enters a local PC-transfer sequence after GETPC".into());
        }
        let jump = &mut insns[i + last];
        jump.enc = Enc::Sopp;
        jump.op = 2;
        jump.aux.imm = (target - jump.offset as i64 - 4) / 4;
        if jump.mnemonic == "s_setpc_b64" {
            jump.dst.clear();
        }
    }
    Ok(())
}

// These instruction sequences elect the lowest active lane to reserve a contiguous
// range, restore EXEC, broadcast the atomic result and add a per-lane prefix.
// The two register allocations differ. Match the complete regions before changing
// a saved EXEC mask from its usual per-invocation predicate into wave-uniform data.
pub(in crate::hlsl) fn mark_wave_allocations(insns: &mut [Insn], wave32_mask: bool, election: Option<ElectionLowering>) -> Result<bool, String> {
    const REGIONS: [&[u32]; 2] = [
        &[
        0xd7650007, 0x000100c1, 0xbe8e147e, 0xbe8c047e, 0xd7660007, 0x00020ec1,
        0xd482000e, 0x00020e0e, 0x7e0e0280, 0xbe8e240e, 0xbf880005, 0xbf800000,
        0xbe8c100c, 0x7e0e020c, 0xe0c84000, 0x80070700, 0xbefe040e, 0xd7650008,
        0x0001007e, 0xbf8c3f70, 0x7e180507, 0xd7660007, 0x0002107f, 0x4a0e0e0c
        ],
        &[
        0xd7650007, 0x000100c1, 0xbe83147e, 0xbe8e047e, 0xd7660007, 0x00020ec1,
        0xd4820012, 0x00020e03, 0x7e0e0280, 0xbe922412, 0xbf880005, 0xbf800000,
        0xbe83100e, 0x7e0e0203, 0xe0c84000, 0x80070700, 0xbefe0412, 0xd7650008,
        0x0001007e, 0xbf8c3f70, 0x7e060507, 0xd7660007, 0x0002107f, 0x4a0e0e03
        ],
    ];
    const COUNT: usize = 18;
    let snapshots = election.is_some() && predicate_snapshots(insns);
    let mut found = false;
    for i in 0..insns.len() {
        // Wave32 programs keep the previous lowering unless the election verifies.
        // Modes that model EXEC as a whole-wave mask already lower it directly.
        if !wave32_mask && insns[i].mnemonic == "s_ff1_i32_b32" && insns[i].src == [Opnd::ExecLo] {
            let mut trial = insns.to_vec();
            match verify_lane_election(&mut trial, i, election.is_some()) {
                Ok(()) => {
                    trial[i].mnemonic = "tb_first_active_lane".into();
                    insns.clone_from_slice(&trial);
                }
                Err(e) if std::env::var_os("TB_DFDEBUG").is_some() => eprintln!("{e}"),
                Err(_) => {}
            }
            continue;
        }
        if insns[i].mnemonic != "s_ff1_i32_b64" || insns[i].src != [Opnd::ExecLo] { continue; }
        let region = i.checked_sub(1).and_then(|first| insns.get(first..first + COUNT))
            .filter(|region| REGIONS.iter().any(|words| region.iter().flat_map(|op| op.raw.iter()).eq(words.iter())));
        let Some(region) = region else {
            if !snapshots {
                verify_lane_election(insns, i, election.is_some())?;
            }
            if election == Some(ElectionLowering::Shared64) { insns[i].mnemonic = "tb_first_exec_lane".into(); }
            continue;
        };
        let first = region[0].offset;
        let end = region[COUNT - 1].offset + 4;
        for branch in insns.iter().filter(|b| b.enc == Enc::Sopp && Emitter::branch_cond(b.op).is_some()) {
            let target = branch.offset as i64 + 4 + 4 * branch.aux.imm;
            if !(first..end).contains(&branch.offset) && target > first as i64 && target < end as i64 {
                return Err("external branch enters wave allocation".into());
            }
        }
        insns[i + 1].mnemonic = "tb_capture_allocation_mask".into();
        insns[i + 12].mnemonic = "tb_allocation_prefix_lo".into();
        insns[i + 15].mnemonic = "tb_allocation_prefix_hi".into();
        found = true;
    }
    Ok(found)
}

#[cfg(test)]
#[path = "../../tests/hlsl/rewrite_lanes.rs"]
mod tests;
