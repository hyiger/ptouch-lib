# brother-ptouch

A small, dependency-light **Python 3.10+** library and CLI for printing labels
on a **Brother PT-P710BT** (P-touch CUBE) on **24 mm TZe tape**.

- Convert a **PNG/JPG** image to the printer's 1-bit raster format and print it.
- Render **plain text** to a label, with selectable **font**, **font size**, and
  **orientation** (horizontal / vertical).
- Generate **QR codes** (all versions that fit on 24 mm tape), **1D barcodes**
  (Code 128, EAN, UPC, …), and **ArUco markers** — optionally with a text string
  printed **beside** or **below** the code.
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
pip install brother-ptouch              # from PyPI (once published)
pip install -e ".[dev]"                 # from a clone, with test + lint extras
pip install "brother-ptouch[windows]"   # adds pywin32 for the Windows RAW path
pip install "brother-ptouch[codes]"     # adds QR + barcode + ArUco support
```

The core only needs [Pillow](https://python-pillow.org/). The code generators
are optional extras so you install only what you use:

| Extra | Pulls in | Enables |
|---|---|---|
| `[qr]` | `qrcode` | `ptouch qr` |
| `[barcode]` | `python-barcode` | `ptouch barcode` |
| `[aruco]` | `opencv-contrib-python-headless` | `ptouch aruco` |
| `[codes]` | all three | every code command |
| `[windows]` | `pywin32` | Windows RAW printing |

A code command raises a clear "install `brother-ptouch[…]`" error if its library
is missing.

## CLI

```
ptouch image   --file PATH    [output opts]
ptouch text    --text STR     [--font PATH] [--font-size N] [--orientation horizontal|vertical] [output opts]
ptouch qr      --data STR     [--ec L|M|Q|H] [--qr-version N] [code opts] [output opts]
ptouch barcode --data STR     [--symbology code128|ean13|...]  [code opts] [output opts]
ptouch aruco   --id N         [--dict 4X4_50|5X5_100|...]      [code opts] [output opts]
ptouch nozzle  NAME           [--no-text] [--no-invert] [--quiet-zone N] [code opts] [output opts]
ptouch list                   # list reachable printers

# code opts:    [--text STR] [--layout side|stack] [--font PATH] [--font-size N]
# output opts:  [--printer TARGET | --out FILE] [--preview PNG] [--size WxH]
#               [--tape MM] [--cut | --no-cut] [--margin-dots N] [--config FILE]
```

| Flag | Meaning |
|---|---|
| `--printer TARGET` | A CUPS queue name, a `usb://…` URI (from `ptouch list`), or a Windows printer name. |
| `--out FILE` | Write the raw `.bin` instead of printing (mutually exclusive with `--printer`). |
| `--preview PNG` | Also write a human-readable PNG of exactly what will print. |
| `--cut` / `--no-cut` | Feed + cut after the label (default), or chain mode (no feed/cut). |
| `--tape MM` | Tape width in mm (default 24; only 24 is fully exercised). |
| `--margin-dots N` | Leading feed before the print, in dots (default 14 ≈ 2 mm). |
| `--orientation` | `horizontal` (reads along the label) or `vertical` (reads across the tape). |
| `--size WxH` | Exact label size in mm (see [Exact size](#exact-size)). |
| `--config FILE` | TOML config supplying defaults (auto-discovered if omitted). |

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

### Codes (QR, barcode, ArUco)

Each code command optionally takes a `--text` string printed alongside the code
— `--layout side` (default) places it beside the code; `--layout stack` puts it
below, sharing the tape width.

```bash
# QR with a name beside it (auto-fits the smallest version for the payload)
ptouch qr --data "https://example.com/i/42" --text "Bin 42" --out /tmp/qr.bin

# A short QR stacked above its label
ptouch qr --data "PLA-0042" --text "PLA Black" --layout stack --printer "$P"

# Code 128 barcode with the part number below it
ptouch barcode --data "ABC-12345" --symbology code128 --text "Part ABC" --layout stack --out /tmp/bc.bin

# An ArUco marker (OpenCV dictionaries) with its id
ptouch aruco --id 7 --dict 4X4_50 --text "Marker 7" --out /tmp/ar.bin
```

Notes:
- **QR** auto-picks the smallest version for the data; any version that
  physically fits 24 mm tape is supported. Long payloads need `--layout side`
  (the QR gets the full tape height); short ones stack fine. A 4-module quiet
  zone is always included.
- **Barcode** `--symbology` is any [python-barcode](https://github.com/WhyNotHugo/python-barcode)
  type (`code128`, `code39`, `ean13`, `ean8`, `upca`, `isbn13`, …); the data
  must be valid for it.
- **ArUco** `--dict` is any OpenCV predefined dictionary, with or without the
  `DICT_` prefix (`4X4_50`, `5X5_100`, `6X6_250`, `7X7_1000`, `APRILTAG_36h11`, …);
  a white quiet zone is baked in so detectors can find it.

### Bambu nozzle markers

`ptouch nozzle NAME` reproduces the small marker the Bambu H2D/H2C hot-end camera
reads to identify the installed nozzle. These are **not** ArUco — they are a
custom Bambu 3×7 module grid (decoded from Bambu's catalog photos) carried in a
built-in table, so the command needs no extra dependencies (Pillow only).

`NAME` accepts forms like `WC0.4`, `wc.4`, `WC 0.4`, `wc4`, `0.4`, `HF0.6`,
`HFWC0.8`. Materials: stainless (none), `HF` (high flow), `WC` (tungsten
carbide), `HFWC`; diameters `0.2`/`0.4`/`0.6`/`0.8` (only `0.2` for stainless).

The marker is physically white-on-black, so by default the whole label is
**inverted** — a white marker (and white text) on a solid black field — to match
the nozzle on ordinary black-on-white tape. Use `--no-invert` for white-on-black
tape. The text matching the nozzle is added by default (the camera reportedly
checks marker *and* text); `--no-text` prints the marker alone.

```bash
# A WC 0.4 nozzle marker + "WC.4" text, inverted for black-on-white tape
ptouch nozzle WC0.4 --out /tmp/nz.bin --preview /tmp/nz.png

# Marker only, at an exact small physical size to cover a nozzle's own marker
ptouch nozzle WC0.4 --no-text --size 8x4 --out /tmp/nz.bin
```

The marker is only a few millimetres on the nozzle, so pair it with `--size`
(see below) for an exact physical size; the auto fill scales it to the full tape
width. The WC / HF-WC bit patterns came from lower-resolution source photos and
may be refined.

### Exact size

By default the content auto-fills the ~18 mm printable tape width and the label
length follows the content. Pass `--size WxH` (millimetres) to pin an exact
physical size instead — **W** runs along the label length, **H** across the tape
(max ~18 mm). The content is scaled to **H** tall (centered as a band on the
tape) and centered in a **W**-long label; content that won't fit the requested
width is rejected with a clear error (shrink `--font-size` or widen the size).

```bash
# A small asset tag: ArUco marker + "| WC.4" at exactly 16.5 mm × 5 mm
ptouch aruco --id 4 --dict 4X4_50 --text "| WC.4" --size 16.5x5 --font-size 14 --out tag.bin

# Fixed-size text label
ptouch text --text "RACK A1" --size 40x9 --out rack.bin
```

On 24 mm tape a sub-18 mm height prints as a centered band with blank tape
above/below (trim to taste). A sized label defaults to a **0 leading margin**
(so the printed length matches **W**; pass `--margin-dots` to override, and note
the printer still enforces its own minimum feed/cut). The same control is
available in the library via `LabelSize`
(`compose_text(..., size=LabelSize.from_mm(40, 9))`).

The CLI exits non-zero with the printer/stderr message on failure.

## Config file

Defaults for the printer, tape width, font, font size, orientation, auto-cut,
and margin can live in a flat TOML file, so you don't repeat them on every run.
**Precedence: CLI flag → config file → built-in default.**

```toml
# ptouch.toml
printer           = "usb://Brother/PT-P710BT?serial=000M5G671606"
tape              = 24
auto_cut          = true
margin_dots       = 14
font              = "/System/Library/Fonts/Supplemental/Arial.ttf"
font_size         = 40
orientation       = "horizontal"
layout            = "side"      # code + text layout for qr/barcode/aruco
qr_ec             = "M"
barcode_symbology = "code128"
aruco_dict        = "4X4_50"
```

```bash
ptouch text --text "PLA Black"                 # uses every default from the config
ptouch text --text "PLA Black" --no-cut        # overrides just auto_cut
ptouch text --text "PLA Black" --config ./my.toml
```

Pass `--config FILE`, or let it auto-discover the first of:
`$PTOUCH_CONFIG` → `./ptouch.toml` → `~/.config/ptouch/config.toml` →
`~/.ptouch.toml`. Every key is optional; unknown keys or bad values are
reported with a clear error. See [`ptouch.example.toml`](ptouch.example.toml).
The same loader is available programmatically as `load_config` / `resolve_config`.

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
