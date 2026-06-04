"""``ptouch`` command-line interface.

    ptouch image  --file PATH   [--printer TARGET | --out FILE] [--preview PNG] [--tape 24] [--no-cut]
    ptouch text   --text STR    [--font PATH] [--font-size N] [--orientation horizontal|vertical]
                                [--printer TARGET | --out FILE] [--preview PNG] [--tape 24] [--no-cut]
    ptouch list                 # list reachable printers
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

from . import __version__
from .encoder import encode_label
from .render import compose_image, compose_text, raster_from_composed


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
        "--tape", type=float, default=24,
        help="tape width in mm (default 24; only 24 is fully exercised)",
    )
    p.add_argument(
        "--no-cut", dest="auto_cut", action="store_false",
        help="chain mode: no feed/cut after the label",
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
    p_text.add_argument("--font", help="path to a TrueType/OpenType font (defaults to a system sans)")
    p_text.add_argument("--font-size", type=int, default=None, help="font height in px (auto-fit by default)")
    p_text.add_argument(
        "--orientation",
        choices=["horizontal", "vertical"],
        default="horizontal",
        help="text reads along the label length (horizontal) or across the tape (vertical)",
    )
    _add_output_args(p_text)

    sub.add_parser("list", help="list reachable printers")
    return parser


def _emit(args: argparse.Namespace, bitmap: bytes, raster_lines: int, composed) -> int:
    data = encode_label(
        bitmap,
        raster_lines,
        tape_width_mm=args.tape,
        auto_cut=args.auto_cut,
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

    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(data)
        print(f"wrote {len(data)} bytes -> {args.out}", file=sys.stderr)
    elif args.printer:
        from .transport import print_raster

        print_raster(args.printer, data)
        print(f"sent {len(data)} bytes -> {args.printer}", file=sys.stderr)
    else:
        # Default sink mirrors the reference CLI: a .bin in the temp dir.
        out_path = os.path.join(tempfile.gettempdir(), "label.bin")
        with open(out_path, "wb") as fh:
            fh.write(data)
        print(f"no --out/--printer given; wrote {len(data)} bytes -> {out_path}", file=sys.stderr)
    return 0


def _cmd_image(args: argparse.Namespace) -> int:
    composed = compose_image(args.file)
    bitmap, raster_lines = raster_from_composed(composed)
    return _emit(args, bitmap, raster_lines, composed)


def _cmd_text(args: argparse.Namespace) -> int:
    composed = compose_text(
        args.text,
        font_path=args.font,
        font_size=args.font_size,
        orientation=args.orientation,
    )
    bitmap, raster_lines = raster_from_composed(composed)
    return _emit(args, bitmap, raster_lines, composed)


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
