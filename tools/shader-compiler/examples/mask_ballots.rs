//! Translate a synthetic compute program, supplied as native words, that reads
//! lane masks as numbers. Its thread group is one guest wave.
use tuxbox_xshader::{ShaderExtract, Stage, hlsl};

mod fixture {
    include!(concat!(env!("CARGO_MANIFEST_DIR"), "/examples/support/wave_guard_fixture.rs"));
}

fn main() {
    let mut args = std::env::args().skip(1);
    let first = args.next().expect("wave width or --paired");
    let paired = first == "--paired";
    let width: u32 = if paired { args.next().expect("wave width") } else { first }.parse().expect("integer");
    assert!(!paired || width == 64);
    assert!(matches!(width, 32 | 64));
    let words: Vec<u32> = args.map(|s| u32::from_str_radix(&s, 16).expect("hex native word")).collect();
    assert!(!words.is_empty() && words.len() <= 512 && words.last() == Some(&0xbf810000));
    let (mut shader, _, _): (ShaderExtract, _, _) = fixture::shader(width);
    shader.isa = words.iter().flat_map(|word| word.to_le_bytes()).collect();
    shader.isa_data = shader.isa.clone();
    assert!(matches!(shader.stage, Stage::Compute));
    let translated = if paired { hlsl::translate_paired_wave64(&shader) } else { hlsl::translate(&shader) };
    match translated {
        Ok(result) => {
            // A ballot holds the lanes of one guest wave.
            assert_eq!(result.required_wave_size, Some(if paired { 32 } else { width }));
            print!("{}", result.hlsl);
        }
        Err(reason) => {
            eprintln!("{reason}");
            std::process::exit(65);
        }
    }
}
