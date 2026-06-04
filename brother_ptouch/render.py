"""Pillow rendering pipeline: image / text -> the printer's raster bitmap.

One shared path turns composed content into the ``raster_lines x 128``
grayscale buffer the encoder wants, applying the hardware-validated
compose -> threshold -> rotate 90 CW -> reverse-raster-line-order -> pack
pipeline.

THE MIRROR TRAP (#587, hardware-confirmed)
    After rotating the composed image 90 CW for raster output you MUST reverse
    the order of the raster lines before packing. Feeding them in rotate order
    prints the label mirrored along its length (text backwards, a QR reversed
    and unscannable). The printer's physical feed direction is opposite the
    raster-line order. This is a pure reversal of whole lines; each line's
    content is untouched. See :func:`raster_from_composed`.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Union

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import codes
from .encoder import (
    BYTES_PER_RASTER_LINE,
    MAX_LABEL_BYTES,
    PRINT_HEAD_DOTS,
    pack_grayscale_bitmap,
)

__all__ = [
    "compose_image",
    "compose_text",
    "compose_code_label",
    "compose_qr",
    "compose_barcode",
    "compose_aruco",
    "raster_from_composed",
    "image_to_raster",
    "text_to_raster",
    "qr_to_raster",
    "barcode_to_raster",
    "aruco_to_raster",
    "render_image",
    "render_text",
]

#: Horizontal padding (dots, ~2mm) at each end of the printable length.
HORIZONTAL_PADDING_DOTS = 14
#: Vertical padding above/below content inside the 128-dot print band.
VERTICAL_PADDING_DOTS = 6
#: Gap between a code and its accompanying text, in dots.
GAP_DOTS = 12
#: Line leading multiplier (rendered line box height / font px).
LINE_LEADING = 1.18
#: Default text height when --font-size is omitted (auto-fit shrinks it).
DEFAULT_FONT_SIZE = 48
#: Smaller default for the text band stacked under a code.
STACK_FONT_SIZE = 28
#: Fraction of the print band reserved for stacked text (rest is the code).
STACK_TEXT_FRACTION = 0.4
#: Floor on the auto-fit font size, so tiny tape never produces 0px text.
MIN_FONT_PX = 8

#: Defensive cap on label length (raster lines), derived from the encoder's
#: 5 MB byte-stream cap. ~263k lines ~= 37 m of tape -- a runaway render.
MAX_RASTER_LINES = (MAX_LABEL_BYTES - 200) // (3 + BYTES_PER_RASTER_LINE)

ImageSource = Union[str, "Path", Image.Image]

# System sans-serif candidates, tried in order before falling back to the
# scalable font Pillow bundles (DejaVuSans via load_default(size=...)).
_SYSTEM_FONT_CANDIDATES = (
    # macOS
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    # Windows
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)


def _load_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont:
    """Resolve a TrueType font at ``size`` px.

    Honors an explicit ``font_path``; otherwise tries common system sans
    fonts, then falls back to the scalable font Pillow bundles so text always
    renders (notably in CI, where no system fonts may be installed).
    """
    size = max(1, int(size))
    if font_path:
        return ImageFont.truetype(font_path, size)
    for candidate in _SYSTEM_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    # Pillow >= 10.1 returns a scalable DejaVuSans when given a size.
    return ImageFont.load_default(size=size)


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    return max(1, math.ceil((ascent + descent) * LINE_LEADING))


def _block_width(font: ImageFont.FreeTypeFont, lines: list[str]) -> int:
    return max(1, math.ceil(max(font.getlength(line) for line in lines)))


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    # Keep interior blank lines but ensure at least one line to render.
    return lines if lines else [""]


def _fit_horizontal(
    lines: list[str], font_path: str | None, base_px: int, band: int
) -> ImageFont.FreeTypeFont:
    """Shrink the font until the N stacked lines fit ``band`` dots tall."""
    n = len(lines)
    px = max(MIN_FONT_PX, int(base_px))
    while px >= MIN_FONT_PX:
        font = _load_font(font_path, px)
        if _line_height(font) * n <= band:
            return font
        px -= 1
    return _load_font(font_path, MIN_FONT_PX)


def _fit_vertical(
    lines: list[str], font_path: str | None, base_px: int, band: int
) -> ImageFont.FreeTypeFont:
    """Shrink the font until the widest line fits ``band`` dots (after the
    90-degree rotation, a line's width runs across the tape)."""
    px = max(MIN_FONT_PX, int(base_px))
    while px >= MIN_FONT_PX:
        font = _load_font(font_path, px)
        if _block_width(font, lines) <= band:
            return font
        px -= 1
    return _load_font(font_path, MIN_FONT_PX)


def _render_text_block(font: ImageFont.FreeTypeFont, lines: list[str]) -> Image.Image:
    """Render multi-line text as black-on-white into a tight ``"L"`` image."""
    line_h = _line_height(font)
    block_w = _block_width(font, lines)
    block_h = line_h * len(lines)
    block = Image.new("L", (block_w, block_h), 255)
    draw = ImageDraw.Draw(block)
    for i, line in enumerate(lines):
        # anchor "la" = left edge, ascender top -> predictable stacking.
        draw.text((0, i * line_h), line, font=font, fill=0, anchor="la")
    return block


def compose_text(
    text: str,
    *,
    font_path: str | None = None,
    font_size: int | None = None,
    orientation: str = "horizontal",
) -> Image.Image:
    """Compose a text label into a ``length x 128`` ``"L"`` image in
    human-reading orientation (height = the 128-dot tape width).

    Args:
        text: The label text. Newlines stack as multiple lines within the band.
        font_path: TrueType/OpenType font path. Defaults to a system sans, then
            Pillow's bundled scalable font.
        font_size: Font height in px. Auto-fit shrinks it to fit the band; when
            omitted a sensible default is auto-fit from scratch.
        orientation: ``"horizontal"`` (reads along the label length) or
            ``"vertical"`` (reads across the tape).

    Returns:
        A Pillow ``Image`` (mode ``"L"``), ready for :func:`raster_from_composed`.
    """
    if orientation not in ("horizontal", "vertical"):
        raise ValueError(f"orientation must be 'horizontal' or 'vertical', got {orientation!r}")
    lines = _split_lines(text)
    band = PRINT_HEAD_DOTS - 2 * VERTICAL_PADDING_DOTS
    base_px = font_size if font_size else DEFAULT_FONT_SIZE

    if orientation == "horizontal":
        font = _fit_horizontal(lines, font_path, base_px, band)
        block = _render_text_block(font, lines)
        footprint = block
    else:
        font = _fit_vertical(lines, font_path, base_px, band)
        block = _render_text_block(font, lines)
        # Rotate 90 CW so the text reads top-to-bottom across the tape.
        footprint = block.transpose(Image.Transpose.ROTATE_270)

    foot_w, foot_h = footprint.size
    width = foot_w + 2 * HORIZONTAL_PADDING_DOTS
    canvas = Image.new("L", (width, PRINT_HEAD_DOTS), 255)
    top = max(0, (PRINT_HEAD_DOTS - foot_h) // 2)
    canvas.paste(footprint, (HORIZONTAL_PADDING_DOTS, top))
    return canvas


def compose_image(source: ImageSource) -> Image.Image:
    """Compose an image label into a ``length x 128`` ``"L"`` image.

    The image is converted to grayscale and scaled so its height fills the
    128-dot tape width (the cross-axis), preserving aspect ratio; the width
    becomes the label length. For a typical landscape image the long edge thus
    becomes the label length.

    Args:
        source: A file path or an already-open Pillow ``Image``.

    Returns:
        A Pillow ``Image`` (mode ``"L"``), ready for :func:`raster_from_composed`.
    """
    img = source if isinstance(source, Image.Image) else Image.open(source)
    img = img.convert("L")
    w, h = img.size
    if h == 0 or w == 0:
        raise ValueError("source image has a zero dimension")
    new_h = PRINT_HEAD_DOTS
    new_w = max(1, round(w * PRINT_HEAD_DOTS / h))
    if (new_w, new_h) != (w, h):
        img = img.resize((new_w, new_h), Image.LANCZOS)
    return img


def raster_from_composed(composed: Image.Image) -> tuple[bytes, int]:
    """Run a composed ``length x 128`` image through the shared raster pipeline.

    Threshold -> rotate 90 CW -> **reverse raster-line order** (#587) ->
    pack to the 1-bit bitmap the encoder expects.

    Args:
        composed: A ``length x 128`` image (any mode; converted to ``"L"``).

    Returns:
        ``(bitmap, raster_lines)`` -- a packed 1-bit bitmap and its line count,
        ready for :func:`brother_ptouch.encoder.encode_label`.

    Raises:
        ValueError: if the composed height is not 128, or the label is longer
            than :data:`MAX_RASTER_LINES`.
    """
    if composed.mode != "L":
        composed = composed.convert("L")
    if composed.height != PRINT_HEAD_DOTS:
        raise ValueError(
            f"composed image must be {PRINT_HEAD_DOTS} dots tall, got {composed.height}"
        )

    # Rotate 90 CW so each output row is one raster line (size -> 128 x length).
    rotated = composed.rotate(-90, expand=True)
    if rotated.width != PRINT_HEAD_DOTS:
        raise ValueError(
            f"internal error: rotated width is {rotated.width}, expected {PRINT_HEAD_DOTS}"
        )
    raster_lines = rotated.height
    if raster_lines < 1:
        raise ValueError("composed image has no length")
    if raster_lines > MAX_RASTER_LINES:
        raise ValueError(
            f"label is {raster_lines} raster lines, exceeding the safety cap of "
            f"{MAX_RASTER_LINES} (~5 MB byte stream)"
        )

    # Reverse the raster-line order, then threshold to pure black/white. The
    # reversal is the #587 hardware un-mirror (see module docstring).
    reversed_img = ImageOps.flip(rotated)
    bw = reversed_img.point(lambda p: 0 if p < 128 else 255)
    grayscale = bw.tobytes()
    bitmap = pack_grayscale_bitmap(grayscale, raster_lines)
    return bitmap, raster_lines


def image_to_raster(source: ImageSource) -> tuple[bytes, int]:
    """Render an image file/object straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_image(source))


def text_to_raster(
    text: str,
    *,
    font_path: str | None = None,
    font_size: int | None = None,
    orientation: str = "horizontal",
) -> tuple[bytes, int]:
    """Render text straight to ``(bitmap, raster_lines)``."""
    composed = compose_text(
        text, font_path=font_path, font_size=font_size, orientation=orientation
    )
    return raster_from_composed(composed)


def _text_block(text, font_path, font_size, max_h, default_size):
    """Render a horizontal multi-line text block whose height fits ``max_h``."""
    lines = _split_lines(text)
    base = font_size if font_size else default_size
    font = _fit_horizontal(lines, font_path, base, max_h)
    return _render_text_block(font, lines)


def _fit_code(code: Image.Image, is_square: bool, max_h: int) -> Image.Image:
    """Scale a code image to fit ``max_h`` dots tall.

    Square codes (QR/ArUco) scale by the largest *integer* factor so modules
    stay crisp; barcodes (whose height carries no data) are scaled to exactly
    ``max_h`` with their bar widths -- the data -- preserved.
    """
    w, h = code.size
    if is_square:
        factor = max_h // h
        if factor < 1:
            raise ValueError(
                f"code is {w}x{h} dots and does not fit the available {max_h} dots "
                "on 24mm tape -- shorten the payload/text, or use --layout side"
            )
        if factor > 1:
            code = code.resize((w * factor, h * factor), Image.NEAREST)
        return code
    if h != max_h:
        code = code.resize((w, max(1, max_h)), Image.NEAREST)
    return code


def compose_code_label(
    code: Image.Image,
    *,
    is_square: bool,
    text: str | None = None,
    layout: str = "side",
    font_path: str | None = None,
    font_size: int | None = None,
) -> Image.Image:
    """Compose a code (QR/barcode/ArUco) -- optionally with a text string --
    into a ``length x 128`` ``"L"`` image in human-reading orientation.

    Args:
        code: A black-on-white code image (from :mod:`brother_ptouch.codes`).
        is_square: True for QR/ArUco (square, integer-scaled), False for
            barcodes (height scaled to fit, bar widths preserved).
        text: Optional text to print alongside the code.
        layout: ``"side"`` (code and text side by side along the length) or
            ``"stack"`` (code above, text below, sharing the tape width).
        font_path, font_size: Font for the accompanying text.

    Returns:
        A Pillow ``Image`` (mode ``"L"``), ready for :func:`raster_from_composed`.
    """
    if layout not in ("side", "stack"):
        raise ValueError(f"layout must be 'side' or 'stack', got {layout!r}")
    text = text if (text and text.strip()) else None
    band = PRINT_HEAD_DOTS - 2 * VERTICAL_PADDING_DOTS

    if layout == "side":
        fitted = _fit_code(code, is_square, band)
        cw, ch = fitted.size
        block = _text_block(text, font_path, font_size, band, DEFAULT_FONT_SIZE) if text else None
        width = 2 * HORIZONTAL_PADDING_DOTS + cw + (GAP_DOTS + block.width if block else 0)
        canvas = Image.new("L", (width, PRINT_HEAD_DOTS), 255)
        canvas.paste(fitted, (HORIZONTAL_PADDING_DOTS, (PRINT_HEAD_DOTS - ch) // 2))
        if block:
            x = HORIZONTAL_PADDING_DOTS + cw + GAP_DOTS
            canvas.paste(block, (x, (PRINT_HEAD_DOTS - block.height) // 2))
        return canvas

    # stack: code on top, text below, sharing the 128-dot band.
    if text:
        block = _text_block(text, font_path, font_size, round(band * STACK_TEXT_FRACTION), STACK_FONT_SIZE)
        fitted = _fit_code(code, is_square, band - block.height - GAP_DOTS)
        cw, ch = fitted.size
        content_h = ch + GAP_DOTS + block.height
        width = max(cw, block.width) + 2 * HORIZONTAL_PADDING_DOTS
        canvas = Image.new("L", (width, PRINT_HEAD_DOTS), 255)
        top = (PRINT_HEAD_DOTS - content_h) // 2
        canvas.paste(fitted, ((width - cw) // 2, top))
        canvas.paste(block, ((width - block.width) // 2, top + ch + GAP_DOTS))
        return canvas

    fitted = _fit_code(code, is_square, band)
    cw, ch = fitted.size
    width = cw + 2 * HORIZONTAL_PADDING_DOTS
    canvas = Image.new("L", (width, PRINT_HEAD_DOTS), 255)
    canvas.paste(fitted, ((width - cw) // 2, (PRINT_HEAD_DOTS - ch) // 2))
    return canvas


def compose_qr(
    data: str,
    *,
    error_correction: str = "M",
    version: int | None = None,
    text: str | None = None,
    layout: str = "side",
    font_path: str | None = None,
    font_size: int | None = None,
) -> Image.Image:
    """Compose a QR code (optionally with text) into a printable label image."""
    img = codes.qr_image(data, error_correction=error_correction, version=version)
    return compose_code_label(
        img, is_square=True, text=text, layout=layout, font_path=font_path, font_size=font_size
    )


def compose_barcode(
    data: str,
    *,
    symbology: str = codes.DEFAULT_BARCODE_SYMBOLOGY,
    text: str | None = None,
    layout: str = "side",
    font_path: str | None = None,
    font_size: int | None = None,
) -> Image.Image:
    """Compose a 1D barcode (optionally with text) into a printable label image."""
    img = codes.barcode_image(data, symbology=symbology)
    return compose_code_label(
        img, is_square=False, text=text, layout=layout, font_path=font_path, font_size=font_size
    )


def compose_aruco(
    marker_id: int,
    *,
    dictionary: str = codes.DEFAULT_ARUCO_DICT,
    text: str | None = None,
    layout: str = "side",
    font_path: str | None = None,
    font_size: int | None = None,
) -> Image.Image:
    """Compose an ArUco marker (optionally with text) into a printable label image."""
    img = codes.aruco_image(marker_id, dictionary=dictionary)
    return compose_code_label(
        img, is_square=True, text=text, layout=layout, font_path=font_path, font_size=font_size
    )


def qr_to_raster(data: str, **kwargs) -> tuple[bytes, int]:
    """Render a QR label straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_qr(data, **kwargs))


def barcode_to_raster(data: str, **kwargs) -> tuple[bytes, int]:
    """Render a barcode label straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_barcode(data, **kwargs))


def aruco_to_raster(marker_id: int, **kwargs) -> tuple[bytes, int]:
    """Render an ArUco label straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_aruco(marker_id, **kwargs))


# Public-API aliases matching the package's documented surface.
render_image = image_to_raster
render_text = text_to_raster
