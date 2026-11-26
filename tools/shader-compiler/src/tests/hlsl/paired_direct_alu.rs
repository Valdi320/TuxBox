use super::direct_alu_expr;

#[test]
fn expression_grammar_does_not_rewrite_masks_or_effectful_forms() {
    assert_eq!(direct_alu_expr("(v10 + s1)", 1, false).as_deref(), Some("(v10__1 + s1)"));
    assert_eq!(direct_alu_expr("tb_s60000 ^ 0xffu", 0, true).as_deref(), Some("tbd_0 ^ 0xffu"));
    for code in ["exec_lo", "vcc_lo", "tb_s60000", "asuint(v0)", "v0++", "v1 = 1u",
                 "v0[1]", "v0.x", "read(v0)", "v10; v11", "scc"] {
        assert!(direct_alu_expr(code, 1, false).is_none(), "{code}");
    }
}
