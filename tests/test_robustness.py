"""Regression tests for the core robustness fixes (issues #9-#12).

- #9  encode_label validates tape width
- #10 auto-sized renders are rejected before a huge Pillow allocation
- #11 truncated streams raise DecodeError (not IndexError)
- #12 compose_text rejects text taller than the tape instead of cropping
"""

import pytest
from PIL import Image

from brother_ptouch.encoder import BYTES_PER_RASTER_LINE, VALID_TAPE_WIDTHS_MM, encode_label
from brother_ptouch.render import PRINT_HEAD_DOTS, compose_image, compose_text
from brother_ptouch.simulator import DecodeError, decode

# --- #9: tape-width validation ------------------------------------------------

def test_encode_label_rejects_unsupported_tape_width():
    bm = bytes(BYTES_PER_RASTER_LINE)
    with pytest.raises(ValueError, match="not a supported"):
        encode_label(bm, 1, tape_width_mm=25)


@pytest.mark.parametrize("width", VALID_TAPE_WIDTHS_MM)
def test_encode_label_accepts_each_standard_width(width):
    encode_label(bytes(BYTES_PER_RASTER_LINE), 1, tape_width_mm=width)  # no raise


# --- #10: reject overlong auto-sized renders before allocating ----------------

def test_compose_image_rejects_extreme_aspect_before_allocating():
    # 2,000,000 x 10 would scale to ~25M dots wide; must raise, not allocate.
    huge = Image.new("L", (2_000_000, 10), 255)
    with pytest.raises(ValueError, match="safety cap"):
        compose_image(huge)


def test_compose_text_rejects_overlong_single_line():
    with pytest.raises(ValueError, match="safety cap"):
        compose_text("W" * 200_000, font_size=40)


# --- #11: truncated streams raise DecodeError, not IndexError -----------------

def test_decode_truncated_print_info_raises_decode_error():
    stream = (
        b"\x00" * 100
        + b"\x1b\x40"            # init
        + b"\x1b\x69\x61\x01"    # raster mode
        + b"\x1b\x69\x7a"        # print-info header, payload truncated
        + b"\x01"                # only 1 of 10 payload bytes
    )
    with pytest.raises(DecodeError):
        decode(stream)


def test_decode_truncated_midheader_raises_decode_error():
    # Ends in the middle of the raster-mode command.
    with pytest.raises(DecodeError):
        decode(b"\x00" * 100 + b"\x1b\x40" + b"\x1b\x69")


# --- #12: reject too-tall text instead of cropping ---------------------------

def test_compose_text_rejects_text_taller_than_tape():
    txt = "\n".join(f"L{i}" for i in range(40))  # 40 lines >> 128 dots even at min font
    with pytest.raises(ValueError, match="tape height"):
        compose_text(txt)


def test_compose_text_normal_multiline_ok():
    img = compose_text("AB\nCD")
    assert img.height == PRINT_HEAD_DOTS
