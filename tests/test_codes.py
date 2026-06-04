"""QR / barcode / ArUco generation, composition, and round-trip coverage.

Tests that need an optional library skip gracefully when it isn't installed
(`pytest.importorskip`), so the core suite still runs on a Pillow-only install.
Where OpenCV is present we *decode* the generated codes to prove they are
correct and un-mirrored after the full render -> encode -> decode pipeline.
"""

import sys

import pytest

from brother_ptouch.encoder import PRINT_HEAD_DOTS, encode_label
from brother_ptouch.render import (
    compose_aruco,
    compose_barcode,
    compose_code_label,
    compose_qr,
    raster_from_composed,
)
from brother_ptouch.simulator import decode, to_preview_image


def _threshold(img):
    return img.convert("L").point(lambda p: 0 if p < 128 else 255)


def _pipeline_preview(composed):
    """compose -> raster -> encode -> decode -> human-readable preview."""
    bitmap, raster_lines = raster_from_composed(composed)
    data = encode_label(bitmap, raster_lines, tape_width_mm=24)
    return to_preview_image(decode(data))


# --------------------------------------------------------------------------- #
# QR
# --------------------------------------------------------------------------- #


def test_qr_compose_dims_and_content():
    pytest.importorskip("qrcode")
    img = compose_qr("hello world", text="Widget A")
    assert img.height == PRINT_HEAD_DOTS
    assert img.width > 0
    assert _threshold(img).getextrema() == (0, 255)


def test_qr_roundtrip_identity():
    pytest.importorskip("qrcode")
    composed = compose_qr("https://example.com/x/42", text="Widget A")
    preview = _pipeline_preview(composed)
    assert preview.tobytes() == _threshold(composed).tobytes()


@pytest.mark.parametrize(
    "layout,payload",
    [
        # Side-by-side gives the QR the full tape height, so a long URL scans.
        ("side", "https://filament-db.local/f/507f1f77bcf86cd799439011"),
        # Stacking shares the height with text, so a short payload is realistic.
        ("stack", "PLA-0042"),
    ],
)
def test_qr_scannable_after_pipeline(layout, payload):
    pytest.importorskip("qrcode")
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    composed = compose_qr(payload, text="PLA Black", layout=layout)
    # Decode the exact pixels that print, recovered through the full pipeline.
    preview = _pipeline_preview(composed)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(np.array(preview.convert("L")))
    assert decoded == payload


def test_qr_explicit_version():
    pytest.importorskip("qrcode")
    from brother_ptouch.codes import qr_image

    img = qr_image("abc", version=5)
    assert img.info["qr_version"] == 5


def test_qr_invalid_ec_rejected():
    pytest.importorskip("qrcode")
    from brother_ptouch.codes import qr_image

    with pytest.raises(ValueError, match="error_correction"):
        qr_image("abc", error_correction="Z")


# --------------------------------------------------------------------------- #
# barcode
# --------------------------------------------------------------------------- #


def test_barcode_compose_dims_and_content():
    pytest.importorskip("barcode")
    img = compose_barcode("ABC-12345", symbology="code128", text="Part ABC")
    assert img.height == PRINT_HEAD_DOTS
    assert _threshold(img).getextrema() == (0, 255)


def test_barcode_roundtrip_identity():
    pytest.importorskip("barcode")
    composed = compose_barcode("ABC-12345", symbology="code128")
    preview = _pipeline_preview(composed)
    assert preview.tobytes() == _threshold(composed).tobytes()


def test_barcode_invalid_data_rejected():
    pytest.importorskip("barcode")
    from brother_ptouch.codes import barcode_image

    with pytest.raises(ValueError, match="invalid data"):
        barcode_image("not-numeric", symbology="ean13")  # EAN-13 needs digits


def test_barcode_unknown_symbology_rejected():
    pytest.importorskip("barcode")
    from brother_ptouch.codes import barcode_image

    with pytest.raises(ValueError, match="unknown barcode symbology"):
        barcode_image("123", symbology="nope")


# --------------------------------------------------------------------------- #
# ArUco
# --------------------------------------------------------------------------- #


def test_aruco_compose_dims():
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    img = compose_aruco(7, dictionary="4X4_50", text="M7")
    assert img.height == PRINT_HEAD_DOTS
    assert _threshold(img).getextrema() == (0, 255)


@pytest.mark.parametrize("layout", ["side", "stack"])
def test_aruco_detectable_after_pipeline(layout):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    composed = compose_aruco(7, dictionary="4X4_50", text="Marker 7", layout=layout)
    preview = _pipeline_preview(composed)
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    _, ids, _ = cv2.aruco.ArucoDetector(d).detectMarkers(np.array(preview.convert("L")))
    assert ids is not None and 7 in ids.flatten().tolist()


def test_aruco_unknown_dict_rejected():
    pytest.importorskip("cv2")
    from brother_ptouch.codes import aruco_image

    with pytest.raises(ValueError, match="unknown ArUco dictionary"):
        aruco_image(0, dictionary="9X9_999")


def test_aruco_bad_id_rejected():
    pytest.importorskip("cv2")
    from brother_ptouch.codes import aruco_image

    with pytest.raises(ValueError, match="invalid marker id"):
        aruco_image(9999, dictionary="4X4_50")  # 4X4_50 only has ids 0-49


# --------------------------------------------------------------------------- #
# layout + error handling (no optional libs needed)
# --------------------------------------------------------------------------- #


def test_invalid_layout_rejected():
    from PIL import Image

    with pytest.raises(ValueError, match="layout"):
        compose_code_label(Image.new("L", (20, 20), 0), is_square=True, layout="diagonal")


def test_square_code_too_large_to_stack_errors():
    from PIL import Image

    # A 200x200 "code" can't integer-fit a tiny stacked region.
    big = Image.new("L", (200, 200), 0)
    with pytest.raises(ValueError, match="does not fit"):
        compose_code_label(big, is_square=True, text="x", layout="stack")


def test_stacked_barcode_with_no_room_errors():
    # Codex review, PR #1: a non-square (barcode) code stacked under text tall
    # enough to leave <= 0 dots must raise, not silently emit a 1-dot barcode.
    from PIL import Image

    bar = Image.new("L", (300, 110), 0)  # wide, non-square -> barcode path
    tall_text = "\n".join(f"L{i}" for i in range(12))  # 12 lines overflow the band
    with pytest.raises(ValueError, match="too little room"):
        compose_code_label(bar, is_square=False, text=tall_text, layout="stack")


def test_stacked_barcode_with_short_text_succeeds():
    # The normal case still works and gives the barcode a real height.
    from PIL import Image

    bar = Image.new("L", (300, 110), 0)
    img = compose_code_label(bar, is_square=False, text="Part ABC", layout="stack")
    assert img.height == PRINT_HEAD_DOTS
    assert img.convert("L").getextrema() == (0, 255)


def test_qr_missing_library_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "qrcode", None)  # force `import qrcode` to fail
    with pytest.raises(ImportError, match=r"brother-ptouch\[qr\]"):
        compose_qr("x")


def test_barcode_missing_library_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "barcode", None)
    with pytest.raises(ImportError, match=r"brother-ptouch\[barcode\]"):
        compose_barcode("x")


# --------------------------------------------------------------------------- #
# CLI subcommands
# --------------------------------------------------------------------------- #


def test_cli_qr(tmp_path):
    pytest.importorskip("qrcode")
    from brother_ptouch.cli import main

    out = tmp_path / "q.bin"
    rc = main(["qr", "--data", "hello", "--text", "Widget", "--layout", "stack", "--out", str(out)])
    assert rc == 0
    assert decode(out.read_bytes()).warnings == []


def test_cli_barcode(tmp_path):
    pytest.importorskip("barcode")
    from brother_ptouch.cli import main

    out = tmp_path / "b.bin"
    rc = main(["barcode", "--data", "ABC-12345", "--text", "Part ABC", "--out", str(out)])
    assert rc == 0
    assert decode(out.read_bytes()).warnings == []


def test_cli_aruco(tmp_path):
    pytest.importorskip("cv2")
    from brother_ptouch.cli import main

    out = tmp_path / "a.bin"
    rc = main(["aruco", "--id", "3", "--dict", "5X5_100", "--out", str(out)])
    assert rc == 0
    assert decode(out.read_bytes()).warnings == []
