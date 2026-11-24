//! Emit a synthetic guarded reduction via the actual RDNA2 translator.
use tuxbox_xshader::{ShaderExtract, Stage, hlsl};

mod fixture {
    include!(concat!(env!("CARGO_MANIFEST_DIR"), "/examples/support/wave_guard_fixture.rs"));
}

fn main() {
    let width: u32 = std::env::args().nth(1).expect("wave width").parse().expect("integer");
    assert!(matches!(width, 32 | 64));
    let (shader, _, _) = fixture::shader(width);
    let translated = hlsl::translate(&shader).expect("translate synthetic reductions");
    assert_eq!(translated.required_wave_size, (width == 32).then_some(32));
    print!("{}", translated.hlsl);
}
