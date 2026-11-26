// Synthetic gfx1030 NGG allocation with shared output count.
// Branch immediates are in dwords; only lane 0 emits a live triangle.
v_mbcnt_lo_u32_b32 v0, -1, 0
v_readfirstlane_b32 s0, v5
s_bfe_u32 s1, s3, 0x00040018
v_mbcnt_hi_u32_b32 v0, -1, v0
s_lshr_b32 s10, s0, 7
v_lshl_add_u32 v1, s1, 6, v0
s_mov_b32 s20, 3
v_mov_b32 v20, 0x80000000
v_mov_b32 v27, 0x00200400
v_cmp_eq_u32 vcc, 0, v1
v_cndmask_b32 v20, v20, v27, vcc
v_cmp_eq_u32 vcc, 2, v1
v_cndmask_b32 v21, -1.0, 3.0, vcc
v_cmp_eq_u32 vcc, 1, v1
v_cndmask_b32 v22, -1.0, 3.0, vcc
v_mov_b32 v23, 0.5
v_mov_b32 v24, 1.0
s_cmp_eq_u32 s20, 0
s_cselect_b64 s[0:1], exec, 0
s_cmpk_gt_u32 s20, 128
s_cselect_b64 s[2:3], exec, 0
s_or_b64 s[0:1], s[0:1], s[0:1]
s_or_b64 s[2:3], s[2:3], s[2:3]
s_or_b64 s[0:1], s[2:3], s[0:1]
v_cndmask_b32_e64 v10, 0, 1, s[0:1]
v_readfirstlane_b32 s0, v10
s_and_b32 s0, 1, s0
s_cmp_eq_u32 s0, 1
s_mov_b64 s[0:1], exec
s_cselect_b64 s[2:3], exec, 0
v_cndmask_b32_e64 v11, s20, 1, s[2:3]
s_xor_b64 s[4:5], s[2:3], -1
v_readfirstlane_b32 s6, v11
v_cmpx_eq_u32 0, v1
s_cbranch_execz 13
s_lshl_b32 s7, s6, 12
s_andn2_b64 vcc, exec, s[2:3]
s_or_b32 s6, s7, s6
s_mov_b32 m0, s6
s_sendmsg sendmsg(MSG_GS_ALLOC_REQ)
s_waitcnt 0
s_cbranch_vccnz 6
s_barrier
v_bfrev_b32 v10, 1
exp pos0 off, off, off, off done
exp prim v10, off, off, off done
s_waitcnt 0xff0f
s_mov_b64 exec, s[0:1]
s_barrier
v_cmp_gt_u32 vcc, s20, v1
s_and_b64 s[0:1], vcc, s[4:5]
s_and_saveexec_b64 s[6:7], s[0:1]
s_cbranch_execz 18
v_bfe_u32 v12, v20, 10, 10
v_bfe_u32 v13, v20, 20, 10
v_and_b32 v14, 0x3ff, v20
v_cmp_le_u32 vcc, s20, v12
v_cmp_le_u32_e64 s[2:3], s20, v13
v_cmp_le_u32_e64 s[0:1], s20, v14
s_or_b64 s[2:3], vcc, s[2:3]
s_or_b64 s[0:1], s[0:1], s[2:3]
v_cndmask_b32_e64 v15, v20, 0x80000000, s[0:1]
exp prim v15, off, off, off done
s_waitcnt 0xff0f
s_mov_b64 exec, s[6:7]
v_cmp_gt_u32 vcc, s20, v1
s_and_b64 s[0:1], vcc, s[4:5]
s_and_saveexec_b64 s[0:1], s[0:1]
s_cbranch_execz 2
exp pos0 v21,v22,v23,v24 done
s_endpgm
