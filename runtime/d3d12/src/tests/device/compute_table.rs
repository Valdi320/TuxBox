use super::*;
use tuxbox_dxbc::rootsig::{RootSig, parse_rts0};

const TABLE: u32 = 0;
const CONSTANTS: u32 = 1;
const ROOT_SRV: u32 = 3;

/// A version 1.1 RTS0 with the given root flags and parameter types: tables of
/// four SRVs, four root constants at b0, root SRVs at t0.
fn root(flags: u32, kinds: &[u32]) -> RootSig {
    let header = 24 + 12 * kinds.len() as u32;
    let mut words = vec![2, kinds.len() as u32, 24, 0, 0, flags];
    let mut body: Vec<u32> = Vec::new();
    for &kind in kinds {
        let at = header + 4 * body.len() as u32;
        words.extend([kind, 0, at]);
        match kind {
            TABLE => body.extend([1, at + 8, 0, 4, 0, 0, 0, 0]),
            CONSTANTS => body.extend([0, 0, 4]),
            _ => body.extend([0, 0, 0]),
        }
    }
    words.extend(body);
    let bytes: Vec<u8> = words.iter().flat_map(|w| w.to_le_bytes()).collect();
    parse_rts0(&bytes).expect("a valid test root")
}

#[test]
fn bindless_compute_roots_have_no_tables_and_index_the_heap() {
    assert!(bindless_compute_root(&root(0x400, &[CONSTANTS])));
    assert!(bindless_compute_root(&root(0x400, &[CONSTANTS, ROOT_SRV])));
    assert!(bindless_compute_root(&root(0x400, &[])));
    // Without the flag the program cannot reach the heap by index.
    assert!(!bindless_compute_root(&root(0, &[CONSTANTS])));
    // A table keeps the existing reflected paths.
    assert!(!bindless_compute_root(&root(0x400, &[TABLE, CONSTANTS])));
}
