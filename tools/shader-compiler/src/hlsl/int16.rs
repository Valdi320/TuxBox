//! VOP3 operates on one selected half of a register. VOP3P computes both
//! halves independently, with separate source selectors for each result.
use super::{r, Enc, Insn, Opnd};

enum Operation {
    Add,
    Subtract,
    Multiply,
    MultiplyAdd,
    UnsignedMax,
    UnsignedMin,
    ShiftLeft,
    ShiftRight,
    ArithmeticShiftRight,
    SignedMax,
    SignedMin,
}

pub(super) fn half(src: &Opnd, select: u32) -> Result<String, String> {
    let value = r(src)?;
    Ok(if select == 0 {
        format!("({value} & 0xffffu)")
    } else {
        format!("({value} >> 16)")
    })
}

pub(super) fn new_half(ins: &Insn) -> bool {
    ins.enc == Enc::Vop3 && matches!(ins.mnemonic.as_str(),
        "v_mul_lo_u16" | "v_max_u16" | "v_min_u16")
}

pub(super) fn expression(ins: &Insn) -> Result<Option<String>, String> {
    if ins.enc != Enc::Vop3 {
        return Ok(None);
    }
    let op = match ins.mnemonic.as_str() {
        "v_add_nc_u16" | "v_pk_add_u16" | "v_pk_add_i16" => Operation::Add,
        "v_sub_nc_u16" | "v_pk_sub_i16" | "v_pk_sub_u16" => Operation::Subtract,
        "v_mul_lo_u16" | "v_pk_mul_lo_u16" => Operation::Multiply,
        "v_pk_mad_i16" | "v_pk_mad_u16" => Operation::MultiplyAdd,
        "v_max_u16" | "v_pk_max_u16" => Operation::UnsignedMax,
        "v_min_u16" | "v_pk_min_u16" => Operation::UnsignedMin,
        "v_lshlrev_b16" | "v_pk_lshlrev_b16" => Operation::ShiftLeft,
        "v_lshrrev_b16" | "v_pk_lshrrev_b16" => Operation::ShiftRight,
        "v_ashrrev_i16" | "v_pk_ashrrev_i16" => Operation::ArithmeticShiftRight,
        "v_max_i16" | "v_pk_max_i16" => Operation::SignedMax,
        "v_min_i16" | "v_pk_min_i16" => Operation::SignedMin,
        _ => return Ok(None),
    };
    let packed = ins.op >= 0x400;
    let new_packed = matches!(ins.mnemonic.as_str(),
        "v_pk_add_i16" | "v_pk_mad_i16" | "v_pk_mad_u16" | "v_pk_max_u16" | "v_pk_min_u16");
    let arity = if matches!(op, Operation::MultiplyAdd) { 3 } else { 2 };
    if !ins.is_complete() || ins.raw.len() < 2 || ins.src.len() < arity {
        return Err("incomplete 16-bit integer instruction".into());
    }
    // New forms are initially limited to ordinary VGPR inputs. In particular
    // do not interpret an EXEC/VCC selector as packed integer data.
    if new_packed && (!packed || !ins.src[..arity].iter().all(|s| matches!(s, Opnd::Vgpr(_)))) {
        return Err("unsupported packed integer source or encoding".into());
    }
    if new_half(ins) && (packed || !ins.src[..arity].iter().all(|s| matches!(s, Opnd::Vgpr(_)))) {
        return Err("unsupported ordinary 16-bit integer source or encoding".into());
    }
    let (w0, w1) = (ins.raw[0], ins.raw[1]);
    // RDNA2 ISA sections 6.5 and 12.10: clamp arithmetic to the
    // representable signed/unsigned 16-bit interval, independently per half.
    let clamped = w0 & 0x8000 != 0;
    let saturating_arithmetic = packed && matches!(op,
        Operation::Add | Operation::Subtract | Operation::Multiply | Operation::MultiplyAdd);
    if clamped && (!saturating_arithmetic
        || !ins.src[..arity].iter().all(|s| matches!(s, Opnd::Vgpr(_)))) {
        return Err("unsupported packed integer clamp form or source".into());
    }
    if w0 & 0x0700 != 0
        || w1 >> (if packed { 29 } else { 27 }) != 0
        || ins.src[..arity].iter().any(|s| matches!(s, Opnd::ConstF32(_)))
    {
        return Err("unsupported 16-bit integer modifiers or inline float".into());
    }
    let result = |selectors: u32| -> Result<String, String> {
        let a = half(&ins.src[0], selectors & 1)?;
        let b = half(&ins.src[1], (selectors >> 1) & 1)?;
        let signed = |x: &str| format!("(asint({x} << 16) >> 16)");
        if clamped {
            let signed_op = matches!(ins.mnemonic.as_str(),
                "v_pk_add_i16" | "v_pk_sub_i16" | "v_pk_mad_i16");
            if signed_op {
                let (a, b) = (signed(&a), signed(&b));
                let value = match op {
                    Operation::Add => format!("({a} + {b})"),
                    Operation::Subtract => format!("({a} - {b})"),
                    Operation::MultiplyAdd => {
                        let c = signed(&half(&ins.src[2], (selectors >> 2) & 1)?);
                        format!("({a} * {b} + {c})")
                    }
                    _ => unreachable!("signed clamp arithmetic"),
                };
                // Keep the saturated representation unsigned before packing. The
                // sign-biased ordering is exactly the signed ordering, but
                // avoids narrow signed intermediates in the host optimizer.
                return Ok(format!("(clamp((asuint({value}) ^ 0x80000000u), 0x7fff8000u, 0x80007fffu) ^ 0x80000000u)"));
            }
            // 65535*65535+65535 fits in uint32, and unsigned subtract
            // must clamp at zero before any modular underflow.
            let value = match op {
                Operation::Add => format!("({a} + {b})"),
                Operation::Subtract => format!("(max({a}, {b}) - {b})"),
                Operation::Multiply => format!("({a} * {b})"),
                Operation::MultiplyAdd => {
                    let c = half(&ins.src[2], (selectors >> 2) & 1)?;
                    format!("({a} * {b} + {c})")
                }
                _ => unreachable!("unsigned clamp arithmetic"),
            };
            return Ok(format!("min({value}, 65535u)"));
        }
        Ok(match op {
            Operation::Add => format!("({a} + {b})"),
            Operation::Subtract => format!("({a} - {b})"),
            Operation::Multiply => format!("({a} * {b})"),
            // Without clamp, signed and unsigned multiply-add produce the
            // same low 16 bits. The uint expression is modular even if the
            // intermediate wraps at 32 bits.
            Operation::MultiplyAdd => {
                let c = half(&ins.src[2], (selectors >> 2) & 1)?;
                format!("({a} * {b} + {c})")
            }
            Operation::UnsignedMax => format!("max({a}, {b})"),
            Operation::UnsignedMin => format!("min({a}, {b})"),
            Operation::ShiftLeft => format!("({b} << ({a} & 15u))"),
            Operation::ShiftRight => format!("({b} >> ({a} & 15u))"),
            Operation::ArithmeticShiftRight => format!("asuint({} >> ({a} & 15u))", signed(&b)),
            Operation::SignedMax => format!("asuint(max({}, {}))", signed(&a), signed(&b)),
            Operation::SignedMin => format!("asuint(min({}, {}))", signed(&a), signed(&b)),
        })
    };
    let lo = result(w0 >> 11)?;
    Ok(Some(if packed {
        let hi = result(((w1 >> 27) & 3) | (((w0 >> 14) & 1) << 2))?;
        format!("(({lo} & 0xffffu) | ({hi} << 16))")
    } else {
        let dst = r(&ins.dst[0])?;
        if (w0 & (1 << 14)) != 0 {
            format!("(({dst} & 0xffffu) | ({lo} << 16))")
        } else {
            format!("(({dst} & 0xffff0000u) | ({lo} & 0xffffu))")
        }
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rdna2::disassemble;

    #[test]
    fn ordinary_half_forms_preserve_the_other_half() {
        for (first, mnemonic) in [
            (0xd705_4804u32, "v_mul_lo_u16"),
            (0xd709_4804u32, "v_max_u16"),
            (0xd70b_4804u32, "v_min_u16"),
        ] {
            let decode = |a: u32, b: u32| {
                let bytes: Vec<u8> = [a,b].iter().flat_map(|v| v.to_le_bytes()).collect();
                disassemble(&bytes).remove(0)
            };
            let ins=decode(first,0x0202_0b04);
            assert_eq!(ins.mnemonic,mnemonic);
            assert!(new_half(&ins));
            let expr=expression(&ins).unwrap().unwrap();
            assert!(expr.contains("v4 & 0xffffu"));
            let expr=expression(&decode(first & !0x4000,0x0202_0b04)).unwrap().unwrap();
            assert!(expr.contains("v4 & 0xffff0000u"));
            assert!(expression(&decode(first | 0x8000,0x0202_0b04)).is_err());
            assert!(expression(&decode(first,0x0202_0a04)).is_err());
        }
    }

    #[test]
    fn new_packed_integer_forms_accept_ordinary_sources_only() {
        // LLVM gfx1030 native words. The default high selectors include
        // source 2 via dword0 bit 14 for the three-operand forms.
        for (first, second, name) in [
            (0xcc02_4004, 0x1a02_0b04, "v_pk_add_i16"),
            (0xcc00_2804, 0x141a_0b04, "v_pk_mad_i16"),
            (0xcc09_4004, 0x1c1a_0b04, "v_pk_mad_u16"),
            (0xcc0c_4804, 0x1202_0b04, "v_pk_max_u16"),
            (0xcc0d_4004, 0x1a02_0b04, "v_pk_min_u16"),
        ] {
            let native = |a: u32, b: u32| {
                let bytes: Vec<u8> = [a,b].iter().flat_map(|v| v.to_le_bytes()).collect();
                disassemble(&bytes).remove(0)
            };
            let ins = native(first,second);
            assert_eq!(ins.mnemonic, name);
            assert!(expression(&ins).unwrap().is_some());
            if matches!(name, "v_pk_max_u16" | "v_pk_min_u16") {
                assert!(expression(&native(first | 0x8000, second)).is_err());
            } else {
                let clamped = expression(&native(first | 0x8000, second)).unwrap().unwrap();
                if name.ends_with("_i16") {
                    assert!(clamped.contains("0x7fff8000u, 0x80007fffu"));
                    assert!(clamped.contains("^ 0x80000000u"));
                }
            }
            assert!(expression(&native(first | 0x8000, (second & !511) | 4)).is_err());
            assert!(expression(&native(first, (second & !511) | 4)).is_err());
            assert!(expression(&native(first | 0x100, second)).is_err());
        }
        let bytes: Vec<u8> = [0xcc09_4004u32,0x1c1a_0b04].iter().flat_map(|v| v.to_le_bytes()).collect();
        let expr = expression(&disassemble(&bytes)[0]).unwrap().unwrap();
        assert!(expr.contains("(v6 & 0xffffu)"));
        assert!(expr.contains("(v6 >> 16)"));
    }
}
