#!/usr/bin/env python3
"""Check on a real GPU the translation that runs two console lanes per host thread.

Each synthetic wave64 program uses lane masks, lane reads, row exchanges, shared
memory or interleaved loops. It is translated for 32-lane waves and run, and
every lane's registers are compared with a model of the console: one program
counter and one set of scalar registers per wave, vector writes under EXEC.
Only the translator's input and output are instrumented. No game data is needed.
"""
import argparse
import json
from pathlib import Path
import random
import re
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from frosty_gpu_guest import run_gpu_guest  # noqa: E402
from project_tuxbox.dxil_store import COMPUTE_FLAGS  # noqa: E402
from project_tuxbox.inspect import digest  # noqa: E402

OUTPUTS = ("v10", "v11", "s20", "s21")
M32, M64 = 0xffffffff, (1 << 64) - 1
WAVES = 256
START = ['v_lshrrev_b32_e32 v3, 31, v0', 'v_and_b32_e32 v0, 0x7fffffff, v0', 'v_cmpx_ne_u32_e32 0, v3']

PROGRAMS = {
    'compare': ['v_cmp_lt_u32_e32 vcc, 0x3e8, v0', 's_bcnt1_i32_b64 s20, vcc', 's_ff1_i32_b64 s21, vcc',
                'v_cndmask_b32_e32 v10, 5, v0, vcc', 'v_cmp_lt_u32_e64 s[30:31], v1, v0',
                'v_cndmask_b32_e64 v11, v1, v0, s[30:31]'],
    'saveexec': ['v_cmp_lt_u32_e32 vcc, 0x3e8, v0', 's_and_saveexec_b64 s[10:11], vcc', ('branch', 's_cbranch_execz', 'end'),
                 'v_mbcnt_lo_u32_b32 v10, exec_lo, 0', 'v_mbcnt_hi_u32_b32 v10, exec_hi, v10',
                 'v_readfirstlane_b32 s20, v0', 's_bcnt1_i32_b64 s21, exec', 'v_add_nc_u32_e32 v11, s20, v1',
                 ('label', 'end'), 's_mov_b64 exec, s[10:11]'],
    'mbcnt': ['v_cmp_lt_u32_e64 s[30:31], 0x3e8, v0', 'v_mbcnt_lo_u32_b32 v10, s30, 100', 'v_mbcnt_hi_u32_b32 v10, s31, v10',
              'v_mbcnt_lo_u32_b32 v11, -1, 0', 'v_mbcnt_hi_u32_b32 v11, -1, v11'],
    'waterfall': ['v_and_b32_e32 v4, 7, v0', 's_mov_b64 s[10:11], exec', ('label', 'again'),
                  'v_readfirstlane_b32 s20, v4', 'v_cmp_eq_u32_e32 vcc, s20, v4', 's_and_saveexec_b64 s[12:13], vcc',
                  's_bcnt1_i32_b64 s14, exec', 'v_mbcnt_lo_u32_b32 v5, exec_lo, 0', 'v_mbcnt_hi_u32_b32 v5, exec_hi, v5',
                  'v_lshlrev_b32_e32 v6, 8, v10', 'v_add_nc_u32_e32 v10, v6, v5', 'v_add_nc_u32_e32 v11, s14, v11',
                  's_add_u32 s21, s21, 1', 's_andn2_b64 exec, s[12:13], exec', ('branch', 's_cbranch_execnz', 'again'),
                  's_mov_b64 exec, s[10:11]'],
    'cmpx_carry': ['v_cmpx_lt_u32_e32 0x64, v0', 'v_add_co_u32 v10, s[30:31], v0, 0xfffffc00',
                   'v_add_co_ci_u32_e64 v11, s[32:33], v1, 7, s[30:31]', 's_bcnt1_i32_b64 s20, s[30:31]',
                   's_bcnt1_i32_b64 s21, exec'],
    'lanes': ['v_readlane_b32 s20, v0, 3', 'v_readlane_b32 s14, v0, 47', 's_add_u32 s20, s20, s14',
              'v_writelane_b32 v10, s14, 5', 'v_writelane_b32 v10, s20, 40', 'v_readfirstlane_b32 s21, v1',
              'v_readlane_b32 s15, v1, s21', 'v_add_nc_u32_e32 v11, s15, v0'],
    'dpp8': ['v_mov_b32_e32 v10, 11', 'v_mov_b32_e32 v11, 22', 'v_add_nc_u32 v10, v0, v1 dpp8:[1,0,3,2,5,4,7,6]',
             'v_max_u32 v11, v0, v0 dpp8:[7,7,7,7,7,7,7,7]'],
    'row_shift': ['v_mov_b32_e32 v10, 11', 'v_mov_b32_e32 v11, 22',
                  'v_mov_b32_dpp v10, v0 row_shr:1 row_mask:0xf bank_mask:0xf bound_ctrl:1',
                  'v_mov_b32_dpp v11, v0 row_shl:3 row_mask:0xf bank_mask:0xf'],
    'row_masks': ['v_mov_b32_e32 v10, 11', 'v_mov_b32_e32 v11, 22',
                  'v_add_nc_u32_dpp v10, v0, v1 row_ror:5 row_mask:0xa bank_mask:0xf',
                  'v_or_b32_dpp v11, v0, v1 quad_perm:[3,2,1,0] row_mask:0xf bank_mask:0x5'],
    'permlane': ['v_mov_b32_e32 v10, 11', 'v_mov_b32_e32 v11, 22', 's_mov_b32 s30, 0x76543210', 's_mov_b32 s31, 0xfedcba98',
                 'v_permlanex16_b32 v10, v0, s30, s31', 's_mov_b32 s30, 0', 'v_permlanex16_b32 v11, v0, s30, s30'],
    'swizzle': ['v_mov_b32_e32 v10, 11', 'v_mov_b32_e32 v11, 22', 'v_cmp_lt_u32_e32 vcc, 0x3e8, v0',
                's_and_saveexec_b64 s[10:11], vcc', 'ds_swizzle_b32 v10, v0 offset:0x801b',
                'ds_swizzle_b32 v11, v0 offset:0x041f', 's_mov_b64 exec, s[10:11]'],
    'empty': ['v_cmpx_lt_u32_e32 0x100000, v0', ('branch', 's_cbranch_execz', 'skip'), 's_mov_b32 s20, 9',
              ('label', 'skip'), 'v_cmp_lt_u32_e32 vcc, 0, v1', ('branch', 's_cbranch_vccz', 'done'),
              's_mov_b32 s21, 4', ('label', 'done'), 'v_readfirstlane_b32 s14, v1', 'v_mov_b32_e32 v10, 1'],
    'dispatch': ['v_cmp_lt_u32_e32 vcc, 0x3e8, v0', 'v_mov_b32_e32 v10, 0', 's_mov_b32 s20, 0', 's_mov_b32 s21, 0', 's_mov_b32 s22, 0',
                 ('label', 'outer'), 's_add_u32 s20, s20, 1', ('label', 'inner'), ('branch', 's_cbranch_vccz', 'quiet'),
                 's_add_u32 s21, s21, 1', ('label', 'quiet'), 's_and_saveexec_b64 s[10:11], vcc', ('branch', 's_cbranch_execz', 'none'),
                 'v_readfirstlane_b32 s14, v0', 'v_add_nc_u32_e32 v10, s14, v10', ('label', 'none'), 's_mov_b64 exec, s[10:11]',
                 's_add_u32 s22, s22, 1', 's_cmp_lt_u32 s20, 3', ('branch', 's_cbranch_scc1', 'outer'),
                 's_cmp_lt_u32 s22, 6', ('branch', 's_cbranch_scc1', 'inner')],
    # Every lane publishes a value, waits, then reads one from the other half of the wave.
    'shared': ['v_mov_b32_e32 v10, 11', 'v_mov_b32_e32 v11, 22', 'v_lshlrev_b32_e32 v4, 2, v1', 'ds_write_b32 v4, v0', 's_barrier',
               'v_xor_b32_e32 v5, 63, v1', 'v_lshlrev_b32_e32 v5, 2, v5', 'ds_read_b32 v10, v5', 's_barrier',
               'v_mov_b32_e32 v6, 0x400', 'v_mov_b32_e32 v7, 0', 'ds_write_b32 v6, v7', 's_barrier', 'ds_add_u32 v6, v1', 's_barrier',
               'ds_read_b32 v11, v6'],
    'masks': ['v_cmp_lt_u32_e32 vcc, 0x3e8, v0', 's_not_b64 s[14:15], vcc', 's_and_b64 s[14:15], s[14:15], exec',
              's_bcnt1_i32_b64 s20, s[14:15]', 's_ff1_i32_b64 s21, s[14:15]', 's_or_saveexec_b64 s[10:11], -1',
              'v_cndmask_b32_e64 v10, 0, v1, s[14:15]', 's_mov_b64 exec, s[10:11]', 'v_add_nc_u32_e32 v11, 1, v1'],
}


PROGRAMS['carry_chain'] = [
    'v_mov_b32_e32 v10, v0',
    'v_mov_b32_e32 v11, 0xffffffff',
    'v_add_co_u32 v10, s[30:31], v10, 0xfffffc00',
] + [
    'v_add_co_ci_u32_e64 v10, s[30:31], v10, v11, s[30:31]',
    'v_add_co_ci_u32_e64 v11, vcc, v11, v10, s[30:31]',
    'v_add_co_ci_u32_e64 v10, s[30:31], v0, v10, vcc',
] * 16 + ['s_bcnt1_i32_b64 s20, s[30:31]', 's_bcnt1_i32_b64 s21, vcc']

PROGRAMS['mad64_chain'] = [
    'v_mov_b32_e32 v10, v0',
    'v_mov_b32_e32 v11, 0xffffffff',
] + [
    'v_mad_u64_u32 v[10:11], s[30:31], v0, v1, v[10:11]',
    'v_mad_u64_u32 v[10:11], vcc, v10, v11, v[10:11]',
] * 16 + ['s_bcnt1_i32_b64 s20, s[30:31]', 's_bcnt1_i32_b64 s21, vcc']


PROGRAMS['exchange_chain'] = [
    'v_mov_b32_e32 v10, v0', 'v_mov_b32_e32 v11, v1',
    's_mov_b32 s30, 0x76543210', 's_mov_b32 s31, 0xfedcba98',
] + [
    'v_add_nc_u32 v10, v10, v11 dpp8:[1,0,3,2,5,4,7,6]',
    'v_mov_b32_dpp v11, v10 row_shr:1 row_mask:0xf bank_mask:0xf bound_ctrl:1',
    'v_permlanex16_b32 v10, v11, s30, s31',
    'ds_swizzle_b32 v11, v10 offset:0x041f',
] * 16


PROGRAMS['direct_alu_chain'] = [
    'v_mov_b32_e32 v10, v0', 'v_mov_b32_e32 v11, v1',
    's_mov_b32 s30, 0x12345678',
] + [
    'v_add_nc_u32_e32 v10, s30, v10',
    'v_sub_nc_u32_e32 v11, v10, v11',
    'v_subrev_nc_u32_e32 v10, v11, v10',
    'v_not_b32_e32 v11, v11',
    'v_xor_b32_e32 v10, v10, v11',
    'v_lshlrev_b32_e32 v10, 5, v10',
    'v_lshrrev_b32_e32 v11, v1, v11',
    'v_or_b32_e32 v11, v10, v11',
    'v_and_b32_e32 v10, 0x7fffffff, v10',
    'v_add_nc_u32 v10, v10, v11 dpp8:[1,0,3,2,5,4,7,6]',
    'v_mov_b32_dpp v11, v10 row_shr:1 row_mask:0xa bank_mask:0x5 bound_ctrl:1',
] * 8


PROGRAMS['unsigned_compare_chain'] = [
    'v_mov_b32_e32 v10, v0', 'v_mov_b32_e32 v11, v1',
    's_mov_b32 s40, 0x12345678',
] + [
    item
    for op in ('eq', 'ne', 'lt', 'le', 'gt', 'ge')
    for item in (
        f'v_cmp_{op}_u32_e32 vcc, s40, v10',
        'v_cndmask_b32_e32 v10, v11, v10, vcc',
        f'v_cmp_{op}_u32_e64 s[30:31], v10, v11',
        'v_cndmask_b32_e64 v11, v10, v11, s[30:31]',
        'v_add_nc_u32_e32 v10, v1, v10',
        's_bcnt1_i32_b64 s20, vcc',
        's_bcnt1_i32_b64 s21, s[30:31]',
    )
] * 4


# Exercise the interactions of all independently reviewed optimizations.
PROGRAMS['combined_chain'] = sum((PROGRAMS[name] for name in (
    'direct_alu_chain', 'carry_chain', 'mad64_chain',
    'unsigned_compare_chain', 'exchange_chain',
)), [])


def assemble(llvm_mc, lines):
    result = subprocess.run([str(llvm_mc), "-triple=amdgcn-amd-amdhsa", "-mcpu=gfx1030",
                             "-mattr=-wavefrontsize32,+wavefrontsize64", "-show-encoding"],
                            input="\n".join(lines) + "\n", text=True, capture_output=True, check=True, timeout=20)
    encoded = re.findall(r"encoding: \[([^\]]+)\]", result.stdout)
    if len(encoded) != len(lines):
        raise ValueError("LLVM encoding count differs")
    return [list(struct.unpack(f"<{len(e.split(',')) // 4}I", bytes(int(v, 16) for v in e.split(","))))
            for e in encoded]


def words_of(llvm_mc, items):
    """The native words of a program, with branch offsets resolved to its labels."""
    encoded = iter(assemble(llvm_mc, [i for i in items if isinstance(i, str)]))
    sizes = [next(encoded) if isinstance(i, str) else i for i in items]
    labels, position = {}, 0
    for item, size in zip(items, sizes):
        if isinstance(item, str):
            position += len(size)
        elif item[0] == "label":
            labels[item[1]] = position
        else:
            position += 1
    out = []
    for item, size in zip(items, sizes):
        if isinstance(item, str):
            out += size
        elif item[0] == "branch":
            word = assemble(llvm_mc, [f"{item[1]} 0"])[0][0]
            out.append(word | ((labels[item[2]] - len(out) - 1) & 0xffff))
    return out + [0xbf810000]


def split(text):
    return [t for t in re.split(r',\s*(?![^\[]*\])', text) if t]


class Wave:
    """One console wave: shared scalars and masks, per-lane vector registers."""
    def __init__(self, inputs, group):
        self.s = [0] * 128
        self.vcc, self.exec, self.scc = 0, M64, 0
        self.lds = {}
        self.v = [[0] * 64 for _ in range(64)]
        self.v[0], self.v[1], self.v[2] = list(inputs), list(range(64)), [group] * 64

    def scalar(self, token, wide=False):
        if token == 'exec': return self.exec
        if token == 'vcc': return self.vcc
        if token == 'exec_lo': return self.exec & M32
        if token == 'exec_hi': return self.exec >> 32
        if m := re.fullmatch(r's\[(\d+):(\d+)\]', token): return self.s[int(m[1])] | self.s[int(m[2])] << 32
        if m := re.fullmatch(r's(\d+)', token):
            n = int(m[1]); return self.s[n] | (self.s[n + 1] << 32 if wide else 0)
        value = int(token, 0)
        return value & (M64 if wide else M32)

    def store(self, token, value):
        if token == 'exec': self.exec = value & M64
        elif token == 'vcc': self.vcc = value & M64
        elif m := re.fullmatch(r's\[(\d+):(\d+)\]', token):
            self.s[int(m[1])], self.s[int(m[2])] = value & M32, (value >> 32) & M32
        else: self.s[int(token[1:])] = value & M32

    def value(self, token, lane):
        if m := re.fullmatch(r'v(\d+)', token): return self.v[int(m[1])][lane]
        return self.scalar(token)

    def active(self, lane): return (self.exec >> lane) & 1


def step(w, text):
    """Execute one instruction. DPP modifiers choose which lane the first source comes from."""
    modifiers = {}
    if m := re.search(r' dpp8:\[([\d,]+)\]', text):
        modifiers['dpp8'] = [int(x) for x in m[1].split(',')]; text = text[:m.start()]
    elif '_dpp ' in text:
        body = re.search(r' (row_shr|row_shl|row_ror|quad_perm):', text)
        tail, text = text[body.start():], text[:body.start()]
        modifiers['dpp16'] = dict(re.findall(r'(\w+):(\[[\d,]+\]|\w+)', tail))
    name, _, rest = text.partition(' ')
    if name == 's_barrier': return
    ops = split(rest)
    base = re.sub(r'_(e32|e64|dpp)$', '', name)
    if base.startswith('s_'):
        if base in ('s_mov_b32', 's_mov_b64'): w.store(ops[0], w.scalar(ops[1], base.endswith('64')))
        elif base == 's_add_u32':
            total = w.scalar(ops[1]) + w.scalar(ops[2]); w.store(ops[0], total); w.scc = int(total > M32)
        elif base == 's_bcnt1_i32_b64':
            n = bin(w.scalar(ops[1], True)).count('1'); w.store(ops[0], n); w.scc = int(n != 0)
        elif base == 's_barrier': pass
        elif base == 's_cmp_lt_u32': w.scc = int(w.scalar(ops[0]) < w.scalar(ops[1]))
        elif base == 's_ff1_i32_b64':
            x = w.scalar(ops[1], True); w.store(ops[0], ((x & -x).bit_length() - 1) & M32)
        elif base == 's_not_b64':
            x = ~w.scalar(ops[1], True) & M64; w.store(ops[0], x); w.scc = int(x != 0)
        elif base in ('s_and_b64', 's_or_b64', 's_andn2_b64'):
            a, b = w.scalar(ops[1], True), w.scalar(ops[2], True)
            x = a & b if base == 's_and_b64' else a | b if base == 's_or_b64' else a & ~b & M64
            w.store(ops[0], x); w.scc = int(x != 0)
        elif base in ('s_and_saveexec_b64', 's_or_saveexec_b64'):
            source, old = w.scalar(ops[1], True), w.exec
            w.store(ops[0], old); w.exec = source & old if 'and' in base else source | old; w.scc = int(w.exec != 0)
        else: raise ValueError('model lacks ' + name)
        return
    if base == 'ds_swizzle_b32':
        off = int(re.search(r'offset:(\S+)', ops[1])[1], 0)
        src = ops[1].split()[0]
        old = [w.value(src, l) if w.active(l) else 0 for l in range(64)]
        for l in range(64):
            if not w.active(l): continue
            if off & 0x8000:
                partner = (l & ~3) | (((off & 0xff) >> (2 * (l & 3))) & 3)
            else:
                partner = (l & ~31) | (((l & (off & 31)) | ((off >> 5) & 31)) ^ ((off >> 10) & 31))
            w.v[int(ops[0][1:])][l] = old[partner]
        return
    if base.startswith('ds_'):
        # One wave per group: its lanes are all of the group.
        for l in range(64):
            if not w.active(l): continue
            if base == 'ds_write_b32': w.lds[w.value(ops[0], l)] = w.value(ops[1], l)
            elif base == 'ds_add_u32':
                a = w.value(ops[0], l); w.lds[a] = (w.lds.get(a, 0) + w.value(ops[1], l)) & M32
        if base == 'ds_read_b32':
            for l in range(64):
                if w.active(l): w.v[int(ops[0][1:])][l] = w.lds.get(w.value(ops[1], l), 0)
        return
    if base == 'v_readfirstlane_b32':
        lane = next((l for l in range(64) if w.active(l)), 0); w.store(ops[0], w.value(ops[1], lane)); return
    if base == 'v_readlane_b32':
        w.store(ops[0], w.value(ops[1], w.scalar(ops[2]) & 63)); return
    if base == 'v_writelane_b32':
        w.v[int(ops[0][1:])][w.scalar(ops[2]) & 63] = w.scalar(ops[1]); return
    if base.startswith('v_cmp'):
        cmpx = base.startswith('v_cmpx')
        if cmpx: dst, a, b = 'exec', ops[-2], ops[-1]
        else: dst, a, b = ops
        test = {'lt': lambda x, y: x < y, 'le': lambda x, y: x <= y, 'gt': lambda x, y: x > y,
                'ge': lambda x, y: x >= y, 'eq': lambda x, y: x == y, 'ne': lambda x, y: x != y}[base.split('_')[2]]
        mask = sum(1 << l for l in range(64) if w.active(l) and test(w.value(a, l), w.value(b, l)))
        w.store(dst, mask); return
    if base == 'v_permlanex16_b32':
        low, high = w.scalar(ops[2]), w.scalar(ops[3]); old = [w.value(ops[1], l) for l in range(64)]
        for l in range(64):
            i = l & 15; sel = ((low if i < 8 else high) >> (4 * (i & 7))) & 15
            source = ((l & ~15) ^ 16) | sel
            if w.active(l) and w.active(source): w.v[int(ops[0][1:])][l] = old[source]
        return
    if base == 'v_mad_u64_u32':
        dst, high = map(int, re.fullmatch(r'v\[(\d+):(\d+)\]', ops[0]).groups())
        a, b = map(int, re.fullmatch(r'v\[(\d+):(\d+)\]', ops[4]).groups())
        mask = 0
        for lane in range(64):
            if w.active(lane):
                total = w.value(ops[2], lane) * w.value(ops[3], lane) + (w.v[a][lane] | w.v[b][lane] << 32)
                w.v[dst][lane], w.v[high][lane] = total & M32, (total >> 32) & M32
                mask |= int(total > M64) << lane
        w.store(ops[1], mask)
        return
    dst = int(ops[0][1:])
    if base in ('v_add_co_u32', 'v_add_co_ci_u32'):
        carry_out, a, b = ops[1], ops[2], ops[3]
        carry_in = w.scalar(ops[4], True) if base == 'v_add_co_ci_u32' else 0
        mask = 0
        for l in range(64):
            if w.active(l):
                total = w.value(a, l) + w.value(b, l) + ((carry_in >> l) & 1)
                w.v[dst][l] = total & M32; mask |= int(total > M32) << l
        w.store(carry_out, mask); return
    first = [w.value(ops[1], l) for l in range(64)]
    others = [[w.value(op, l) for l in range(64)] for op in ops[2:] if op != 'vcc']
    select = w.vcc if base == 'v_cndmask_b32' and (len(ops) < 4 or ops[3] == 'vcc') else (
        w.scalar(ops[3], True) if base == 'v_cndmask_b32' else 0)
    results = {}
    for l in range(64):
        if not w.active(l): continue
        a = first[l]
        if 'dpp8' in modifiers:
            source = (l & ~7) | modifiers['dpp8'][l & 7]
            a = first[source] if w.active(source) else 0
        if 'dpp16' in modifiers:
            m = modifiers['dpp16']
            rows, banks = int(m.get('row_mask', '0xf'), 0), int(m.get('bank_mask', '0xf'), 0)
            zero = int(m.get('bound_ctrl', '0'), 0)
            position = l & 15; inside = True
            if 'row_shr' in m: n = int(m['row_shr']); inside = position >= n; source = l - n
            elif 'row_shl' in m: n = int(m['row_shl']); inside = position + n < 16; source = l + n
            elif 'row_ror' in m: n = int(m['row_ror']); source = (l & ~15) | ((position - n) & 15)
            else: source = (l & ~3) | [int(x) for x in m['quad_perm'].strip('[]').split(',')][l & 3]
            valid = inside and bool(w.active(source & 63))
            if not (rows >> (l >> 4)) & (banks >> (l & 3)) & 1 or not (valid or zero): continue
            a = first[source] if valid else 0
        x = [o[l] for o in others]
        results[l] = {'v_mov_b32': lambda: a, 'v_add_nc_u32': lambda: a + x[0],
                      'v_sub_nc_u32': lambda: a - x[0], 'v_subrev_nc_u32': lambda: x[0] - a,
                      'v_not_b32': lambda: ~a, 'v_and_b32': lambda: a & x[0],
                      'v_or_b32': lambda: a | x[0], 'v_xor_b32': lambda: a ^ x[0], 'v_max_u32': lambda: max(a, x[0]),
                      'v_lshlrev_b32': lambda: x[0] << (a & 31), 'v_lshrrev_b32': lambda: x[0] >> (a & 31),
                      'v_cndmask_b32': lambda: x[0] if (select >> l) & 1 else a,
                      'v_mbcnt_lo_u32_b32': lambda: x[0] + bin(a & ((1 << min(l, 32)) - 1)).count('1'),
                      'v_mbcnt_hi_u32_b32': lambda: x[0] + bin(a & ((1 << max(l - 32, 0)) - 1)).count('1')}[base]() & M32
    for l, result in results.items(): w.v[dst][l] = result


def model(items, inputs, group):
    w = Wave(inputs, group)
    labels = {item[1]: at for at, item in enumerate(items) if not isinstance(item, str) and item[0] == 'label'}
    at = steps = 0
    while at < len(items):
        item = items[at]; at += 1
        if isinstance(item, str): step(w, item)
        elif item[0] == 'branch':
            taken = {'s_cbranch_execz': w.exec == 0, 's_cbranch_execnz': w.exec != 0, 's_cbranch_vccz': w.vcc == 0,
                     's_cbranch_vccnz': w.vcc != 0, 's_cbranch_scc0': w.scc == 0, 's_cbranch_scc1': w.scc == 1}[item[1]]
            if taken: at = labels[item[2]]
        steps += 1
        assert steps < 10000, 'model did not finish'
    return [[w.v[int(r[1:])][l] if r[0] == 'v' else w.s[int(r[1:])] for r in OUTPUTS] for l in range(64)]


def instrument(hlsl, threads):
    start = re.search(r'^    s\d+ = gid\.x; s\d+ = gid\.y; s\d+ = gid\.z;\n', hlsl, re.M)
    assert start and '[WaveSize(32)]' in hlsl, 'unexpected prologue'
    index = [f'(gid.x * {threads}u + tb_first + tb_pair + {32 * h}u)' for h in (0, 1)]
    load = ''.join(f'    v0__{h} = tb_in.Load({index[h]} * 4u); v1__{h} = tb_pair + {32 * h}u; '
                   f'v2__{h} = {index[h]} / 64u;\n' for h in (0, 1))
    stores = ''
    for column, register in enumerate(OUTPUTS):
        for h in (0, 1):
            name = f'{register}__{h}' if register[0] == 'v' else register
            value = name if re.search(rf'\b{name} = ', hlsl) else '0u'
            stores += f'    tb_out.Store(({index[h]} * 4u + {column}u) * 4u, {value});\n'
    hlsl = hlsl[:start.end()] + load + hlsl[start.end():]
    end = hlsl.rstrip().rfind('}')
    return ('ByteAddressBuffer tb_in : register(t0);\nRWByteAddressBuffer tb_out : register(u0);\n'
            + hlsl[:end] + stores + '}\n')


def inputs(rng):
    words = []
    for wave in range(WAVES):
        density = rng.choice((1.0, 1.0, 0.8, 0.3, 0.05))
        empty = rng.choice((None, None, None, None, 0, 1, 2))
        top = rng.choice((8, 2000, 2000, 2000, 0x7fffffff))
        for lane in range(64):
            on = empty != 2 and rng.random() < density and lane // 32 != empty
            words.append((int(on) << 31) | rng.randrange(top))
    return words


def selected():
    """Each program in one console wave per group, and some in four."""
    single = [(name, body, 64) for name, body in PROGRAMS.items()]
    return single + [(f"{name}_x4", PROGRAMS[name], 256) for name in ("saveexec", "waterfall", "permlane", "dispatch", "carry_chain", "mad64_chain",
                           "dpp8", "row_shift", "row_masks", "swizzle", "exchange_chain",
                           "direct_alu_chain", "unsigned_compare_chain", "combined_chain")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("emitter", "llvm-mc", "dxc", "dxv", "provider", "proton", "prefix", "compiler-dir", "work",
                 "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--adapter", required=True)
    args = parser.parse_args(argv)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    rng = random.Random(11)
    with TemporaryDirectory(dir=work) as tmp:
        trial = Path(tmp)
        expected, shaders, cases = {}, {}, []
        for name, body, threads in selected():
            items = START + body
            emitted = subprocess.check_output(
                [args.emitter.resolve(), str(threads), *(f"{w:08x}" for w in words_of(args.llvm_mc, items))],
                text=True, timeout=60)
            source, blob = trial / f"{name}.hlsl", trial / f"{name}.cso"
            source.write_text(instrument(emitted, threads))
            subprocess.run([args.dxc, "-T", "cs_6_6", "-E", "main", *COMPUTE_FLAGS, "-Fo", blob, source],
                           check=True, timeout=60, capture_output=True)
            subprocess.run([args.dxv, blob], check=True, timeout=60, capture_output=True)
            shaders[name] = dict(hlsl_sha256=digest(source), dxil_sha256=digest(blob))
            data = inputs(rng)
            # Every lane publishes before any reads in the shared memory program.
            if name.startswith("shared"):
                data = [word | 0x80000000 for word in data]
            (trial / f"{name}.input").write_bytes(struct.pack(f"<{len(data)}I", *data))
            expected[name] = [model(items, data[w * 64:w * 64 + 64], w) for w in range(WAVES)]
            cases.append(f"{name} {name} {name}.input {WAVES * 64 // threads} {WAVES * 64}\n")
        (trial / "cases.txt").write_text("".join(cases))
        exe, status, fields = run_gpu_guest(
            ROOT / "tools/reference/independent-shaders/wave_pairs.cpp", "wave-pairs.exe", trial, work,
            args.proton.resolve(), args.prefix, args.provider, args.compiler_dir)
        wrong = {}
        for name, waves in expected.items():
            path = trial / f"{name}.bin"
            actual = path.read_bytes() if path.is_file() else b""
            if len(actual) != WAVES * 64 * 16:
                wrong[name] = "no readback"
                continue
            got = struct.unpack(f"<{WAVES * 256}I", actual)
            for index, value in enumerate(got):
                wave, lane, column = index // 256, index // 4 % 64, index % 4
                if value != waves[wave][lane][column]:
                    wrong[name] = dict(wave=wave, lane=lane, register=OUTPUTS[column], got=value,
                                       wanted=waves[wave][lane][column])
                    break
        passed = status == 0 and fields.get("adapter") == args.adapter and not wrong
        report = dict(passed=passed, scope="Synthetic wave64 programs on 32-lane host waves",
                      adapter=fields.get("adapter"), guest_exit=status, cases=len(expected),
                      compared_words=len(expected) * WAVES * 256, wrong=wrong, shaders=shaders,
                      emitter_sha256=digest(args.emitter), guest_sha256=digest(exe))
        if fields.get("error"):
            report["error"] = fields["error"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    for name, detail in wrong.items():
        print(f"WRONG {name}: {detail}")
    if not passed:
        print(f"FAIL: {len(wrong)} of {len(expected)} programs, guest exit {status}")
        return 1
    print(f"PASS: {len(expected)} programs, {report['compared_words']} words, adapter {fields['adapter']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
