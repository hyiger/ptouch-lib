# CLAUDE.md

Guidance for AI agents (and humans) working in this repository.

## What this is

`brother-ptouch` — a Python 3.10+ library and `ptouch` CLI that renders labels
(image, text, QR, barcode, ArUco, and **Bambu nozzle marker bands**) and prints
them on a **Brother PT-P710BT** (P-touch CUBE) on 24 mm TZe tape, over USB via
the host OS print system (CUPS / Windows spooler). The core needs only **Pillow**
(plus `tomli` on Python 3.10, where `tomllib` is not yet stdlib); the code
generators are optional extras.

## Architecture — the pipeline is `compose → raster → encode → transport`

| Module | Role |
|---|---|
| `render.py` | `compose_*()` build a PIL **`"L"`** image in human-reading orientation (`length × 128` dots). `raster_from_composed()` turns it into the 1-bit bitmap. `LabelSize` / `--size` live here. |
| `encoder.py` | Pure, **zero-dependency, byte-exact** Brother raster protocol. `encode_label(bitmap, raster_lines, ...)`. |
| `codes.py` | QR / barcode / ArUco generators (optional libs, **lazy-imported**) + Bambu nozzle markers (`nozzle_image` generated, `nozzle_band_image` bundled photo). |
| `cli.py` | argparse `ptouch` CLI. `_emit()` is the shared output sink: `--out` → `--printer` → config printer → temp file. |
| `config.py` | Flat TOML defaults, **strict** (unknown keys are an error). Precedence: CLI flag > config > built-in default. |
| `transport.py` | Print via CUPS (`lp -o raw`, an auto-managed queue) or Windows (pywin32 / PowerShell fallback); `list_printers()`. |
| `simulator.py` | Decode a printed byte stream back to a PNG — the basis of most tests. |
| `nozzle_bands/*.png` | 13 cleaned photos of the real nozzle bands, shipped as package data. |

## Critical invariants — do not break these

- **#587 raster-mirror trap** (`render.raster_from_composed`): after rotating the
  composed image 90° CW you MUST reverse the raster-line order (`ImageOps.flip`)
  before packing, or the label prints mirrored (text backwards, QR unscannable).
  Hardware-confirmed; there are tests for it.
- Composed images are always **128 dots tall** (`PRINT_HEAD_DOTS`); the rasterizer
  rejects anything else.
- `encoder.py` is **byte-exact** — a port of a hardware-validated implementation.
  Don't "tidy" the protocol bytes; tests assert exact bytes and round-trip through
  the simulator. Resolution is 180 dpi → `DOTS_PER_MM = 180/25.4 ≈ 7.087`.
- `--size WxH` is **mm**: W = length along the feed, H = content band height
  (≤ ~18 mm / 128 dots). Sized labels default to a **0 leading margin** so the cut
  length equals W (`_emit` keys off `args.size`).
- **Nozzle**: the default is the **photo band** (`source="photo"`, exact, and the
  only variant recognized by an H2D). `--generated` builds from the decoded 3×7
  marker grid + a system font (not hardware-tested). A band image is the 16×5 mm
  heat-sink face, so no `--size` prints it at true physical size. White-on-black
  by default (black-on-white tape); `--no-invert` for white-on-black tape.

## Dev commands (this repo uses `uv`)

```bash
uv run --extra dev pytest -q                          # tests (core)
uv run --extra dev ruff check brother_ptouch/ tests/  # lint
uv run --python 3.12 --extra codes pytest -q          # incl. QR/barcode/ArUco tests (need opencv etc.)
uv build                                               # wheel + sdist
```

- The optional-dep tests **skip gracefully** (`pytest.importorskip`) on a
  Pillow-only environment, so the core suite runs anywhere.
- `nozzle_bands/*.png` and `_win_raw_print.ps1` must end up in the built wheel
  (currently via hatchling's default package-data inclusion).

## Conventions

- **ruff** (rules E,F,I,W,UP,B), line length **110**, target `py310`.
- Google-style docstrings; type hints throughout (`from __future__ import annotations`).
- Optional deps (`cv2`, `qrcode`, `barcode`, `pywin32`) are **lazy-imported inside
  functions** so importing the package pulls in only the core deps (Pillow, plus
  `tomli` on 3.10); a missing optional lib raises a clear
  `pip install 'brother-ptouch[...]'` error.

## Adding a new code command (the established pattern)

1. A generator in `codes.py` returning a **black-on-white `"L"`** image (1 px/module).
2. `compose_X()` in `render.py` calling `compose_code_label(img, is_square=..., ...)`.
3. A subcommand + `_cmd_X()` in `cli.py`; register it in `_build_parser()` and the
   `handlers` dict in `main()`.
4. Export the public functions in `__init__.py`; add tests that round-trip through
   `simulator.decode` (see `tests/test_codes.py` / `tests/test_nozzle.py`).

## Hardware / status

- Validated on **macOS over USB**. The Windows/Linux transport paths are
  implemented but **not hardware-confirmed**.
- The Bambu nozzle relabel is **H2D-confirmed**: a printed `WC.4` label made a
  third-party Diamondback nozzle read as WC. Only WC.4 has been hardware-tested.

## Workflow

- All changes go through a **feature branch → PR → CI (pytest + ruff on 3.10–3.13
  + macOS) → Codex review (`@codex review`, resolve every thread) → merge**. Don't
  push to `main` directly.
- Backlog and audit findings live in **GitHub issues**.
