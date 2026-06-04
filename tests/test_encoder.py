"""Byte-exact coverage for the Brother PT-P710BT raster command encoder.

Ported from ``tests/labelEncoder.test.ts`` in the filament-db project. These
tests pin both the wire format (so a regression in the byte sequence is caught
before a print run wastes tape) and the grayscale -> 1-bit packing (so a
bit-order flip turns a QR into noise loudly rather than silently), plus every
section-4 protocol trap.
"""

import pytest

from brother_ptouch.encoder import (
    BYTES_PER_RASTER_LINE,
    PRINT_HEAD_DOTS,
    encode_label,
    pack_grayscale_bitmap,
    pack_grayscale_row,
)


def make_bitmap(raster_lines: int, fill: int = 0x00) -> bytes:
    return bytes([fill]) * (raster_lines * BYTES_PER_RASTER_LINE)


# --------------------------------------------------------------------------- #
# encode_label -- wire format
# --------------------------------------------------------------------------- #


def test_exact_fixed_header_for_1line_24mm_autocut():
    b = encode_label(make_bitmap(1), 1, tape_width_mm=24)
    # Invalidate: 100 x 0x00
    assert b[:100] == b"\x00" * 100
    # ESC @ (initialize)
    assert b[100:102] == b"\x1b\x40"
    # ESC i a 01 (raster mode)
    assert b[102:106] == b"\x1b\x69\x61\x01"
    # ESC i z <flags 0x84> <media 0x01> <width 24> <length 0> <lines LE u32> <page 0> <0>
    assert b[106:109] == b"\x1b\x69\x7a"
    assert b[109] == 0x84
    assert b[110] == 0x01
    assert b[111] == 24
    assert b[112] == 0x00
    assert b[113:117] == bytes([1, 0, 0, 0])  # raster line count LE u32
    assert b[117] == 0x00  # page
    assert b[118] == 0x00  # reserved
    # ESC i M 0x40 (auto-cut)
    assert b[119:123] == b"\x1b\x69\x4d\x40"
    # ESC i K 0x08 -- bit 3 set = no-chain (feed + cut after the last label)
    assert b[123:127] == b"\x1b\x69\x4b\x08"
    # ESC i d 0x0E 0x00 (14 dots = 2mm margin)
    assert b[127:132] == b"\x1b\x69\x64\x0e\x00"
    # M 0x00 (uncompressed)
    assert b[132:134] == b"\x4d\x00"
    # G 0x10 0x00 + 16 data bytes
    assert b[134:137] == b"\x47\x10\x00"
    assert b[137:153] == b"\x00" * 16
    # trailer
    assert b[153] == 0x1A
    assert len(b) == 154


def test_raster_line_count_little_endian_u32():
    # 4321 = 0x10E1 -> LE bytes 0xE1 0x10 0x00 0x00
    b = encode_label(make_bitmap(4321), 4321, tape_width_mm=24)
    assert b[113:117] == bytes([0xE1, 0x10, 0, 0])


def test_autocut_false_swaps_mode_kflag_and_trailer():
    b = encode_label(make_bitmap(1), 1, tape_width_mm=24, auto_cut=False)
    assert b[122] == 0x00  # mode bits -- auto-cut off
    assert b[126] == 0x00  # ESC i K -- chain mode
    assert b[-1] == 0x0C  # trailer (print, no cut)


def test_custom_margin_dots_le_u16():
    b = encode_label(make_bitmap(1), 1, tape_width_mm=24, margin_dots=350)  # 0x015E
    assert b[130:132] == bytes([0x5E, 0x01])


@pytest.mark.parametrize(
    "width,byte",
    [(3.5, 4), (6, 6), (9, 9), (12, 12), (18, 18), (24, 24)],
)
def test_media_width_byte_rounding(width, byte):
    # Brother's media-width byte is integer-valued: 3.5mm encodes as 4; every
    # other width round-trips verbatim. (Trap 4.3.)
    b = encode_label(make_bitmap(1), 1, tape_width_mm=width)
    assert b[111] == byte


def test_preserves_raster_line_data_verbatim():
    raster_lines = 5
    bitmap = bytearray(raster_lines * BYTES_PER_RASTER_LINE)
    for i in range(raster_lines):
        bitmap[i * BYTES_PER_RASTER_LINE] = 0xA0 + i
        bitmap[i * BYTES_PER_RASTER_LINE + 15] = 0xB0 + i
    b = encode_label(bytes(bitmap), raster_lines, tape_width_mm=24)
    for i in range(raster_lines):
        line_start = 134 + i * 19 + 3  # skip G + length
        assert b[line_start] == 0xA0 + i
        assert b[line_start + 15] == 0xB0 + i


def test_rejects_bitmap_length_mismatch():
    with pytest.raises(ValueError, match="bitmap length"):
        encode_label(bytes(15), 1, tape_width_mm=24)
    with pytest.raises(ValueError, match="bitmap length"):
        encode_label(bytes(32), 1, tape_width_mm=24)


def test_rejects_raster_lines_below_one():
    with pytest.raises(ValueError, match="raster_lines"):
        encode_label(bytes(0), 0, tape_width_mm=24)


def test_rejects_margin_out_of_u16_range():
    with pytest.raises(ValueError, match="margin_dots"):
        encode_label(make_bitmap(1), 1, tape_width_mm=24, margin_dots=-1)
    with pytest.raises(ValueError, match="margin_dots"):
        encode_label(make_bitmap(1), 1, tape_width_mm=24, margin_dots=65536)


def test_rejects_oversize_stream():
    with pytest.raises(ValueError, match="safety cap"):
        # 300k lines -> ~5.7 MB, past the 5 MB cap.
        encode_label(make_bitmap(300_000), 300_000, tape_width_mm=24)


def test_output_length_math():
    raster_lines = 100
    b = encode_label(make_bitmap(raster_lines), raster_lines, tape_width_mm=24)
    header_len = 100 + 2 + 4 + 13 + 4 + 4 + 5 + 2  # = 134
    assert len(b) == header_len + raster_lines * 19 + 1


# --------------------------------------------------------------------------- #
# pack_grayscale_row
# --------------------------------------------------------------------------- #


def test_pack_row_msb_first_black_is_one():
    row = bytearray([255] * PRINT_HEAD_DOTS)
    for i in range(8):
        row[i] = 0
    packed = pack_grayscale_row(bytes(row))
    assert packed[0] == 0xFF
    assert packed[1:] == b"\x00" * (BYTES_PER_RASTER_LINE - 1)


def test_pack_row_msb_of_byte0_is_leftmost_dot():
    row = bytearray([255] * PRINT_HEAD_DOTS)
    row[0] = 0
    assert pack_grayscale_row(bytes(row))[0] == 0x80


def test_pack_row_lsb_of_byte15_is_rightmost_dot():
    row = bytearray([255] * PRINT_HEAD_DOTS)
    row[PRINT_HEAD_DOTS - 1] = 0
    assert pack_grayscale_row(bytes(row))[BYTES_PER_RASTER_LINE - 1] == 0x01


def test_pack_row_threshold_128_stays_white():
    packed = pack_grayscale_row(bytes([128] * PRINT_HEAD_DOTS))
    assert packed == b"\x00" * BYTES_PER_RASTER_LINE


def test_pack_row_threshold_127_flips_black():
    packed = pack_grayscale_row(bytes([127] * PRINT_HEAD_DOTS))
    assert packed == b"\xff" * BYTES_PER_RASTER_LINE


def test_pack_row_rejects_wrong_length():
    with pytest.raises(ValueError, match="row length"):
        pack_grayscale_row(bytes(127))
    with pytest.raises(ValueError, match="row length"):
        pack_grayscale_row(bytes(129))


# --------------------------------------------------------------------------- #
# pack_grayscale_bitmap
# --------------------------------------------------------------------------- #


def test_pack_bitmap_packs_each_row_independently():
    buf = bytearray([255] * (2 * PRINT_HEAD_DOTS))
    for i in range(PRINT_HEAD_DOTS):
        buf[i] = 0  # row 0 fully black, row 1 fully white
    packed = pack_grayscale_bitmap(bytes(buf), 2)
    assert packed[:BYTES_PER_RASTER_LINE] == b"\xff" * BYTES_PER_RASTER_LINE
    assert packed[BYTES_PER_RASTER_LINE:] == b"\x00" * BYTES_PER_RASTER_LINE


def test_pack_bitmap_rejects_mismatch():
    with pytest.raises(ValueError, match="buffer length"):
        pack_grayscale_bitmap(bytes(PRINT_HEAD_DOTS - 1), 1)


# --------------------------------------------------------------------------- #
# end-to-end round trip (pack -> encode -> unpack)
# --------------------------------------------------------------------------- #


def test_pack_encode_unpack_single_dot():
    gray_row = bytearray([255] * PRINT_HEAD_DOTS)
    gray_row[42] = 0
    bitmap = pack_grayscale_row(bytes(gray_row))
    b = encode_label(bitmap, 1, tape_width_mm=24)
    payload = b[137 : 137 + BYTES_PER_RASTER_LINE]
    # dot 42: byte 5, bit 7-(42&7)=5
    assert (payload[5] >> 5) & 1 == 1
    black_bits = sum(
        (payload[dot >> 3] >> (7 - (dot & 7))) & 1 for dot in range(PRINT_HEAD_DOTS)
    )
    assert black_bits == 1
