"""CLI coverage -- exercises the console entry point without hardware."""

import pytest
from PIL import Image

from brother_ptouch.cli import main
from brother_ptouch.simulator import decode


def test_text_to_out_file_decodes_clean(tmp_path):
    out = tmp_path / "t.bin"
    preview = tmp_path / "t.png"
    rc = main([
        "text", "--text", "PLA Black",
        "--font-size", "40", "--orientation", "vertical",
        "--out", str(out), "--preview", str(preview),
    ])
    assert rc == 0
    assert out.exists() and preview.exists()
    decoded = decode(out.read_bytes())
    assert decoded.tape_width_mm == 24
    assert decoded.trailer == "cut"
    assert decoded.warnings == []


def test_image_to_out_file_decodes_clean(tmp_path):
    img_path = tmp_path / "x.png"
    img = Image.new("L", (240, 120), 255)
    img.paste(0, (10, 10, 60, 110))
    img.save(img_path)
    out = tmp_path / "x.bin"
    rc = main(["image", "--file", str(img_path), "--out", str(out)])
    assert rc == 0
    decoded = decode(out.read_bytes())
    assert len(decoded.raster_lines) == decoded.raster_line_count
    assert decoded.warnings == []


def test_no_cut_flag_sets_chain_mode(tmp_path):
    out = tmp_path / "n.bin"
    rc = main(["text", "--text", "Hi", "--out", str(out), "--no-cut"])
    assert rc == 0
    decoded = decode(out.read_bytes())
    assert decoded.auto_cut is False
    assert decoded.trailer == "no-cut"


def test_out_and_printer_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        main(["text", "--text", "x", "--out", "/tmp/a.bin", "--printer", "queue"])


def test_list_returns_zero():
    # list_printers never raises; on a CI box with no printers it prints a
    # message and still exits 0.
    assert main(["list"]) == 0


def test_missing_required_arg_exits():
    with pytest.raises(SystemExit):
        main(["text"])  # --text is required
