"""Render + simulator round-trip coverage (no hardware).

The headline test runs a known image/text through the full
compose -> finalize -> encode -> decode pipeline and confirms the simulator
reconstructs the exact pixels that were composed -- proving the geometry,
the #587 raster-line reversal, and the bit packing all line up.
"""

import pytest
from PIL import Image

from brother_ptouch.encoder import PRINT_HEAD_DOTS, encode_label, pack_grayscale_bitmap
from brother_ptouch.render import (
    compose_image,
    compose_text,
    image_to_raster,
    raster_from_composed,
    text_to_raster,
)
from brother_ptouch.simulator import decode, to_preview_image


def _threshold(img: Image.Image) -> Image.Image:
    return img.convert("L").point(lambda p: 0 if p < 128 else 255)


def _roundtrip_preview(composed: Image.Image) -> Image.Image:
    """compose -> raster -> encode -> decode -> preview."""
    bitmap, raster_lines = raster_from_composed(composed)
    data = encode_label(bitmap, raster_lines, tape_width_mm=24)
    return to_preview_image(decode(data))


# --------------------------------------------------------------------------- #
# compose geometry
# --------------------------------------------------------------------------- #


def test_compose_text_is_128_tall_with_black_pixels():
    img = compose_text("PLA Black", font_size=40)
    assert img.height == PRINT_HEAD_DOTS
    assert img.width > 0
    assert _threshold(img).getextrema() == (0, 255)  # has both black and white


def test_compose_text_vertical_is_128_tall():
    img = compose_text("PLA Black", font_size=40, orientation="vertical")
    assert img.height == PRINT_HEAD_DOTS
    assert _threshold(img).getextrema() == (0, 255)


def test_compose_multiline_text():
    img = compose_text("Line one\nLine two\nLine three", font_size=60)
    assert img.height == PRINT_HEAD_DOTS
    assert _threshold(img).getextrema() == (0, 255)


def test_compose_image_scales_height_to_128():
    src = Image.new("L", (400, 200), 255)
    src.paste(0, (10, 10, 60, 60))  # a black square
    composed = compose_image(src)
    assert composed.height == PRINT_HEAD_DOTS
    assert composed.width == round(400 * PRINT_HEAD_DOTS / 200)


def test_invalid_orientation_rejected():
    with pytest.raises(ValueError, match="orientation"):
        compose_text("x", orientation="sideways")


def test_raster_from_composed_rejects_wrong_height():
    with pytest.raises(ValueError, match="128 dots tall"):
        raster_from_composed(Image.new("L", (100, 64), 255))


# --------------------------------------------------------------------------- #
# full pipeline round trips
# --------------------------------------------------------------------------- #


def test_text_roundtrip_reconstructs_composed_pixels():
    composed = compose_text("PLA Black", font_size=40)
    preview = _roundtrip_preview(composed)
    assert preview.size == composed.size
    # The simulator must reconstruct exactly the (thresholded) composed image.
    assert preview.tobytes() == _threshold(composed).tobytes()


def test_vertical_text_roundtrip_reconstructs_composed_pixels():
    composed = compose_text("ABS White", font_size=36, orientation="vertical")
    preview = _roundtrip_preview(composed)
    assert preview.tobytes() == _threshold(composed).tobytes()


def test_image_roundtrip_reconstructs_composed_pixels():
    src = Image.new("L", (300, 128), 255)
    # An asymmetric mark so a mirror/flip bug would be caught.
    src.paste(0, (0, 0, 40, 128))  # solid black bar on the left edge
    src.paste(0, (250, 50, 300, 78))  # a small block near the right
    composed = compose_image(src)
    preview = _roundtrip_preview(composed)
    assert preview.tobytes() == _threshold(composed).tobytes()


def test_image_to_raster_matches_compose_plus_raster():
    src = Image.new("L", (200, 100), 255)
    src.paste(0, (5, 5, 25, 25))
    a = image_to_raster(src)
    b = raster_from_composed(compose_image(src))
    assert a == b


def test_text_to_raster_returns_packed_bitmap():
    bitmap, raster_lines = text_to_raster("Hi", font_size=48)
    assert raster_lines > 0
    assert len(bitmap) == raster_lines * 16


# --------------------------------------------------------------------------- #
# synthetic grayscale round trip (orientation-independent)
# --------------------------------------------------------------------------- #


def test_synthetic_grayscale_roundtrip_through_simulator():
    # A distinctive per-line signature so any line drop/reorder shows up.
    raster_lines = 7
    gray = bytearray([255] * (raster_lines * PRINT_HEAD_DOTS))
    for r in range(raster_lines):
        gray[r * PRINT_HEAD_DOTS + r] = 0  # black dot at column r of line r
    bitmap = pack_grayscale_bitmap(bytes(gray), raster_lines)
    data = encode_label(bitmap, raster_lines, tape_width_mm=24)
    decoded = decode(data)
    assert decoded.raster_line_count == raster_lines
    assert len(decoded.raster_lines) == raster_lines
    assert b"".join(decoded.raster_lines) == bytes(gray)
