"""Fixed-size label coverage (--size / LabelSize)."""

import pytest
from PIL import Image

from brother_ptouch.cli import main
from brother_ptouch.encoder import PRINT_HEAD_DOTS, encode_label
from brother_ptouch.render import (
    DOTS_PER_MM,
    LabelSize,
    compose_code_label,
    compose_image,
    compose_text,
    raster_from_composed,
)
from brother_ptouch.simulator import decode, to_preview_image


def _threshold(img):
    return img.convert("L").point(lambda p: 0 if p < 128 else 255)


# --------------------------------------------------------------------------- #
# LabelSize
# --------------------------------------------------------------------------- #


def test_labelsize_from_mm():
    s = LabelSize.from_mm(16.5, 5)
    assert s.length_dots == round(16.5 * DOTS_PER_MM)  # 117
    assert s.band_dots == round(5 * DOTS_PER_MM)  # 35


def test_labelsize_rejects_height_beyond_tape():
    with pytest.raises(ValueError, match="printable tape width"):
        LabelSize.from_mm(16.5, 25)  # 25mm > ~18mm tape


def test_labelsize_partial_axes():
    assert LabelSize.from_mm(width_mm=20).band_dots is None
    assert LabelSize.from_mm(height_mm=5).length_dots is None


def test_labelsize_rejects_absurd_width():
    # Codex review, PR #3: a huge width must be rejected before any canvas
    # allocation, not OOM.
    with pytest.raises(ValueError, match="safety cap"):
        LabelSize.from_mm(1_000_000, 5)


def test_apply_length_guards_huge_length():
    from brother_ptouch.render import MAX_RASTER_LINES, _apply_length

    huge = LabelSize(length_dots=MAX_RASTER_LINES + 1)
    with pytest.raises(ValueError, match="safety cap"):
        _apply_length(Image.new("L", (10, PRINT_HEAD_DOTS), 255), huge)


def test_text_taller_than_requested_height_errors():
    # Codex review, PR #3: a tiny H band that even MIN_FONT_PX overshoots must
    # be rejected, not silently drawn taller than requested.
    with pytest.raises(ValueError, match="does not fit the requested height"):
        compose_text("HELLO", size=LabelSize.from_mm(30, 1))


def test_code_side_text_taller_than_height_errors():
    # Codex review, PR #3 (round 2): the same guard for a code label's
    # side-layout text. Wide enough that the length guard doesn't fire first.
    code = Image.new("L", (20, 20), 0)
    with pytest.raises(ValueError, match="does not fit the requested height"):
        compose_code_label(
            code, is_square=True, text="A\nB\nC", layout="side", size=LabelSize.from_mm(60, 4)
        )


# --------------------------------------------------------------------------- #
# compose_* honour the size
# --------------------------------------------------------------------------- #


def test_compose_text_exact_size():
    img = compose_text("HELLO", size=LabelSize.from_mm(30, 6))
    assert img.size == (round(30 * DOTS_PER_MM), PRINT_HEAD_DOTS)


def test_compose_image_sub_band_and_length():
    src = Image.new("L", (200, 100), 255)
    src.paste(0, (0, 0, 50, 100))
    img = compose_image(src, size=LabelSize.from_mm(20, 5))
    assert img.size == (round(20 * DOTS_PER_MM), PRINT_HEAD_DOTS)


def test_compose_code_label_exact_size():
    code = Image.new("L", (20, 20), 0)  # synthetic square code (no libs needed)
    img = compose_code_label(code, is_square=True, text="W", size=LabelSize.from_mm(20, 5))
    assert img.size == (round(20 * DOTS_PER_MM), PRINT_HEAD_DOTS)


def test_content_too_long_for_width_errors():
    with pytest.raises(ValueError, match="exceeding the requested label length"):
        compose_text("a label far too long to fit", size=LabelSize.from_mm(5, 6))


# --------------------------------------------------------------------------- #
# round trip still holds at a fixed size
# --------------------------------------------------------------------------- #


def test_sized_text_roundtrip_identity():
    composed = compose_text("HELLO", size=LabelSize.from_mm(30, 6))
    bitmap, lines = raster_from_composed(composed)
    assert lines == round(30 * DOTS_PER_MM)
    preview = to_preview_image(decode(encode_label(bitmap, lines, tape_width_mm=24)))
    assert preview.tobytes() == _threshold(composed).tobytes()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_size_sets_label_length(tmp_path):
    out = tmp_path / "s.bin"
    rc = main(["text", "--text", "HI", "--size", "30x6", "--out", str(out)])
    assert rc == 0
    decoded = decode(out.read_bytes())
    assert decoded.raster_line_count == round(30 * DOTS_PER_MM)
    assert decoded.warnings == []


def test_cli_bad_size_reports_error(tmp_path, capsys):
    rc = main(["text", "--text", "HI", "--size", "30", "--out", str(tmp_path / "x.bin")])
    assert rc == 1
    assert "WxH" in capsys.readouterr().err


def test_cli_size_too_tall_reports_error(tmp_path, capsys):
    rc = main(["text", "--text", "HI", "--size", "30x25", "--out", str(tmp_path / "x.bin")])
    assert rc == 1
    assert "printable tape width" in capsys.readouterr().err
