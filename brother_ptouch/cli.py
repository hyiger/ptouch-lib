"""``ptouch`` command-line interface.

    ptouch image   --file PATH    [output opts]
    ptouch text    --text STR     [--font PATH] [--font-size N] [--orientation H|V] [output opts]
    ptouch qr      --data STR     [--ec L|M|Q|H] [--qr-version N] [code opts] [output opts]
    ptouch barcode --data STR     [--symbology code128|ean13|...] [code opts] [output opts]
    ptouch aruco   --id N         [--dict 4X4_50|...] [code opts] [output opts]
    ptouch nozzle  NAME           [--no-text] [--no-invert] [code opts] [output opts]
    ptouch list                   # list reachable printers

  code opts:    [--text STR] [--layout side|stack] [--font PATH] [--font-size N]
  output opts:  [--printer TARGET | --out FILE] [--preview PNG] [--tape MM]
                [--cut | --no-cut] [--margin-dots N] [--config FILE]

Defaults for the printer, tape width, font, font size, orientation, auto-cut,
and margin can come from a TOML config file (``--config`` or an auto-discovered
``ptouch.toml`` / ``~/.config/ptouch/config.toml``). CLI flags always win over
the config, which wins over the built-in defaults. See ``config.py``.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

from . import __version__
from .config import Config, resolve_config
from .encoder import encode_label
from .render import (
    LabelSize,
    compose_aruco,
    compose_barcode,
    compose_image,
    compose_nozzle,
    compose_qr,
    compose_text,
    raster_from_composed,
)

# Built-in defaults, applied when neither a CLI flag nor the config sets a value.
_DEFAULT_TAPE = 24.0
_DEFAULT_AUTO_CUT = True
_DEFAULT_MARGIN_DOTS = 14
_DEFAULT_ORIENTATION = "horizontal"
_DEFAULT_LAYOUT = "side"
_DEFAULT_QR_EC = "M"
_DEFAULT_BARCODE_SYMBOLOGY = "code128"
_DEFAULT_ARUCO_DICT = "4X4_50"


def _first(*values):
    """Return the first non-None value (the precedence resolver)."""
    for v in values:
        if v is not None:
            return v
    return None


def _add_output_args(p: argparse.ArgumentParser) -> None:
    sink = p.add_mutually_exclusive_group()
    sink.add_argument(
        "--printer", metavar="TARGET",
        help="CUPS queue, usb:// URI, or Windows printer name",
    )
    sink.add_argument(
        "--out", metavar="FILE",
        help="write the raw .bin byte stream instead of printing",
    )
    p.add_argument(
        "--preview", metavar="PNG",
        help="also write a human-readable PNG preview of the label",
    )
    p.add_argument(
        "--tape", type=float, default=None,
        help=f"tape width in mm (default {_DEFAULT_TAPE:g}; only 24 is fully exercised)",
    )
    p.add_argument(
        "--cut", dest="auto_cut", default=None, action=argparse.BooleanOptionalAction,
        help="feed + cut after the label (default); --no-cut for chain mode",
    )
    p.add_argument(
        "--margin-dots", type=int, default=None,
        help=f"leading feed before the print, in dots (default {_DEFAULT_MARGIN_DOTS})",
    )
    p.add_argument(
        "--config", metavar="FILE",
        help="TOML config file with defaults (auto-discovered if omitted)",
    )
    p.add_argument(
        "--size", metavar="WxH",
        help="exact label size in mm (W along the length, H across the tape, "
             "max ~18mm); content is scaled to H and centered in a W-long label",
    )


def _parse_size(value: str | None) -> LabelSize | None:
    """Parse a ``--size WxH`` argument (mm) into a LabelSize, or None."""
    if value is None:
        return None
    parts = value.lower().replace(" ", "").split("x")
    if len(parts) != 2:
        raise ValueError(f"--size must be WxH in mm, e.g. 16.5x5 (got {value!r})")
    try:
        width_mm, height_mm = float(parts[0]), float(parts[1])
    except ValueError:
        raise ValueError(f"--size must be WxH in mm, e.g. 16.5x5 (got {value!r})") from None
    return LabelSize.from_mm(width_mm=width_mm, height_mm=height_mm)


def _add_text_opts(p: argparse.ArgumentParser) -> None:
    """Font options for a command that renders accompanying text."""
    p.add_argument("--font", default=None, help="path to a TrueType/OpenType font")
    p.add_argument("--font-size", type=int, default=None, help="font height in px (auto-fit by default)")


def _add_code_opts(p: argparse.ArgumentParser) -> None:
    """The --text / --layout pair shared by the code subcommands."""
    p.add_argument("--text", default=None, help="text to print beside or below the code")
    p.add_argument(
        "--layout", choices=["side", "stack"], default=None,
        help="place text beside the code (side) or below it (stack)",
    )
    _add_text_opts(p)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ptouch",
        description="Print labels on a Brother PT-P710BT (24mm TZe tape).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_image = sub.add_parser("image", help="print a PNG/JPG image as a label")
    p_image.add_argument("--file", required=True, help="path to a PNG or JPG image")
    _add_output_args(p_image)

    p_text = sub.add_parser("text", help="render and print plain text as a label")
    p_text.add_argument("--text", required=True, help="the label text (newlines stack as lines)")
    _add_text_opts(p_text)
    p_text.add_argument(
        "--orientation",
        choices=["horizontal", "vertical"],
        default=None,
        help="text reads along the label length (horizontal) or across the tape (vertical)",
    )
    _add_output_args(p_text)

    p_qr = sub.add_parser("qr", help="print a QR code (optionally with text)")
    p_qr.add_argument("--data", required=True, help="the QR payload")
    p_qr.add_argument("--ec", choices=["L", "M", "Q", "H"], default=None, help="error correction level")
    p_qr.add_argument("--qr-version", type=int, default=None, help="QR version 1-40 (auto-fit if omitted)")
    _add_code_opts(p_qr)
    _add_output_args(p_qr)

    p_bc = sub.add_parser("barcode", help="print a 1D barcode (optionally with text)")
    p_bc.add_argument("--data", required=True, help="the barcode payload")
    p_bc.add_argument(
        "--symbology", default=None,
        help="barcode type, e.g. code128, code39, ean13, ean8, upca (default code128)",
    )
    _add_code_opts(p_bc)
    _add_output_args(p_bc)

    p_ar = sub.add_parser("aruco", help="print an ArUco marker (optionally with text)")
    p_ar.add_argument("--id", type=int, required=True, dest="marker_id", help="marker id")
    p_ar.add_argument("--dict", default=None, dest="dictionary", help="ArUco dictionary (default 4X4_50)")
    _add_code_opts(p_ar)
    _add_output_args(p_ar)

    p_nz = sub.add_parser("nozzle", help="print a Bambu nozzle label (white-on-black)")
    p_nz.add_argument("nozzle", help="nozzle name, e.g. WC0.4, HF0.6, HFWC0.8, 0.4")
    p_nz.add_argument(
        "--generated", action="store_true",
        help="build the label from the decoded marker grid + a system font "
             "(customizable) instead of the exact photo-derived band (default)",
    )
    p_nz.add_argument(
        "--no-text", dest="no_text", action="store_true",
        help="(--generated only) print the marker alone, without the text",
    )
    p_nz.add_argument(
        "--invert", default=True, action=argparse.BooleanOptionalAction,
        help="invert to white-on-black for ordinary black-on-white tape "
             "(default); --no-invert for white-on-black tape",
    )
    p_nz.add_argument(
        "--quiet-zone", type=int, default=0, dest="quiet_zone",
        help="black border around the marker, in modules (default 0; the "
             "inverted field / black tape already surrounds it)",
    )
    p_nz.add_argument(
        "--separator", default=True, action=argparse.BooleanOptionalAction,
        help="draw the | divider between the marker and the text, as on the "
             "nozzle (default); --no-separator to omit it",
    )
    _add_code_opts(p_nz)
    _add_output_args(p_nz)

    sub.add_parser("list", help="list reachable printers")
    return parser


def _emit(args: argparse.Namespace, cfg: Config, bitmap: bytes, raster_lines: int, composed) -> int:
    tape = _first(args.tape, cfg.tape, _DEFAULT_TAPE)
    auto_cut = _first(args.auto_cut, cfg.auto_cut, _DEFAULT_AUTO_CUT)
    # A sized label's requested length IS the printed length, so default to a
    # 0 leading margin -- otherwise the ~2mm default feed makes the cut label
    # longer than the advertised W. An explicit --margin-dots/config still wins.
    # (The printer enforces its own minimum feed regardless.) (Codex review, PR #3.)
    default_margin = 0 if args.size else _DEFAULT_MARGIN_DOTS
    margin_dots = _first(args.margin_dots, cfg.margin_dots, default_margin)

    data = encode_label(
        bitmap,
        raster_lines,
        tape_width_mm=tape,
        auto_cut=auto_cut,
        margin_dots=margin_dots,
    )

    if args.preview:
        # The exact pixels that print, shown the way a person reads them.
        composed.point(lambda p: 0 if p < 128 else 255).save(args.preview)
        print(f"preview PNG -> {args.preview}", file=sys.stderr)

    length_mm = raster_lines / 7.087  # 180 dpi -> ~7.087 dots/mm
    print(
        f"rendered: {raster_lines} raster lines (~{length_mm:.1f} mm), {len(data)} bytes",
        file=sys.stderr,
    )

    # --out (explicit local write) wins; else a --printer flag; else a config
    # printer; else a scratch file in the temp dir.
    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(data)
        print(f"wrote {len(data)} bytes -> {args.out}", file=sys.stderr)
        return 0

    target = _first(args.printer, cfg.printer)
    if target:
        from .transport import print_raster

        print_raster(target, data)
        print(f"sent {len(data)} bytes -> {target}", file=sys.stderr)
        return 0

    out_path = os.path.join(tempfile.gettempdir(), "label.bin")
    with open(out_path, "wb") as fh:
        fh.write(data)
    print(f"no --out/--printer/config printer; wrote {len(data)} bytes -> {out_path}", file=sys.stderr)
    return 0


def _cmd_image(args: argparse.Namespace) -> int:
    cfg = resolve_config(args.config)
    composed = compose_image(args.file, size=_parse_size(args.size))
    bitmap, raster_lines = raster_from_composed(composed)
    return _emit(args, cfg, bitmap, raster_lines, composed)


def _cmd_text(args: argparse.Namespace) -> int:
    cfg = resolve_config(args.config)
    composed = compose_text(
        args.text,
        font_path=_first(args.font, cfg.font),
        font_size=_first(args.font_size, cfg.font_size),
        orientation=_first(args.orientation, cfg.orientation, _DEFAULT_ORIENTATION),
        size=_parse_size(args.size),
    )
    bitmap, raster_lines = raster_from_composed(composed)
    return _emit(args, cfg, bitmap, raster_lines, composed)


def _cmd_qr(args: argparse.Namespace) -> int:
    cfg = resolve_config(args.config)
    composed = compose_qr(
        args.data,
        error_correction=_first(args.ec, cfg.qr_ec, _DEFAULT_QR_EC),
        version=args.qr_version,
        text=args.text,
        layout=_first(args.layout, cfg.layout, _DEFAULT_LAYOUT),
        font_path=_first(args.font, cfg.font),
        font_size=_first(args.font_size, cfg.font_size),
        size=_parse_size(args.size),
    )
    bitmap, raster_lines = raster_from_composed(composed)
    return _emit(args, cfg, bitmap, raster_lines, composed)


def _cmd_barcode(args: argparse.Namespace) -> int:
    cfg = resolve_config(args.config)
    composed = compose_barcode(
        args.data,
        symbology=_first(args.symbology, cfg.barcode_symbology, _DEFAULT_BARCODE_SYMBOLOGY),
        text=args.text,
        layout=_first(args.layout, cfg.layout, _DEFAULT_LAYOUT),
        font_path=_first(args.font, cfg.font),
        font_size=_first(args.font_size, cfg.font_size),
        size=_parse_size(args.size),
    )
    bitmap, raster_lines = raster_from_composed(composed)
    return _emit(args, cfg, bitmap, raster_lines, composed)


def _cmd_aruco(args: argparse.Namespace) -> int:
    cfg = resolve_config(args.config)
    composed = compose_aruco(
        args.marker_id,
        dictionary=_first(args.dictionary, cfg.aruco_dict, _DEFAULT_ARUCO_DICT),
        text=args.text,
        layout=_first(args.layout, cfg.layout, _DEFAULT_LAYOUT),
        font_path=_first(args.font, cfg.font),
        font_size=_first(args.font_size, cfg.font_size),
        size=_parse_size(args.size),
    )
    bitmap, raster_lines = raster_from_composed(composed)
    return _emit(args, cfg, bitmap, raster_lines, composed)


def _cmd_nozzle(args: argparse.Namespace) -> int:
    from .codes import nozzle_text

    cfg = resolve_config(args.config)
    # The bundled band IS the 16x5mm heat-sink face: when no size is given, default
    # to that exact physical size so the photo band prints 1:1. Set args.size (not
    # just the parsed value) so _emit() also suppresses the default leading feed
    # margin -- otherwise the "actual size" label would be ~2mm longer than 16mm.
    if not args.generated and args.size is None:
        args.size = "16x5"
    size = _parse_size(args.size)
    if args.generated:
        text = None if args.no_text else _first(args.text, nozzle_text(args.nozzle))
        composed = compose_nozzle(
            args.nozzle,
            source="generated",
            text=text,
            invert=args.invert,
            quiet_zone_modules=args.quiet_zone,
            separator=args.separator,
            layout=_first(args.layout, cfg.layout, _DEFAULT_LAYOUT),
            font_path=_first(args.font, cfg.font),
            font_size=_first(args.font_size, cfg.font_size),
            size=size,
        )
    else:
        composed = compose_nozzle(args.nozzle, source="photo", invert=args.invert, size=size)
    bitmap, raster_lines = raster_from_composed(composed)
    return _emit(args, cfg, bitmap, raster_lines, composed)


def _cmd_list(args: argparse.Namespace) -> int:
    from .transport import list_printers

    devices = list_printers()
    if not devices:
        print("No printers found.", file=sys.stderr)
        return 0
    for d in devices:
        badge = " *" if d.looks_like_printer else "  "
        print(f"{badge} {d.friendly_name}\n     {d.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "image": _cmd_image,
        "text": _cmd_text,
        "qr": _cmd_qr,
        "barcode": _cmd_barcode,
        "aruco": _cmd_aruco,
        "nozzle": _cmd_nozzle,
        "list": _cmd_list,
    }
    try:
        return handlers[args.command](args)
    except Exception as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
