"""The synthetic Xbox compute shaders the Lanes self-test loads.

All are small hand-written console programs, not taken from any game. One is a
`v_mov_b32` and `s_endpgm` with an empty root signature embedded. Another sums
the lane numbers of each 64-lane console wave and writes a word per thread. The
two `compute-root` programs carry no root and store one word through table 1.
"""
import hashlib
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from examples.lanes.raster_fixture import container as stage_container, image  # noqa: E402
from tools.reference.scarlett_worker_fixture import container, pack, uav_shader  # noqa: E402

CODE = (0x7E0002FF, 0x12345678, 0xBF810000)
# Root signature 1.0 with no parameters, samplers or flags.
EMPTY_ROOT = pack([1, 0, 24, 0, 24, 0])


def shader():
    base = container(list(CODE))
    count = struct.unpack_from("<I", base, 28)[0]
    offsets = struct.unpack_from(f"<{count}I", base, 32)
    chunks = [base[start:start + 8 + struct.unpack_from("<I", base, start + 4)[0]] for start in offsets]
    chunks.append(b"RTS0" + pack([len(EMPTY_ROOT)]) + EMPTY_ROOT)
    start = 32 + 4 * len(chunks)
    table = []
    for chunk in chunks:
        table.append(start)
        start += len(chunk)
    return b"DXBC" + bytes(16) + pack([1, start, len(chunks), *table]) + b"".join(chunks)


# Two console waves in one group. v0 is the thread number.
WAVE_THREADS = 128
WAVE_CODE = (
    0xF4080402, 0xFA000000,  # s_load_dwordx4 s[16:19], s[4:5]: the u0 descriptor
    0xBF8CC07F,  # s_waitcnt lgkmcnt(0)
    # The 64-lane sum of v0, left in s7: an EXEC-saving prologue, DPP and
    # cross-row exchanges, then reads of lanes 31 and 63.
    0xBE8025C1, 0xD5010002, 0x00020080, 0x4A0404E9, 0xDE54C102, 0x4A0404E9, 0x93601202, 0x4A0404E9, 0x00092402,
    0x4A0404FA, 0xFF094002, 0xD7780003, 0x02010102, 0x4A040503, 0xD7600006, 0x00014102, 0x4A040406, 0xD7600007,
    0x00010102, 0xBEFE0400,
    0x4A080007,  # v_add_nc_u32 v4, s7, v0
    0x340A0082,  # v_lshlrev_b32 v5, 2, v0
    0xE0701000, 0x80040405,  # buffer_store_dword v4, v5, s[16:19], offen
    0xBF810000,  # s_endpgm
)


def wave_shader():
    return uav_shader(list(WAVE_CODE), width=WAVE_THREADS)


def wave_expected():
    """The word each thread writes: its number plus the sum over its wave."""
    return [n + sum(range(n & ~63, (n & ~63) + 64)) for n in range(WAVE_THREADS)]


# The `compute-root` programs, checked with `llvm-mc -mcpu=gfx1030 -show-encoding`.
# Each stores ROOT_EXPECTED through the descriptor at a byte offset of table 1,
# whose pointer arrives in s5. Their FSSM record and PSV binding name it a raw
# UAV u3, space2, which the scenario's root does not declare: a translation
# without that root cannot be bound. The root declares u0-u3, space1 in table 1,
# so slot 2 (0x40) is u2, space1 there, and slot 5 (0xa0) is past the table.
ROOT_EXPECTED = 0x7AB1E002
ROOT_THREADS = 64
ROOT_RESOURCES = ([7, 2, 3, 3],)  # PSV binding [type, space, lower, upper]: raw UAV


def root_code(offset):
    return (
        0xBE800305,                       # s_mov_b32 s0, s5
        0xBE810380,                       # s_mov_b32 s1, 0
        0xF4080200, 0xFA000000 | offset,  # s_load_dwordx4 s[8:11], s[0:1], offset
        0xBF8CC07F,                       # s_waitcnt lgkmcnt(0)
        0x7E0002FF, ROOT_EXPECTED,        # v_mov_b32 v0, ROOT_EXPECTED
        0xE0700000, 0x80020000,           # buffer_store_dword v0, off, s[8:11], 0
        0xBF810000,                       # s_endpgm
    )


def root_shader(offset):
    """A root-less compute program storing through table 1 at `offset` bytes."""
    info = bytearray(48)
    info[24] = 5
    struct.pack_into("<3I", info, 36, ROOT_THREADS, 1, 1)
    psv = pack([len(info)]) + info + pack([len(ROOT_RESOURCES), 16, *ROOT_RESOURCES[0]])
    # FSSM row [class, dword offset, register, shape, location]: a raw UAV,
    # register 3, in table 1 (s5).
    used = ([0x303, offset // 4, 3, 1, 0x1_0504],)
    return stage_container(0x50060, image(7, root_code(offset), (), used), [(b"PSV0", psv)])


def recovered_root_shader():
    """Two stores, one in an original table and one in an uncovered second-table slot.

    Only the second descriptor has a corroborating user-data record: whole
    reflection fails, so the provider must append the missing range rather
    than replace the original layout.
    """
    info = bytearray(48)
    info[24] = 5
    struct.pack_into("<3I", info, 36, ROOT_THREADS, 1, 1)
    resources = ([7, 0, 0, 0], [7, 2, 3, 3])
    psv = pack([len(info)]) + info + pack([2, 16, *resources[0], *resources[1]])
    first = list(root_code(0)[:-1])
    first[0] = 0xBE800304  # s4, table 0
    second = list(root_code(0x40))
    second[0] = 0xBE800305  # s5, table 1
    user = ([3, 16, 1, 3, 0x105],)
    used = ([0x303, 0, 0, 1, 0x404], [0x303, 16, 3, 1, 0x10504])
    return stage_container(0x50060, image(7, first + second, user, used), [(b"PSV0", psv)])


BINDLESS_INDEX = 80
BINDLESS_EXPECTED = 0x7AB1E002


def bindless_compute_shader():
    """Direct-indexed raw UAV store at a guest index outside the paged limit."""
    code = [0xbe8403ff, BINDLESS_INDEX, 0x8f048504,
            0xf4080205, 0x08000000, 0xbf8cc07f,
            0x7e0002ff, BINDLESS_EXPECTED, 0xe0700000, 0x80020000, 0xbf810000]
    info = bytearray(48)
    info[24] = 5
    struct.pack_into("<3I", info, 36, 64, 1, 1)
    root = pack([2, 0, 24, 0, 24, 0x400])
    return stage_container(0x50066, image(7, code, [(5, 0, 1, 0, 10)]), [
        (b"SFI0", struct.pack("<Q", 1 << 25)),
        (b"PSV0", pack([48]) + info + pack([0])),
        (b"RTS0", root),
    ])


def declaration(name, data):
    values = ",".join(str(byte) for byte in data)
    return (f"constexpr unsigned char {name}[] = {{{values}}};\n"
            "// The name its translation is cached under.\n"
            f"constexpr wchar_t {name}_digest[] = L\"{hashlib.sha256(data).hexdigest()}\";\n")


def header():
    words = ",".join(f"{word}u" for word in wave_expected())
    return ("// Generated from shader_fixture.py.\n"
            + declaration("xbox_shader", shader()) + declaration("wave_shader", wave_shader())
            + f"constexpr unsigned wave_shader_expected[] = {{{words}}};\n"
            + declaration("recovered_root_shader", recovered_root_shader())
            + declaration("bindless_compute_shader", bindless_compute_shader())
            + f"constexpr unsigned bindless_index = {BINDLESS_INDEX};\nconstexpr unsigned bindless_expected = {BINDLESS_EXPECTED:#x};\n"
            + declaration("root_shader", root_shader(0x40)) + declaration("root_refused_shader", root_shader(0xA0))
            + f"constexpr unsigned root_shader_expected = {ROOT_EXPECTED:#x};\n")


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(header())
