"""Simulator decode + validation coverage."""

import pytest

from brother_ptouch.encoder import encode_label, pack_grayscale_row
from brother_ptouch.simulator import DecodeError, decode


def _one_line_stream(auto_cut=True):
    row = bytearray([255] * 128)
    row[10] = 0
    bitmap = pack_grayscale_row(bytes(row))
    return encode_label(bitmap, 1, tape_width_mm=24, auto_cut=auto_cut)


def test_decode_reports_print_info_and_clean_stream():
    decoded = decode(_one_line_stream())
    assert decoded.tape_width_mm == 24
    assert decoded.media_type == 0x01
    assert decoded.raster_line_count == 1
    assert decoded.auto_cut is True
    assert decoded.compression == 0
    assert decoded.margin_dots == 14
    assert decoded.trailer == "cut"
    assert decoded.warnings == []


def test_decode_no_cut_trailer():
    decoded = decode(_one_line_stream(auto_cut=False))
    assert decoded.auto_cut is False
    assert decoded.trailer == "no-cut"


def test_decode_rejects_truncated_header():
    with pytest.raises(DecodeError):
        decode(b"\x00" * 100 + b"\x1b\x40")  # stops right after ESC @


def test_decode_rejects_bad_initialize():
    with pytest.raises(DecodeError, match="ESC @"):
        decode(b"\x00" * 100 + b"\xff\xff" + b"\x00" * 50)


def test_decode_warns_on_short_invalidate_prefix():
    # Hand-build a minimal-but-valid stream with only 8 zero bytes of prefix.
    body = (
        b"\x1b\x40"
        b"\x1b\x69\x61\x01"
        b"\x1b\x69\x7a\x84\x01\x18\x00\x01\x00\x00\x00\x00\x00"
        b"\x1b\x69\x4d\x40"
        b"\x1b\x69\x4b\x08"
        b"\x1b\x69\x64\x0e\x00"
        b"\x4d\x00"
        b"\x47\x10\x00" + b"\x00" * 16 +
        b"\x1a"
    )
    decoded = decode(b"\x00" * 8 + body)
    assert any("invalidate prefix" in w for w in decoded.warnings)
