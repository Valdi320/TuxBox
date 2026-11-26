use super::*;

#[test]
fn wave_range_compatibility_checks_interval_overlap() {
    let wave64_refusal = check_wave_range((64, 64), (32, 32)).unwrap();
    assert_eq!(wave64_refusal.tag, "host-lacks-wave64");
    assert!(wave64_refusal.detail.contains("host lacks wave64"));
    assert_eq!(check_wave_range((64, 64), (32, 64)), None);
    assert_eq!(check_wave_range((64, 64), (64, 64)), None);
    assert_eq!(check_wave_range((32, 32), (32, 32)), None);
    assert_eq!(check_wave_range((32, 32), (32, 64)), None);
    assert_eq!(check_wave_range((32, 64), (32, 32)), None);
    let wave32_refusal = check_wave_range((32, 32), (64, 64)).unwrap();
    assert_eq!(wave32_refusal.tag, "host-lacks-wave32");
    let range_refusal = check_wave_range((64, 128), (32, 32)).unwrap();
    assert_eq!(range_refusal.tag, "host-wave-range-32-32-excludes-64-128");
    assert_eq!(check_wave_range((64, 64), (0, 0)), None);
    assert_eq!(check_wave_range((64, 64), (64, 32)), None);
}

#[test]
fn only_a_64_lane_program_on_a_32_lane_host_is_translated_again() {
    assert!(needs_narrow_waves(Some((64, 64)), (32, 32)));
    assert!(!needs_narrow_waves(Some((64, 64)), (32, 64)));
    assert!(!needs_narrow_waves(Some((64, 64)), (64, 64)));
    assert!(!needs_narrow_waves(Some((32, 32)), (32, 32)));
    assert!(!needs_narrow_waves(None, (32, 32)));
    // The translation runs waves of 32, so a host limited below that cannot use it.
    assert!(!needs_narrow_waves(Some((64, 64)), (16, 16)));
}

#[test]
fn compute_compile_labels_distinguish_rooted_narrow_and_original_attempts() {
    let guest = "a".repeat(64);
    let rooted = "b".repeat(64);
    let narrow = format!("{rooted}_w32");
    assert_eq!(compute_compile_label(Some(&guest), Some(&rooted), 123),
               format!("CS aaaaaaaa request={rooted} bytes=123"));
    assert_eq!(compute_compile_label(Some(&guest), Some(&narrow), 456),
               format!("CS aaaaaaaa request={narrow} bytes=456"));
    assert_eq!(compute_compile_label(Some(&guest), None, 789),
               "CS aaaaaaaa request=original bytes=789");
}
