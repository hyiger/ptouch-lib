"""Bambu nozzle-marker generation, composition, CLI, and round-trip coverage.

These need no optional libraries (the marker is drawn from a built-in table with
Pillow only), so the whole file runs on a Pillow-only install.
"""

import pytest

from brother_ptouch.cli import main
from brother_ptouch.codes import (
    NOZZLE_MARKERS,
    normalize_nozzle,
    nozzle_image,
    nozzle_text,
)
from brother_ptouch.encoder import PRINT_HEAD_DOTS, encode_label
from brother_ptouch.render import LabelSize, compose_nozzle, raster_from_composed
from brother_ptouch.simulator import decode, to_preview_image


def _threshold(img):
    return img.convert("L").point(lambda p: 0 if p < 128 else 255)


def _pipeline_preview(composed):
    """compose -> raster -> encode -> decode -> human-readable preview."""
    bitmap, raster_lines = raster_from_composed(composed)
    data = encode_label(bitmap, raster_lines, tape_width_mm=24)
    return to_preview_image(decode(data))


# --------------------------------------------------------------------------- #
# name normalization + text
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("WC0.4", "WC0.4"), ("wc0.4", "WC0.4"), ("WC.4", "WC0.4"),
        ("wc4", "WC0.4"), ("WC 0.4", "WC0.4"), ("wc_0.4", "WC0.4"),
        ("0.4", "0.4"), (".4", "0.4"), ("4", "0.4"),
        ("HF0.6", "HF0.6"), ("hf6", "HF0.6"),
        ("HFWC0.8", "HFWC0.8"), ("hfwc8", "HFWC0.8"), ("hf/wc.8", "HFWC0.8"),
        ("0.2", "0.2"), ("2", "0.2"),
    ],
)
def test_normalize_nozzle_variants(raw, expected):
    assert normalize_nozzle(raw) == expected


def test_every_marker_normalizes_to_itself():
    for key in NOZZLE_MARKERS:
        assert normalize_nozzle(key) == key


@pytest.mark.parametrize(
    "raw,expected",
    [("0.4", "0.4"), ("WC0.6", "WC.6"), ("HF0.8", "HF.8"), ("HFWC0.4", "HF\nWC.4")],
)
def test_nozzle_text(raw, expected):
    assert nozzle_text(raw) == expected


def test_unknown_nozzle_rejected():
    with pytest.raises(ValueError, match="unknown nozzle"):
        normalize_nozzle("HF0.2")  # HF has no 0.2


def test_unreadable_diameter_rejected():
    with pytest.raises(ValueError, match="diameter"):
        normalize_nozzle("WC0.5")  # 5 is not a valid diameter digit


# --------------------------------------------------------------------------- #
# marker image content
# --------------------------------------------------------------------------- #


def test_nozzle_image_matches_table():
    # 1 px per module, no quiet zone: '#' -> black (0), '.' -> white (255).
    grid = NOZZLE_MARKERS["WC0.4"]
    img = nozzle_image("WC0.4", quiet_zone_modules=0)
    assert img.size == (len(grid[0]), len(grid))
    px = img.load()
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            assert px[x, y] == (0 if ch == "#" else 255), (x, y, ch)


def test_nozzle_image_quiet_zone_is_white():
    img = nozzle_image("0.4", quiet_zone_modules=2)
    # 7x3 grid + 2 modules each side -> 11x7
    assert img.size == (7 + 4, 3 + 4)
    assert img.load()[0, 0] == 255  # corner is quiet-zone white


# --------------------------------------------------------------------------- #
# bundled photo bands (the exact, default source)
# --------------------------------------------------------------------------- #


def test_every_marker_has_a_bundled_band():
    from brother_ptouch.codes import nozzle_band_image

    for key in NOZZLE_MARKERS:
        img = nozzle_band_image(key)
        assert img.mode == "L"
        assert img.getextrema() == (0, 255), key  # white content on a black field
        # 16x5mm face -> aspect ~3.2
        assert 2.6 < img.width / img.height < 3.8, key


def test_photo_band_missing_raises():
    from brother_ptouch.codes import nozzle_band_image

    with pytest.raises(ValueError, match="diameter|unknown|no band image"):
        nozzle_band_image("ZZ9")


def test_photo_band_roundtrip_identity():
    # The default (photo) path prints un-mirrored / uncorrupted.
    composed = compose_nozzle("WC0.4", size=LabelSize.from_mm(16, 5))
    preview = _pipeline_preview(composed)
    assert preview.tobytes() == _threshold(composed).tobytes()


# --------------------------------------------------------------------------- #
# compose + invert
# --------------------------------------------------------------------------- #


def test_compose_nozzle_dims_and_band():
    img = compose_nozzle("WC0.4")
    assert img.height == PRINT_HEAD_DOTS
    assert img.width > 0
    assert _threshold(img).getextrema() == (0, 255)


def test_invert_default_gives_black_field():
    # Inverted (default): the background/field is black (0), so a corner is 0.
    img = compose_nozzle("WC0.4", text=None)
    assert img.load()[0, 0] == 0


def test_no_invert_gives_white_field():
    img = compose_nozzle("WC0.4", text=None, invert=False)
    assert img.load()[0, 0] == 255


def test_true_size_marker_below_min_code_dots():
    # The real nozzle marker is ~2.2mm (~15 dots) tall -- under the QR/barcode
    # MIN_CODE_DOTS floor. The nozzle path must still render it (not raise).
    size = LabelSize.from_mm(width_mm=5.2, height_mm=2.2)
    img = compose_nozzle(
        "WC0.4", source="generated", text=None, invert=False, quiet_zone_modules=0, size=size
    )
    # marker scaled to ~5 dots/module -> ~15 dots tall, well under MIN_CODE_DOTS (24)
    ys = [y for y in range(img.height) if any(img.load()[x, y] < 128 for x in range(img.width))]
    marker_h = ys[-1] - ys[0] + 1
    assert 12 <= marker_h <= 20


def test_invert_is_pixelwise_complement():
    pos = compose_nozzle("HF0.6", text="HF.6", invert=False)
    neg = compose_nozzle("HF0.6", text="HF.6", invert=True)
    assert pos.size == neg.size
    p, n = _threshold(pos).tobytes(), _threshold(neg).tobytes()
    assert all(a + b == 255 for a, b in zip(p, n, strict=True))


# --------------------------------------------------------------------------- #
# round trip through the printer pipeline
# --------------------------------------------------------------------------- #


def test_nozzle_roundtrip_identity():
    # The exact pixels that print equal the composed pixels -- not mirrored,
    # not corrupted (the #587 raster-order trap).
    composed = compose_nozzle("WC0.4", text="WC.4")
    preview = _pipeline_preview(composed)
    assert preview.tobytes() == _threshold(composed).tobytes()


def test_marker_grid_survives_pipeline():
    # End-to-end: recover the 3x7 grid from the printed label and compare to the
    # table. Use invert=False + no quiet zone + no text so the marker fills a
    # clean integer-scaled block we can downsample.
    nozzle = "WC0.6"
    grid = NOZZLE_MARKERS[nozzle]
    composed = compose_nozzle(
        nozzle, source="generated", text=None, invert=False, quiet_zone_modules=0
    )
    preview = _pipeline_preview(composed).convert("L")
    px = preview.load()
    w, h = preview.size
    # Find the marker bounding box (black ink on white) in the printed preview.
    cols = [x for x in range(w) if any(px[x, y] < 128 for y in range(h))]
    ys = [y for y in range(h) if any(px[x, y] < 128 for x in range(w))]
    x0, x1, y0, y1 = cols[0], cols[-1] + 1, ys[0], ys[-1] + 1
    mw, mh = x1 - x0, y1 - y0
    ncols, nrows = len(grid[0]), len(grid)
    for r in range(nrows):
        for c in range(ncols):
            cx = x0 + int((c + 0.5) * mw / ncols)
            cy = y0 + int((r + 0.5) * mh / nrows)
            black = px[cx, cy] < 128
            assert black == (grid[r][c] == "#"), (r, c)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_nozzle_to_out_decodes_clean(tmp_path):
    out = tmp_path / "nz.bin"
    preview = tmp_path / "nz.png"
    rc = main(["nozzle", "WC0.4", "--out", str(out), "--preview", str(preview)])
    assert rc == 0
    assert out.exists() and preview.exists()
    decoded = decode(out.read_bytes())
    assert decoded.tape_width_mm == 24
    assert decoded.warnings == []


def test_cli_nozzle_photo_default_is_actual_size(tmp_path):
    # No --size: the photo band defaults to the real 16x5mm heat-sink face.
    out = tmp_path / "p.bin"
    rc = main(["nozzle", "WC0.4", "--out", str(out)])
    assert rc == 0
    decoded = decode(out.read_bytes())
    assert abs(decoded.raster_line_count - round(16 * 180 / 25.4)) <= 1  # 16 mm long


def test_cli_nozzle_default_suppresses_feed_margin(tmp_path):
    # The implicit 16x5mm default must also zero the leading feed margin, so the
    # cut label is exactly 16mm -- not 16mm + ~2mm feed. (Codex review, PR #5.)
    out = tmp_path / "m.bin"
    rc = main(["nozzle", "WC0.4", "--out", str(out)])
    assert rc == 0
    assert decode(out.read_bytes()).margin_dots == 0


def test_cli_nozzle_generated_sized_label(tmp_path):
    out = tmp_path / "s.bin"
    # --generated: marker-only, --size height is the marker grid height.
    rc = main(["nozzle", "WC0.4", "--generated", "--no-text", "--size", "16x2.2", "--out", str(out)])
    assert rc == 0
    decoded = decode(out.read_bytes())
    assert abs(decoded.raster_line_count - round(16 * 180 / 25.4)) <= 1  # 16 mm long


def test_cli_unknown_nozzle_returns_error(tmp_path):
    out = tmp_path / "bad.bin"
    rc = main(["nozzle", "ZZ9", "--out", str(out)])
    assert rc == 1
    assert not out.exists()
