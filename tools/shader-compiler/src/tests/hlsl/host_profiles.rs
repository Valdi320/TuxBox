//! What each host profile receives for a fixed set of synthetic programs.
//!
//! A change to the translator shows here which hosts it affects. When the change
//! is meant, update the record and include it in the pull request:
//! `TUXBOX_BLESS_PROFILES=1 cargo test ... host_profiles`.
use super::*;

#[path = "host_profile_programs.rs"]
mod programs;

const RECORD: &str = include_str!("host_profiles.txt");

fn program(threads: u32, words: &[u32]) -> ShaderExtract {
    let isa: Vec<u8> = words.iter().flat_map(|word| word.to_le_bytes()).collect();
    ShaderExtract {
        flat_inputs: Vec::new(), flat_outputs: Vec::new(), stage: Stage::Compute, shader_model: (6, 0),
        required_features: None, bitcode_stripped: true, fspx_config: None, ps09_pixel_inputs: None,
        fssm: Vec::new(), entry: Some("main".into()), isa_data: isa.clone(), isa, isa_offset_in_fssm: 0,
        psv_threads: None, isa_preamble: [0, threads, 1, 1], has_input_sig: false, has_output_sig: false,
        has_psv0: false, has_root_sig: false, input_sig: Vec::new(), output_sig: Vec::new(), resources: Vec::new(),
        gs_info: None, mesh_info: None, primitive_sig: Vec::new(), root_sig: None,
    }
}

/// FNV-1a, enough to notice that a translation changed.
fn summary(translated: &Result<Translated, String>) -> String {
    match translated {
        Ok(t) => {
            let hash = t.hlsl.bytes().fold(0xcbf2_9ce4_8422_2325u64, |hash, byte| {
                (hash ^ u64::from(byte)).wrapping_mul(0x0100_0000_01b3)
            });
            let waves = t.required_wave_size.map_or("any".into(), |width| width.to_string());
            format!("{} waves={waves} {hash:016x}", t.profile)
        }
        Err(_) => "refused".into(),
    }
}

#[test]
fn every_host_profile_receives_the_recorded_translation() {
    let mut now = String::new();
    let mut narrowed = 0;
    for &(name, threads, words) in programs::PROGRAMS {
        let shader = program(threads, words);
        let ordinary = translate(&shader);
        let narrow = translate_for_wave32_host(&shader);
        // Only a program that needs 64-lane waves may differ on a narrower host.
        if matches!(&ordinary, Ok(t) if t.required_wave_size == Some(64)) {
            narrowed += 1;
        } else {
            assert_eq!(summary(&narrow), summary(&ordinary), "{name}");
        }
        writeln!(now, "{name} any-host {}", summary(&ordinary)).unwrap();
        writeln!(now, "{name} wave32-host {}", summary(&narrow)).unwrap();
        writeln!(now, "{name} two-lanes {}", summary(&translate_paired_wave64(&shader))).unwrap();
    }
    assert!(narrowed > 0, "no program exercises the narrow-wave path");
    if std::env::var_os("TUXBOX_BLESS_PROFILES").is_some() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/src/tests/hlsl/host_profiles.txt");
        std::fs::write(path, &now).unwrap();
        return;
    }
    let recorded = RECORD.replace("\r\n", "\n");
    let changed: Vec<&str> = now.lines().filter(|line| !recorded.lines().any(|old| old == *line)).collect();
    assert!(
        now == recorded,
        "translations differ for these programs and host profiles:\n{}\nIf this is intended, rerun with \
         TUXBOX_BLESS_PROFILES=1 and commit host_profiles.txt.",
        changed.join("\n")
    );
}

/// RDNA2 reads 0 past a group's LDS allocation and drops the writes. An HLSL
/// groupshared index past the array is undefined and can fault on NVIDIA, so
/// every access must be checked against the declared size.
#[test]
fn group_shared_accesses_are_checked_against_the_declared_size() {
    let &(_, threads, words) = programs::PROGRAMS.iter().find(|program| program.0 == "shared").unwrap();
    let shader = program(threads, words);
    let mut checked = 0;
    for translated in [translate(&shader), translate_for_wave32_host(&shader), translate_paired_wave64(&shader)] {
        let Ok(translated) = translated else { continue };
        let hlsl = &translated.hlsl;
        assert!(hlsl.contains("#define TB_LDS_WORDS "), "{hlsl}");
        assert!(!hlsl.contains("8191u"), "an access still wraps instead of being checked:\n{hlsl}");
        for line in hlsl.lines().filter(|line| line.contains("tb_lds[")) {
            assert!(line.contains("TB_LDS_WORDS"), "unchecked group shared access: {line}");
        }
        checked += 1;
    }
    assert!(checked > 0, "no host profile translated the shared-memory program");
}

#[test]
fn paired_carry_uses_paired_registers_and_only_needed_mask_locals() {
    let &(_, threads, words) = programs::PROGRAMS.iter().find(|p| p.0 == "cmpx_carry").unwrap();
    let hlsl = translate_paired_wave64(&program(threads, words)).unwrap().hlsl;
    // The carry instructions write a scalar mask, but must not copy all
    // vector operands back or snapshot a write-only mask.
    let carry = hlsl.split_once("// 0x0018 v_add_co_u32").expect("carry instruction").1
        .split("// 0x0024 v_add_co_ci_u32").next().unwrap();
    assert!(!carry.contains("uint tbx_s30"), "{carry}");
    assert!(!carry.contains("v0__0 = v0;"), "{carry}");
    assert!(!carry.contains("uint tbx_vcc_lo"), "{carry}");
    assert!(carry.contains("v10__0 = t0;"), "{carry}");
    assert!(carry.contains("v0__0"), "{carry}");
}

#[test]
fn paired_local_exchanges_need_only_one_read_per_guest_lane() {
    // All four instruction families select a source inside the lane's own
    // 32-lane half. Each of these fixtures has exactly two exchange operations.
    for name in ["dpp8", "row_shift", "row_masks", "permlane"] {
        let (_, threads, words) = programs::PROGRAMS.iter().find(|p| p.0 == name).unwrap();
        let hlsl = translate_paired_wave64(&program(*threads, words)).unwrap().hlsl;
        assert_eq!(hlsl.matches("WaveReadLaneAt(").count(), 4, "{name}");
        assert!(!hlsl.contains("tb_low"), "{name}");
        assert!(!hlsl.contains("tb_high"), "{name}");
    }
}

#[test]
fn paired_simple_alu_writes_lane_registers_directly() {
    // v_mov_b32_e32 v10, v0; v_add_nc_u32 v10, v10, v11; s_endpgm.
    let words = [0x7e140300, 0x4a14170a, 0xbf810000];
    let hlsl = translate_paired_wave64(&program(64, &words)).unwrap().hlsl;
    assert!(hlsl.contains("v10__0 = v0__0;"), "{hlsl}");
    assert!(hlsl.contains("v10__1 = v0__1;"));
    assert!(!hlsl.contains("uint tbx_exec_lo"));
}

#[test]
fn paired_integer_compare_emits_ballots_without_lane_shadows() {
    // v_cmp_lt_u32_e32 vcc, 0x3e8, v0; s_endpgm.
    let words = [0x7d8200ff, 0x000003e8, 0xbf810000];
    let hlsl = translate_paired_wave64(&program(64, &words)).unwrap().hlsl;
    assert!(hlsl.contains("WaveActiveBallot(0x3e8u < v0__0)"));
    assert!(hlsl.contains("WaveActiveBallot(0x3e8u < v0__1)"));
    assert!(!hlsl.contains("tbm_vcc_lo"));
    assert!(!hlsl.contains("uint v0 ="));
}
