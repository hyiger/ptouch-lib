"""brother_ptouch -- a dependency-light library + CLI for printing labels on a
Brother PT-P710BT (P-touch CUBE) over USB on 24mm TZe tape.

Public API
----------
Encoding (pure, zero-dependency, byte-exact)::

    from brother_ptouch import encode_label, pack_grayscale_bitmap

Rendering (Pillow)::

    from brother_ptouch import render_image, render_text

Transport (OS print system)::

    from brother_ptouch import list_printers, print_raster

Simulation (decode a stream back to a PNG -- for tests / inspection)::

    from brother_ptouch import decode, to_preview_image

Typical one-shot::

    from brother_ptouch import render_text, encode_label, print_raster
    bitmap, lines = render_text("PLA Black", font_size=40, orientation="vertical")
    print_raster("usb://Brother/PT-P710BT?serial=...", encode_label(bitmap, lines))
"""

from __future__ import annotations

__version__ = "0.1.0"

from .encoder import (
    BYTES_PER_RASTER_LINE,
    MAX_LABEL_BYTES,
    PRINT_HEAD_DOTS,
    VALID_TAPE_WIDTHS_MM,
    encode_label,
    pack_grayscale_bitmap,
    pack_grayscale_row,
)
from .render import (
    compose_image,
    compose_text,
    image_to_raster,
    raster_from_composed,
    render_image,
    render_text,
    text_to_raster,
)
from .simulator import DecodeError, DecodeResult, decode, to_preview_image
from .transport import PrinterDevice, PrintError, list_printers, print_raster

__all__ = [
    "__version__",
    # encoder
    "encode_label",
    "pack_grayscale_row",
    "pack_grayscale_bitmap",
    "PRINT_HEAD_DOTS",
    "BYTES_PER_RASTER_LINE",
    "VALID_TAPE_WIDTHS_MM",
    "MAX_LABEL_BYTES",
    # render
    "render_image",
    "render_text",
    "image_to_raster",
    "text_to_raster",
    "compose_image",
    "compose_text",
    "raster_from_composed",
    # transport
    "list_printers",
    "print_raster",
    "PrinterDevice",
    "PrintError",
    # simulator
    "decode",
    "to_preview_image",
    "DecodeResult",
    "DecodeError",
]
