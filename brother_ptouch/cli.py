"""``ptouch`` command-line interface.

    ptouch image  --file PATH   [--printer TARGET | --out FILE] [--preview PNG]
                                [--tape MM] [--cut | --no-cut] [--margin-dots N] [--config FILE]
    ptouch text   --text STR    [--font PATH] [--font-size N] [--orientation horizontal|vertical]
                                [--printer TARGET | --out FILE] [--preview PNG]
                                [--tape MM] [--cut | --no-cut] [--margin-dots N] [--config FILE]
    ptouch list                 # list reachable printers

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
from .render import compose_image, compose_text, raster_from_composed

# Built-in defaults, applied when neither a CLI flag nor the config sets a value.
_DEFAULT_TAPE = 24.0
_DEFAULT_AUTO_CUT = True
_DEFAULT_MARGIN_DOTS = 14
_DEFAULT_ORIENTATION = "horizontal"


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
    p_text.add_argument("--font", default=None, help="path to a TrueType/OpenType font")
    p_text.add_argument("--font-size", type=int, default=None, help="font height in px (auto-fit by default)")
    p_text.add_argument(
        "--orientation",
        choices=["horizontal", "vertical"],
        default=None,
        help="text reads along the label length (horizontal) or across the tape (vertical)",
    )
    _add_output_args(p_text)

    sub.add_parser("list", help="list reachable printers")
    return parser


def _emit(args: argparse.Namespace, cfg: Config, bitmap: bytes, raster_lines: int, composed) -> int:
    tape = _first(args.tape, cfg.tape, _DEFAULT_TAPE)
    auto_cut = _first(args.auto_cut, cfg.auto_cut, _DEFAULT_AUTO_CUT)
    margin_dots = _first(args.margin_dots, cfg.margin_dots, _DEFAULT_MARGIN_DOTS)

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
    composed = compose_image(args.file)
    bitmap, raster_lines = raster_from_composed(composed)
    return _emit(args, cfg, bitmap, raster_lines, composed)


def _cmd_text(args: argparse.Namespace) -> int:
    cfg = resolve_config(args.config)
    composed = compose_text(
        args.text,
        font_path=_first(args.font, cfg.font),
        font_size=_first(args.font_size, cfg.font_size),
        orientation=_first(args.orientation, cfg.orientation, _DEFAULT_ORIENTATION),
    )
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
    handlers = {"image": _cmd_image, "text": _cmd_text, "list": _cmd_list}
    try:
        return handlers[args.command](args)
    except Exception as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
