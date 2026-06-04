# brother-ptouch

A small, dependency-light **Python 3.10+** library and CLI for printing labels
on a **Brother PT-P710BT** (P-touch CUBE) on **24 mm TZe tape**.

- Convert a **PNG/JPG** image to the printer's 1-bit raster format and print it.
- Render **plain text** to a label, with selectable **font**, **font size**, and
  **orientation** (horizontal / vertical).
- Print over **USB** through the host OS print system (CUPS on macOS/Linux, the
  spooler on Windows) — the PT-P710BT is a USB printer-class device on the
  desktop, *not* a serial/Bluetooth device.
- Or write the raw byte stream to a **file** for inspection, a simulator, or CI.

The wire-format encoder is a byte-for-byte port of a hardware-validated
TypeScript implementation; the protocol traps it encodes (the media-width
rounding, the auto-cut / no-chain coupling, and the **#587 raster-line mirror
fix**) were learned on real hardware. See [`brother_ptouch/encoder.py`](brother_ptouch/encoder.py).

> Validated on **macOS over USB**. The Windows and Linux transport paths are
> implemented but **need on-hardware confirmation**.

## Install

```bash
pip install brother-ptouch          # from PyPI (once published)
pip install -e ".[dev]"             # from a clone, with test + lint extras
pip install "brother-ptouch[windows]"   # adds pywin32 for the Windows RAW path
```

Only dependency is [Pillow](https://python-pillow.org/). `pywin32` is an
optional extra used by the Windows transport.

## CLI

```
ptouch image  --file PATH   [--printer TARGET | --out FILE] [--preview PNG] [--tape 24] [--no-cut]
ptouch text   --text STR    [--font PATH] [--font-size N] [--orientation horizontal|vertical]
                            [--printer TARGET | --out FILE] [--preview PNG] [--tape 24] [--no-cut]
ptouch list                 # list reachable printers
```

| Flag | Meaning |
|---|---|
| `--printer TARGET` | A CUPS queue name, a `usb://…` URI (from `ptouch list`), or a Windows printer name. |
| `--out FILE` | Write the raw `.bin` instead of printing (mutually exclusive with `--printer`). |
| `--preview PNG` | Also write a human-readable PNG of exactly what will print. |
| `--no-cut` | Chain mode — no feed/cut after the label (default is auto-cut). |
| `--tape` | Tape width in mm (default 24; only 24 is fully exercised). |
| `--orientation` | `horizontal` (reads along the label) or `vertical` (reads across the tape). |

Examples:

```bash
# Find the printer
ptouch list

# Print an image
ptouch image --file logo.png --printer "usb://Brother/PT-P710BT?serial=000M5G671606"

# Print vertical text at a fixed size
ptouch text --text "PLA Black" --font-size 40 --orientation vertical \
  --printer "usb://Brother/PT-P710BT?serial=000M5G671606"

# Render to a file (no hardware) + a preview PNG
ptouch text --text "ABS White" --out /tmp/label.bin --preview /tmp/label.png
```

The CLI exits non-zero with the printer/stderr message on failure.

## Library

```python
from brother_ptouch import (
    render_text, render_image, encode_label,
    list_printers, print_raster,
    decode, to_preview_image,
)

# Text -> packed 1-bit bitmap + raster-line count
bitmap, lines = render_text("PLA Black", font_size=40, orientation="vertical")

# Serialize to the Brother raster command stream
data = encode_label(bitmap, lines, tape_width_mm=24, auto_cut=True)

# Print it (or write `data` to a file)
print_raster("usb://Brother/PT-P710BT?serial=...", data)

# Hardware-free: decode a stream back to a PNG to verify it
to_preview_image(decode(data)).save("preview.png")
```

`render_image` accepts a path or a Pillow `Image`; both render functions return
`(bitmap, raster_lines)` ready for `encode_label`.

## How it works

- **Geometry** — 128-dot, 180 dpi print head ⇒ every raster line is exactly 16
  bytes. A label is composed `length × 128` in human-reading orientation, then
  rotated 90° CW so each output row is one raster line, then the raster-line
  order is **reversed** (the hardware un-mirror) before packing.
- **Encoder** (`encoder.py`) — pure, zero-dependency, byte-exact. Emits the
  invalidate / init / raster-mode / print-info / mode / expansion / margin /
  compression / raster-lines / trailer sequence.
- **Transport** (`transport.py`) — CUPS `lp -o raw` (auto-managing a hidden raw
  queue for `usb://` devices) or the Windows spooler RAW datatype. `ptouch list`
  scopes `lpinfo` to the `usb` scheme so it never hangs probing the network.
- **Simulator** (`simulator.py`) — decodes a byte stream back to a PNG and
  validates structure, so the whole pipeline is testable in CI with no printer.

## Develop

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```

CI runs the test suite on Python 3.10–3.13 (Linux) plus a macOS smoke test.

## License

MIT — see [LICENSE](LICENSE).
