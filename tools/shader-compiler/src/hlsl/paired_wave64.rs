//! Console wave64 compute on 32-lane hardware: each host thread runs two console lanes.
//!
//! Thread `n` of a host wave holds console lanes `n` and `n + 32`. Scalar registers
//! and lane masks keep their full console values, the same in every thread of the
//! wave, so branches and scalar instructions need no rewriting. Vector registers are
//! stored once per lane. A vector instruction is emitted once and run for each lane
//! with that lane's registers and mask bits, and a mask it produces is rebuilt from
//! all 64 results. No part of this needs a barrier.

use std::collections::BTreeSet;

use super::*;

/// Translate a wave64 compute program for hosts with 32-lane waves.
pub fn translate_paired_wave64(ex: &ShaderExtract) -> Result<Translated, String> {
    let threads = compute_group(ex).into_iter().try_fold(1u32, u32::checked_mul).unwrap_or(0);
    if threads == 0 || threads % 64 != 0 || threads > 2048 {
        return Err("paired wave64 needs whole console waves and at most 2048 threads".into());
    }
    let mut insns = disassemble(&ex.isa);
    lower_local_pc_transfers(&mut insns)?;
    let mut em = Emitter::new(ex, insns);
    em.paired = true;
    em.required_wave_size = Some(32);
    em.vreg_array = false;
    if em.insns.iter().any(|i| i.mnemonic.starts_with("v_movrel")) {
        return Err("paired wave64: relative vector registers".into());
    }
    if !em.resolve_packed(None)? {
        em.resolve_resources()?;
    }
    em.infer_tbuf_types();
    if em.packed.is_none() && !em.uav_uint.is_empty() {
        em.resolve_wide_image_atomics()?;
    }
    em.resolve_structured_strides();
    let live_in = em.live_in();
    em.live = live_in.clone();
    em.emit_body(false)?;
    if em.compute_derivatives {
        return Err("paired wave64: implicit derivatives".into());
    }
    let mut hlsl = em.finish(false, &[], None)?;
    hlsl.insert_str(0, &format!("// entry-live registers (read before written): {}\n", live_in.join(" ")));
    Ok(Translated {
        hlsl,
        entry: "main",
        profile: "cs_6_6",
        needs_barycentrics: false,
        packed_dot_product: false,
        typed_int64_atomics: !em.wide_visibility_uavs.is_empty() || em.packed.as_ref().is_some_and(|p| !p.wide_images.is_empty()),
        required_wave_size: Some(32),
        portable_compute_group: None,
        ps_input_interp: InputInterp::default(),
    })
}

/// A branch on a lane mask tests the whole console mask.
pub(super) fn branch_condition(op: u32) -> Option<&'static str> {
    Some(match op {
        6 => "(vcc_lo | vcc_hi) == 0u",
        7 => "(vcc_lo | vcc_hi) != 0u",
        8 => "(exec_lo | exec_hi) == 0u",
        9 => "(exec_lo | exec_hi) != 0u",
        _ => return None,
    })
}

/// The compute prologue: registers, the two lanes this thread runs, and the host group.
pub(super) fn prologue(em: &Emitter<'_>, header: &mut String, group_register: usize) {
    let [x, y, z] = compute_group(em.ex);
    let threads = x * y * z;
    let _ = writeln!(header, "[WaveSize(32)]\n[numthreads({}, 1, 1)]", threads / 2);
    let _ = writeln!(header, "void main(uint3 gid : SV_GroupID, uint3 gtid : SV_GroupThreadID) {{");
    let _ = writeln!(header, "    uint exec_lo = 0xffffffffu, exec_hi = 0xffffffffu, vcc_lo = 0u, vcc_hi = 0u, scc = 0u, m0 = 0u, nul = 0u;");
    em.heap_sampler_locals(header);
    let mut names = Vec::new();
    for register in &em.regs {
        if is_vector_register(register) {
            names.push(format!("{register}__0 = 0u, {register}__1 = 0u"));
        } else if register == "tb_fp_mode" {
            names.push(format!("{register} = 0xc0u"));
        } else {
            names.push(format!("{register} = 0u"));
        }
    }
    let _ = writeln!(header, "    uint {};", names.join(", "));
    // Full subgroups follow the thread index, so each run of 32 threads is one wave.
    let _ = writeln!(header, "    uint tb_pair = WaveGetLaneIndex(), tb_first = (gtid.x / 32u) * 64u;");
    for lane in 0..2 {
        let flat = format!("(tb_first + tb_pair + {}u)", 32 * lane);
        let _ = writeln!(header, "    v0__{lane} = {flat} % {x}u; v1__{lane} = ({flat} / {x}u) % {y}u; v2__{lane} = {flat} / {}u;", x * y);
    }
    let g = group_register;
    let _ = writeln!(header, "    s{} = gid.x; s{} = gid.y; s{} = gid.z;", g, g + 1, g + 2);
}

fn is_vector_register(name: &str) -> bool {
    name.len() > 1 && name.starts_with('v') && name[1..].bytes().all(|b| b.is_ascii_digit())
}

fn per_lane(encoding: Enc) -> bool {
    matches!(encoding, Enc::Vop1 | Enc::Vop2 | Enc::Vop3 | Enc::Vopc | Enc::Mubuf | Enc::Mtbuf
        | Enc::Mimg | Enc::Flat | Enc::Ds)
}

/// Identifiers in a piece of emitted code, ignoring comment lines.
fn identifiers(code: &str) -> impl Iterator<Item = (&str, &str)> {
    code.lines().filter(|line| !line.trim_start().starts_with("//")).flat_map(|line| {
        let mut found = Vec::new();
        let bytes = line.as_bytes();
        let mut at = 0;
        while at < bytes.len() {
            if bytes[at].is_ascii_alphabetic() || bytes[at] == b'_' {
                let start = at;
                while at < bytes.len() && (bytes[at].is_ascii_alphanumeric() || bytes[at] == b'_') {
                    at += 1;
                }
                // A member such as `.x` or a literal suffix is not a register.
                if start == 0 || (bytes[start - 1] != b'.' && !bytes[start - 1].is_ascii_digit()) {
                    found.push((&line[start..at], &line[at..]));
                }
            } else if bytes[at].is_ascii_digit() {
                while at < bytes.len() && bytes[at].is_ascii_alphanumeric() {
                    at += 1;
                }
            } else {
                at += 1;
            }
        }
        found
    })
}

/// Rename VGPR identifiers in a single lane's emitted arithmetic body.
/// The two lanes have disjoint VGPR storage, so reads, assignments and
/// in/out arguments can address the paired register directly. Keep the
/// existing lexical boundaries: do not touch comments, members or literals.
fn paired_vector_code(code: &str, lane: u32) -> String {
    let mut result = String::with_capacity(code.len());
    for line in code.split_inclusive('\n') {
        let body = line.trim_end_matches(['\r', '\n']);
        let mut copied = 0;
        for (word, after) in identifiers(body).filter(|(word, _)| is_vector_register(word)) {
            let end = body.len() - after.len();
            let start = end - word.len();
            result.push_str(&line[copied..start]);
            let _ = write!(result, "{word}__{lane}");
            copied = end;
        }
        result.push_str(&line[copied..]);
    }
    result
}

/// The two words of the lane mask a vector instruction writes through `operand`.
fn mask_words(operand: &Opnd) -> Option<[String; 2]> {
    match operand {
        Opnd::Sgpr(n) => Some([format!("s{n}"), format!("s{}", n + 1)]),
        Opnd::VccLo => Some(["vcc_lo".into(), "vcc_hi".into()]),
        Opnd::ExecLo => Some(["exec_lo".into(), "exec_hi".into()]),
        _ => None,
    }
}

/// `true` in the bit of `word_lo`/`word_hi` belonging to console lane `lane` (an expression).
fn lane_bit(low: &str, high: &str, lane: u32) -> String {
    let word = if lane == 0 { low } else { high };
    format!("(({word} >> tb_pair) & 1u)")
}

fn first_lane(destination: &str) -> [String; 1] {
    [format!("uint tb_leader = exec_lo != 0u ? firstbitlow(exec_lo) : (exec_hi != 0u ? 32u + firstbitlow(exec_hi) : 0u); {destination}")]
}

/// Read vector register or scalar `source` of console lane `tb_leader` into `destination`.
fn read_lane(em: &mut Emitter<'_>, destination: &Opnd, source: &Opnd, leader: &str) -> Result<(), String> {
    // The destination field of a lane read numbers a scalar register.
    let destination = &match *destination {
        Opnd::Vgpr(n @ 0..=105) => Opnd::Sgpr(n),
        Opnd::Vgpr(106) => Opnd::VccLo,
        Opnd::Vgpr(107) => Opnd::VccHi,
        Opnd::Vgpr(124) => Opnd::M0,
        Opnd::Vgpr(126) => Opnd::ExecLo,
        Opnd::Vgpr(127) => Opnd::ExecHi,
        Opnd::Vgpr(n) => return Err(format!("paired wave64: lane read into scalar code {n}")),
        other => other,
    };
    em.touch(destination);
    em.touch(source);
    let to = name(destination)?;
    let value = match source {
        Opnd::Vgpr(n) => format!("{to} = tb_leader < 32u ? WaveReadLaneAt(v{n}__0, tb_leader) : WaveReadLaneAt(v{n}__1, tb_leader - 32u);"),
        other => format!("{to} = {};", r(other)?),
    };
    let first = if leader.is_empty() { first_lane(&value)[0].clone() } else { format!("uint tb_leader = {leader}; {value}") };
    em.line(&format!("{{ {first} }}"));
    Ok(())
}

/// The temporary that stands for a source moved from another lane.
const MOVED: u16 = 60000;

/// DPP8, DPP16 row/quad controls, permlane16 and DS swizzle only
/// select within one 32-lane half. Read that half's VGPR directly; reading
/// both halves and choosing at run time needlessly doubles wave operations.
fn from_half(register: u32, prefix: &str, lane: u32, partner: &str) -> [String; 2] {
    [
        format!("uint tb_from{lane} = {partner};"),
        format!("uint {prefix}{lane} = WaveReadLaneAt(v{register}__{lane}, tb_from{lane} & 31u);"),
    ]
}

/// Whether the source in this same half is active.
fn source_active(lane: u32) -> String {
    let exec = if lane == 0 { "exec_lo" } else { "exec_hi" };
    format!("(({exec} >> (tb_from{lane} & 31u)) & 1u) != 0u")
}

fn dpp8(em: &mut Emitter<'_>, ins: &Insn, fetch_inactive: bool) -> Result<Vec<String>, String> {
    if !matches!(ins.enc, Enc::Vop1 | Enc::Vop2 | Enc::Vopc) {
        return Err("paired wave64: DPP8 outside a short encoding".into());
    }
    let control = *ins.raw.get(1).ok_or("paired wave64: DPP8 without its control word")?;
    let (register, selectors) = (control & 0xff, control >> 8);
    em.regs.insert(format!("v{register}"));
    let mut lines = Vec::new();
    for lane in 0..2u32 {
        let me = format!("(tb_pair + {}u)", 32 * lane);
        let partner = format!("({me} & ~7u) | (({selectors:#x}u >> (3u * ({me} & 7u))) & 7u)");
        lines.extend(from_half(register, "tbd_", lane, &partner));
        if !fetch_inactive {
            lines.push(format!("if (!({})) tbd_{lane} = 0u;", source_active(lane)));
        }
    }
    Ok(lines)
}

/// The sixteen-lane-row controls: which lane each lane reads, and which lanes write.
fn dpp16(em: &mut Emitter<'_>, ins: &Insn) -> Result<Vec<String>, String> {
    if !matches!(ins.enc, Enc::Vop1 | Enc::Vop2) {
        return Err("paired wave64: DPP16 outside a short arithmetic encoding".into());
    }
    let control = *ins.raw.get(1).ok_or("paired wave64: DPP16 without its control word")?;
    let (register, select) = (control & 0xff, (control >> 8) & 0x1ff);
    let (fetch_inactive, zero_invalid) = (control & (1 << 18) != 0, control & (1 << 19) != 0);
    let (banks, rows) = ((control >> 24) & 0xf, control >> 28);
    if control & 0x00f0_0000 != 0 {
        return Err("paired wave64: DPP16 source modifiers".into());
    }
    em.regs.insert(format!("v{register}"));
    let mut lines = Vec::new();
    for lane in 0..2u32 {
        let me = format!("(tb_pair + {}u)", 32 * lane);
        let count = select & 15;
        // (the lane read, whether it lies inside the row)
        let (partner, inside) = match select {
            0..=0xff => (format!("({me} & ~3u) | (({select:#x}u >> (2u * ({me} & 3u))) & 3u)"), "true".to_string()),
            0x101..=0x10f => (format!("({me} & ~15u) | (({me} + {count}u) & 15u)"), format!("({me} & 15u) + {count}u < 16u")),
            0x111..=0x11f => (format!("({me} & ~15u) | (({me} - {count}u) & 15u)"), format!("({me} & 15u) >= {count}u")),
            0x121..=0x12f => (format!("({me} & ~15u) | (({me} - {count}u) & 15u)"), "true".to_string()),
            0x140 => (format!("({me} & ~15u) | (15u - ({me} & 15u))"), "true".to_string()),
            0x141 => (format!("({me} & ~7u) | (7u - ({me} & 7u))"), "true".to_string()),
            0x150..=0x15f => (format!("({me} & ~15u) | {count}u"), "true".to_string()),
            0x160..=0x16f => (format!("{me} ^ {count}u"), "true".to_string()),
            _ => return Err(format!("paired wave64: DPP16 control {select:#x}")),
        };
        lines.extend(from_half(register, "tbd_", lane, &partner));
        let valid = if fetch_inactive { format!("({inside})") } else { format!("({inside}) && {}", source_active(lane)) };
        lines.push(format!("bool tbo_{lane} = {valid};"));
        lines.push(format!("if (!tbo_{lane}) tbd_{lane} = 0u;"));
        let enabled = format!("((({rows}u >> ({me} >> 4)) & ({banks}u >> ({me} & 3u))) & 1u)");
        let writes = if zero_invalid { enabled } else { format!("({enabled} & (tbo_{lane} ? 1u : 0u))") };
        lines.push(format!("uint tbw_{lane} = {writes};"));
    }
    Ok(lines)
}

/// Rows of sixteen lanes exchange values, within a row or with the neighbouring one.
fn permlane(em: &mut Emitter<'_>, ins: &Insn) -> Result<(), String> {
    if (ins.raw[0] >> 11) & 0xf != 0 || ins.aux.mask != 0 || ins.aux.imm != 0 || ins.aux.imm2 != 0 {
        return Err("paired wave64: permlane modifiers".into());
    }
    let (Some(Opnd::Vgpr(to)), Some(Opnd::Vgpr(from))) = (ins.dst.first(), ins.src.first()) else {
        return Err("paired wave64: permlane operands".into());
    };
    let (low, high) = (r(&ins.src[1])?, r(&ins.src[2])?);
    let across = if ins.mnemonic == "v_permlanex16_b32" { " ^ 16u" } else { "" };
    em.line("{");
    for lane in 0..2u32 {
        let me = format!("(tb_pair + {}u)", 32 * lane);
        let selector = format!("((({me} & 8u) != 0u ? {high} : {low}) >> (4u * ({me} & 7u))) & 15u");
        let partner = format!("(({me} & ~15u){across}) | ({selector})");
        for line in from_half(u32::from(*from), "tbp_", lane, &partner) {
            em.line(&line);
        }
    }
    // Both lanes are read before either is written. An inactive source keeps the destination.
    for lane in 0..2u32 {
        let active = lane_bit("exec_lo", "exec_hi", lane);
        em.line(&format!("if ({active} != 0u && {}) v{to}__{lane} = tbp_{lane};", source_active(lane)));
    }
    em.line("}");
    Ok(())
}

/// Permute lanes within each quad (QDMode) or within each 32-lane half-wave.
fn ds_swizzle(em: &mut Emitter<'_>, ins: &Insn) -> Result<(), String> {
    let mode = ins.aux.imm as u32;
    let quad = mode & 0x8000 != 0;
    if ins.raw[0] & 0x3_0000 != 0 || (quad && mode & 0xff00 != 0x8000) {
        return Err("unsupported compute DS swizzle encoding".into());
    }
    let (Some(Opnd::Vgpr(to)), Some(Opnd::Vgpr(from))) = (ins.dst.first(), ins.src.first()) else {
        return Err("paired wave64: ds_swizzle operands".into());
    };
    em.line("{");
    for lane in 0..2u32 {
        let me = format!("(tb_pair + {}u)", 32 * lane);
        let partner = if quad {
            format!("({me} & ~3u) | (({:#x}u >> (2u * ({me} & 3u))) & 3u)", mode & 255)
        } else {
            format!("({me} & ~31u) | ((({me} & {}u) | {}u) ^ {}u)", mode & 31, (mode >> 5) & 31, (mode >> 10) & 31)
        };
        for line in from_half(u32::from(*from), "tbs_", lane, &partner) {
            em.line(&line);
        }
        em.line(&format!("if (!({})) tbs_{lane} = 0u;", source_active(lane)));
    }
    // Both lanes are read before either is written.
    for lane in 0..2u32 {
        let active = lane_bit("exec_lo", "exec_hi", lane);
        em.line(&format!("if ({active} != 0u) v{to}__{lane} = tbs_{lane};"));
    }
    em.line("}");
    Ok(())
}

/// Inline only the small integer ALU subset whose emitted body is a single
/// predicated VGPR assignment. Both lanes have distinct storage, and no
/// scalar or mask is written. Anything outside this closed expression grammar
/// keeps the generic shadow-register path.
fn direct_alu_expr(rhs: &str, lane: u32, moved: bool) -> Option<String> {
    if !rhs.is_ascii() || rhs.contains("++") || rhs.contains("--") {
        return None;
    }
    let bytes = rhs.as_bytes();
    let mut out = String::new();
    let mut at = 0;
    while at < bytes.len() {
        let start = at;
        if bytes[at].is_ascii_alphanumeric() || bytes[at] == b'_' {
            while at < bytes.len() && (bytes[at].is_ascii_alphanumeric() || bytes[at] == b'_') {
                at += 1;
            }
            let word = &rhs[start..at];
            if is_vector_register(word) {
                let _ = write!(out, "{word}__{lane}");
            } else if moved && word == "tb_s60000" {
                let _ = write!(out, "tbd_{lane}");
            } else if word.strip_prefix('s').is_some_and(|n| !n.is_empty() && n.bytes().all(|c| c.is_ascii_digit())) {
                out.push_str(word);
            } else {
                let literal = word.strip_suffix('u').unwrap_or(word);
                let valid = if let Some(hex) = literal.strip_prefix("0x") {
                    !hex.is_empty() && hex.bytes().all(|c| c.is_ascii_hexdigit())
                } else {
                    !literal.is_empty() && literal.bytes().all(|c| c.is_ascii_digit())
                };
                if !valid {
                    return None;
                }
                out.push_str(word);
            }
        } else {
            if !matches!(bytes[at], b' ' | b'\t' | b'(' | b')' | b'+' | b'-'
                | b'*' | b'&' | b'|' | b'^' | b'~' | b'<' | b'>') {
                return None;
            }
            out.push(bytes[at] as char);
            at += 1;
        }
    }
    Some(out)
}

fn direct_alu(em: &mut Emitter<'_>, ins: &Insn, code: &str, moved: Option<&[String]>, gated: bool) -> bool {
    if !matches!(ins.mnemonic.as_str(), "v_mov_b32" | "v_add_nc_u32" | "v_sub_nc_u32"
        | "v_subrev_nc_u32" | "v_and_b32" | "v_or_b32" | "v_xor_b32" | "v_not_b32"
        | "v_lshlrev_b32" | "v_lshrrev_b32") {
        return false;
    }
    let [Opnd::Vgpr(dst)] = ins.dst.as_slice() else { return false };
    let mut lines = code.lines().map(str::trim).filter(|line| !line.is_empty() && !line.starts_with("//"));
    let Some(statement) = lines.next() else { return false };
    if lines.next().is_some() { return false; }
    let prefix = format!("if (exec_lo & 1u) v{dst} = ");
    let Some(rhs) = statement.strip_prefix(&prefix).and_then(|s| s.strip_suffix(';')) else { return false };
    let Some(first) = direct_alu_expr(rhs, 0, moved.is_some()) else { return false };
    let Some(second) = direct_alu_expr(rhs, 1, moved.is_some()) else { return false };
    for (word, _) in identifiers(code) {
        if is_vector_register(word) {
            em.regs.insert(word.to_string());
        }
    }
    if let Some(reads) = moved {
        em.line("{");
        for line in reads {
            em.line(line);
        }
    }
    for (lane, rhs) in [first, second].iter().enumerate() {
        let active = lane_bit("exec_lo", "exec_hi", lane as u32);
        let gate = if gated { format!(" & tbw_{lane}") } else { String::new() };
        em.line(&format!("if ({active}{gate}) v{dst}__{lane} = {rhs};"));
    }
    if moved.is_some() {
        em.line("}");
    }
    true
}

/// A simple unsigned comparison produces two ballot words directly. Keep
/// the generic lane-local implementation for any other emitted shape,
/// including CMPX, DPP, mask operands and aliased scalar destinations.
fn direct_compare(em: &mut Emitter<'_>, ins: &Insn, code: &str, written: &[[String; 2]]) -> bool {
    let op = match ins.mnemonic.as_str() {
        "v_cmp_eq_u32" => "==",
        "v_cmp_ne_u32" => "!=",
        "v_cmp_lt_u32" => "<",
        "v_cmp_le_u32" => "<=",
        "v_cmp_gt_u32" => ">",
        "v_cmp_ge_u32" => ">=",
        _ => return false,
    };
    let [[low, high]] = written else { return false };
    let [a, b] = ins.src.as_slice() else { return false };
    let operand = |source: &Opnd, lane: Option<u32>| -> Option<String> {
        match source {
            Opnd::Vgpr(n) => Some(match lane {
                Some(lane) => format!("v{n}__{lane}"),
                None => format!("v{n}"),
            }),
            Opnd::Sgpr(_) => {
                let value = r(source).ok()?;
                if &value == low || &value == high { None } else { Some(value) }
            }
            Opnd::ConstInt(_) | Opnd::Literal(_) => r(source).ok(),
            _ => None,
        }
    };
    let (Some(a0), Some(b0)) = (operand(a, None), operand(b, None)) else { return false };
    let expected = format!("{low} = (((exec_lo & 1u) != 0u) && ({a0} {op} {b0})) ? 1u : 0u; {high} = 0u;");
    let actual = code.lines().map(str::trim).filter(|s| !s.is_empty() && !s.starts_with("//"))
        .collect::<Vec<_>>().join(" ");
    if actual != expected { return false; }
    for (word, _) in identifiers(code) {
        if is_vector_register(word) {
            em.regs.insert(word.to_string());
        }
    }
    em.line("{");
    for lane in 0..2u32 {
        // Ballot across the host wave, then intersect with guest EXEC.
        // Both words are computed before either destination is modified.
        let (a, b) = (operand(a, Some(lane)).unwrap(), operand(b, Some(lane)).unwrap());
        let exec = if lane == 0 { "exec_lo" } else { "exec_hi" };
        em.line(&format!("uint tb_cmp{lane} = WaveActiveBallot({a} {op} {b}).x & {exec};"));
    }
    em.line(&format!("{low} = tb_cmp0; {high} = tb_cmp1;"));
    em.line("}");
    true
}

pub(super) fn emit(em: &mut Emitter<'_>, idx: usize, ins: &Insn, is_ps: bool, br: Option<usize>) -> Result<(), String> {
    let m = ins.mnemonic.as_str();
    if !per_lane(ins.enc) {
        let start = em.body.len();
        em.emit_insn_body(idx, ins, is_ps, br)?;
        if identifiers(&em.body[start..]).any(|(word, _)| word.starts_with("Wave") || word.starts_with("Quad")) {
            return Err(format!("paired wave64: unhandled wave operation in scalar instruction {m}"));
        }
        return Ok(());
    }
    em.line(&format!("// {:#06x} {m}", ins.offset));
    em.cur_idx = idx;
    if !matches!(m, "v_readfirstlane_b32" | "v_readlane_b32") {
        for operand in ins.src.iter().chain(&ins.dst) {
            em.touch(operand);
        }
    }
    let destination = ins.dst.first();
    match m {
        "v_readfirstlane_b32" => {
            return read_lane(em, destination.ok_or("no destination")?, &ins.src[0], "");
        }
        "v_readlane_b32" => {
            let lane = format!("({}) & 63u", r(&ins.src[1])?);
            return read_lane(em, destination.ok_or("no destination")?, &ins.src[0], &lane);
        }
        "v_writelane_b32" => {
            let Some(Opnd::Vgpr(n)) = destination else { return Err("paired wave64: writelane destination".into()) };
            let (value, lane) = (r(&ins.src[0])?, r(&ins.src[1])?);
            em.line(&format!("if ((({lane}) & 63u) == tb_pair) v{n}__0 = {value};"));
            em.line(&format!("if ((({lane}) & 63u) == tb_pair + 32u) v{n}__1 = {value};"));
            return Ok(());
        }
        "v_mbcnt_lo_u32_b32" | "v_mbcnt_hi_u32_b32" => {
            // The first source is a whole mask word, not this lane's bit.
            let Some(Opnd::Vgpr(n)) = destination else { return Err("paired wave64: mbcnt destination".into()) };
            let mask = r(&ins.src[0])?;
            for lane in 0..2u32 {
                let add = match &ins.src[1] {
                    Opnd::Vgpr(a) => format!("v{a}__{lane}"),
                    other => r(other)?,
                };
                let below = match (m.ends_with("lo_u32_b32"), lane) {
                    (true, 0) => "((1u << tb_pair) - 1u)",
                    (true, _) => "0xffffffffu",
                    (false, 0) => "0u",
                    (false, _) => "((1u << tb_pair) - 1u)",
                };
                let active = lane_bit("exec_lo", "exec_hi", lane);
                em.line(&format!("if ({active} != 0u) v{n}__{lane} = {add} + countbits(({mask}) & {below});"));
            }
            return Ok(());
        }
        _ => {}
    }
    if matches!(m, "v_permlane16_b32" | "v_permlanex16_b32") {
        return permlane(em, ins);
    }
    if m == "ds_swizzle_b32" {
        return ds_swizzle(em, ins);
    }
    // A DPP8 first source reads a neighbour within the lane's group of eight.
    let (mut moved, mut gated) = (None, false);
    match ins.src.first() {
        Some(&Opnd::Raw(code @ (233 | 234))) => moved = Some(dpp8(em, ins, code == 234)?),
        Some(&Opnd::Raw(250)) => (moved, gated) = (Some(dpp16(em, ins)?), true),
        _ => {}
    }
    let mut ins = ins.clone();
    if moved.is_some() {
        ins.src[0] = Opnd::Temp(MOVED);
    }
    let ins = &ins;
    if m.contains("permlane") || m.contains("swizzle") || m.starts_with("tb_") || m.starts_with("ds_append")
        || m.starts_with("ds_consume") || m.starts_with("ds_ordered") {
        return Err(format!("paired wave64: cross-lane instruction {m}"));
    }

    let start = em.body.len();
    em.emit_insn_body(idx, ins, is_ps, br)?;
    let code = em.body.split_off(start);
    if let Some((word, _)) = identifiers(&code).find(|(word, _)| {
        word.starts_with("Wave") || word.starts_with("Quad") || word.starts_with("tb_lane") || word.starts_with("tb_swizzle")
    }) {
        return Err(format!("paired wave64: {m} needs {word}"));
    }
    let mut written: Vec<[String; 2]> = ins.dst.iter().filter_map(mask_words).collect();
    // `v_cmpx` and the two-operand carry forms name their mask destination only in the opcode.
    let implied = if m.starts_with("v_cmpx_") { Some(Opnd::ExecLo) }
        else if ins.enc == Enc::Vop2 && (m.contains("_co_") || m.ends_with("_co_u32")) { Some(Opnd::VccLo) }
        else { None };
    // Three-operand carry and 64-bit multiply-add keep their mask destination in the first word.
    let carries = m.contains("_co_") || m.ends_with("_co_u32") || matches!(m, "v_mad_u64_u32" | "v_mad_i64_i32");
    let scalar = if ins.enc == Enc::Vop3 && carries {
        match (ins.raw[0] >> 8) & 0x7f {
            c @ 0..=105 => Some(Opnd::Sgpr(c as u8)),
            106 => Some(Opnd::VccLo),
            _ => None,
        }
    } else { None };
    if let Some(words) = scalar.as_ref().and_then(mask_words) {
        if !written.contains(&words) {
            written.push(words);
        }
    }
    if let Some(words) = implied.as_ref().and_then(mask_words) {
        if !written.contains(&words) {
            written.push(words);
        }
    }
    let mut read: Vec<[String; 2]> = vec![["vcc_lo".into(), "vcc_hi".into()], ["exec_lo".into(), "exec_hi".into()]];
    if m.starts_with("v_cndmask") || m.contains("_co_ci_") {
        if let Some(words) = ins.src.get(2).and_then(mask_words) {
            if !read.contains(&words) {
                read.push(words);
            }
        }
    }
    // Every scalar the code assigns must be a mask this instruction is known to write.
    for (word, after) in identifiers(&code) {
        let scalar = word == "vcc_lo" || word == "vcc_hi" || word == "exec_lo" || word == "exec_hi"
            || (word.len() > 1 && word.starts_with('s') && word[1..].bytes().all(|b| b.is_ascii_digit()));
        let after = after.trim_start();
        if scalar && after.starts_with('=') && !after.starts_with("==") && !written.iter().any(|w| w.iter().any(|n| n == word)) {
            return Err(format!("paired wave64: {m} writes {word} outside a known mask"));
        }
    }
    if written.is_empty() && direct_alu(em, ins, &code, moved.as_deref(), gated) {
        return Ok(());
    }
    if moved.is_none() && direct_compare(em, ins, &code, &written) {
        return Ok(());
    }
    let names: BTreeSet<&str> = identifiers(&code).map(|(word, _)| word).collect();
    let vectors: BTreeSet<&str> = names.iter().copied().filter(|w| is_vector_register(w)).collect();
    // EXEC is needed for ballot reconstruction even when the body does
    // not mention it. Other masks need a local only if used or written.
    read.retain(|pair| pair[0] == "exec_lo" || written.contains(pair)
        || pair.iter().any(|word| names.contains(word.as_str())));
    // Arithmetic has no cross-lane VGPR access here: DPP sources were
    // captured before either lane writes, and wave intrinsics were rejected.
    // Keep the existing shadow path for memory instructions.
    let direct = matches!(ins.enc, Enc::Vop1 | Enc::Vop2 | Enc::Vop3 | Enc::Vopc);
    let mut shadowed = read.clone();
    for words in &written {
        if !shadowed.contains(words) {
            shadowed.push(words.clone());
        }
    }
    em.line("{");
    if let Some(reads) = &moved {
        for line in reads {
            em.line(line);
        }
    }
    // A write-only mask starts at zero in each lane. Its full original
    // value is never consumed; avoid snapshotting it.
    for [low, high] in &read {
        em.line(&format!("uint tbx_{low} = {low}, tbx_{high} = {high};"));
    }
    for [low, _] in &written {
        em.line(&format!("bool tbm_{low}_0 = false, tbm_{low}_1 = false;"));
    }
    for lane in 0..2u32 {
        em.line("{");
        for words @ [low, high] in &shadowed {
            // A mask the instruction only writes starts clear in every lane.
            let value = if gated && low == "exec_lo" {
                format!("{} & tbw_{lane}", lane_bit("tbx_exec_lo", "tbx_exec_hi", lane))
            } else if read.contains(words) {
                lane_bit(&format!("tbx_{low}"), &format!("tbx_{high}"), lane)
            } else {
                "0u".into()
            };
            em.line(&format!("uint {low} = {value}, {high} = 0u;"));
        }
        for vector in &vectors {
            em.regs.insert((*vector).to_string());
            if !direct {
                em.line(&format!("uint {vector} = {vector}__{lane};"));
            }
        }
        if moved.is_some() {
            em.line(&format!("uint {} = tbd_{lane};", name(&Opnd::Temp(MOVED))?));
        }
        if direct {
            em.body.push_str(&paired_vector_code(&code, lane));
        } else {
            em.body.push_str(&code);
            for vector in &vectors {
                em.line(&format!("{vector}__{lane} = {vector};"));
            }
        }
        for [low, _] in &written {
            em.line(&format!("tbm_{low}_{lane} = ({low} & 1u) != 0u;"));
        }
        em.line("}");
    }
    for [low, high] in &written {
        // Inactive lanes write zero. A compare into EXEC already includes the old mask.
        let (a, b) = (format!("WaveActiveBallot(tbm_{low}_0).x"), format!("WaveActiveBallot(tbm_{low}_1).x"));
        em.line(&format!("{low} = {a} & tbx_exec_lo; {high} = {b} & tbx_exec_hi;"));
    }
    em.line("}");
    Ok(())
}

#[cfg(test)]
#[path = "../tests/hlsl/paired_direct_alu.rs"]
mod direct_alu_tests;
