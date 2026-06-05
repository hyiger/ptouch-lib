"""Fill remaining decode/compose coverage gaps (issue #22).

- simulator warning branches (packbits, unknown compression, line-count
  mismatch, bad/missing trailer) and the empty-preview error
- the nozzle photo-band invert toggling polarity
"""

import pytest

from brother_ptouch.nozzle import compose_nozzle
from brother_ptouch.render import LabelSize
from brother_ptouch.simulator import DecodeError, decode, to_preview_image


def _stream(comp=0x00, declared=1, actual=1, trailer=0x1A):
    """Build a structurally valid stream with adjustable knobs for warnings."""
    out = bytearray(b"\x00" * 100 + b"\x1b\x40" + b"\x1b\x69\x61\x01")
    out += b"\x1b\x69\x7a" + b"\x84\x01\x18\x00" + int(declared).to_bytes(4, "little") + b"\x00\x00"
    out += b"\x1b\x69\x4d\x40" + b"\x1b\x69\x4b\x08" + b"\x1b\x69\x64\x0e\x00"
    out += b"\x4d" + bytes([comp])
    for _ in range(actual):
        out += b"\x47" + (16).to_bytes(2, "little") + bytes(16)
    if trailer is not None:
        out += bytes([trailer])
    return bytes(out)


def test_packbits_compression_warns():
    assert any("packbits" in w for w in decode(_stream(comp=0x02)).warnings)


def test_unrecognized_compression_warns():
    assert any("compression" in w for w in decode(_stream(comp=0x03)).warnings)


def test_raster_line_count_mismatch_warns():
    assert any("line count mismatch" in w for w in decode(_stream(declared=5, actual=1)).warnings)


def test_unrecognized_trailer_warns():
    assert any("trailer" in w for w in decode(_stream(trailer=0x99)).warnings)


def test_missing_trailer_warns():
    assert any("no terminator" in w for w in decode(_stream(trailer=None)).warnings)


def test_empty_preview_raises():
    res = decode(_stream(declared=0, actual=0))
    assert res.raster_lines == []
    with pytest.raises(DecodeError):
        to_preview_image(res)


def _ink(img):
    bw = img.convert("L").point(lambda v: 0 if v < 128 else 255)
    return sum(1 for p in bw.tobytes() if p == 0)


def test_photo_band_invert_toggles_polarity():
    size = LabelSize.from_mm(16, 5)
    inverted = compose_nozzle("WC0.4", invert=True, size=size)    # black field inked
    plain = compose_nozzle("WC0.4", invert=False, size=size)      # field is bare tape
    assert inverted.tobytes() != plain.tobytes()
    # the inverted (black-on-white-tape) variant inks the whole band field
    assert _ink(inverted) > _ink(plain)
