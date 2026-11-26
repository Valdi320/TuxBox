use super::*;

fn element(register: u32, comp_type: u32, mask: u8) -> SigElement {
    SigElement { name: "TEXCOORD".into(), index: register, sysval: 0, comp_type, register, mask, rw_mask: mask }
}

#[test]
fn half_registers_share_parameters_by_channel() {
    let sig = [element(1, 6, 15), element(2, 1, 3), element(3, 6, 7), element(4, 6, 3), element(5, 6, 7), element(6, 6, 15)];
    let params = assign(&sig, &[6], |_| true);
    assert_eq!(params, vec![
        Param::Half(vec![(1, 0), (3, 2)]), Param::Full(2), Param::Half(vec![(4, 0), (5, 1)]), Param::Full(6),
    ]);
    assert_eq!(params[0].component(3, false), Some((3, 2)));
    assert_eq!(params[2].component(1, true), Some((5, 1)));
    assert_eq!(params[2].channel(5, 2), Some((2, Some(false))));
    assert_eq!(params[3].channel(6, 3), Some((3, None)));
}

#[test]
fn partial_layout_requires_unique_count_and_channel_evidence() {
    let mut sig = [element(0, 3, 1), element(1, 3, 1), element(2, 3, 1), element(3, 3, 1)];
    sig[0].rw_mask = 0;
    sig[2].rw_mask = 0;
    let all = assign(&sig, &[], |_| true);
    let reads = BTreeSet::from([(0, 0, None), (2, 0, None)]);
    assert_eq!(unique_full_layout(&all, &sig, &reads, 3),
        Some(vec![Param::Full(1), Param::Full(2), Param::Full(3)]));
    // Reading only the final slot cannot distinguish the two unused inputs.
    assert_eq!(unique_full_layout(&all, &sig, &BTreeSet::from([(2, 0, None)]), 3), None);
    assert_eq!(unique_full_layout(&all, &sig, &reads, 2), None);
    assert_eq!(unique_full_layout(&all, &sig, &BTreeSet::from([(0, 1, None), (2, 0, None)]), 3), None);
    assert_eq!(unique_full_layout(&all, &sig, &BTreeSet::from([(0, 0, Some(false))]), 3), None);
    assert_eq!(unique_full_layout(&[Param::Half(vec![(1, 0)])], &sig, &BTreeSet::new(), 1), None);
}

#[test]
fn partial_layout_reports_distinct_failed_proofs() {
    let mut sig = [element(0, 3, 1), element(1, 3, 1), element(2, 3, 1), element(3, 3, 1)];
    sig[0].rw_mask = 0;
    sig[2].rw_mask = 0;
    let all = assign(&sig, &[], |_| true);
    let r = BTreeSet::from([(0, 0, None), (2, 0, None)]);
    assert!(unique_full_layout_reason(&all, &sig, &r, 3).is_ok());
    assert_eq!(unique_full_layout_reason(&all, &sig, &BTreeSet::from([(2, 0, None)]), 3), Err("ambiguous_layout"));
    assert_eq!(unique_full_layout_reason(&all, &sig, &r, 2), Err("attribute_outside_count"));
    assert_eq!(unique_full_layout_reason(&all, &sig, &BTreeSet::from([(0, 1, None)]), 3), Err("no_layout"));
    assert_eq!(unique_full_layout_reason(&all, &sig, &r, 5), Err("count_exceeds_signature"));
    assert_eq!(unique_full_layout_reason(&all, &sig, &BTreeSet::from([(0, 0, Some(false))]), 3), Err("half_read"));
    assert_eq!(unique_full_layout_reason(&[Param::Half(vec![(1, 0)])], &sig, &BTreeSet::new(), 1), Err("packed_half_layout"));
}
