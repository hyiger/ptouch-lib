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
from dataclasses import dataclass
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
    "DOTS_PER_MM",
    "LabelSize",
    "compose_image",
    "compose_text",
    "compose_code_label",
    "compose_qr",
    "compose_barcode",
    "compose_aruco",
    "compose_nozzle",
    "raster_from_composed",
    "image_to_raster",
    "text_to_raster",
    "qr_to_raster",
    "barcode_to_raster",
    "aruco_to_raster",
    "nozzle_to_raster",
    "render_image",
    "render_text",
]

#: 180 dpi print head -> dots per millimetre.
DOTS_PER_MM = 180 / 25.4

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
#: Minimum usable height (dots, ~3.4mm) for a code. When stacked text crowds
#: the code below this, we reject the label rather than emit an unscannably
#: tiny code (a 1-dot-high barcode, in particular). (Codex review, PR #1.)
MIN_CODE_DOTS = 24

#: Defensive cap on label length (raster lines), derived from the encoder's
#: 5 MB byte-stream cap. ~263k lines ~= 37 m of tape -- a runaway render.
MAX_RASTER_LINES = (MAX_LABEL_BYTES - 200) // (3 + BYTES_PER_RASTER_LINE)

ImageSource = Union[str, "Path", Image.Image]


@dataclass(frozen=True)
class LabelSize:
    """An explicit physical label size, instead of auto-filling the tape.

    ``band_dots`` is the content height **across** the tape (the content is
    scaled to it and centered in the 128-dot band; the rest is blank tape).
    ``length_dots`` is the exact label length **along** the feed (content is
    centered, padded with blank tape; an over-long content is rejected).
    Either may be ``None`` to keep the auto behaviour for that axis.
    """

    band_dots: int | None = None
    length_dots: int | None = None

    @classmethod
    def from_mm(cls, width_mm: float | None = None, height_mm: float | None = None) -> LabelSize:
        """Build from physical millimetres (width = length, height = across tape)."""
        band = round(height_mm * DOTS_PER_MM) if height_mm is not None else None
        length = round(width_mm * DOTS_PER_MM) if width_mm is not None else None
        if band is not None and not (1 <= band <= PRINT_HEAD_DOTS):
            raise ValueError(
                f"height {height_mm}mm = {band} dots is outside the printable tape "
                f"width (1..{PRINT_HEAD_DOTS} dots, ~{PRINT_HEAD_DOTS / DOTS_PER_MM:.1f}mm)"
            )
        if length is not None and length < 1:
            raise ValueError(f"width {width_mm}mm is too small to print")
        if length is not None and length > MAX_RASTER_LINES:
            raise ValueError(
                f"width {width_mm}mm = {length} dots exceeds the safety cap of "
                f"{MAX_RASTER_LINES} raster lines (~5 MB / ~{MAX_RASTER_LINES / DOTS_PER_MM:.0f}mm)"
            )
        return cls(band_dots=band, length_dots=length)


def _band_for(size: LabelSize | None, default: int) -> int:
    """The content band height to fit into, honoring an explicit size."""
    if size is not None and size.band_dots is not None:
        return size.band_dots
    return default


def _require_band_fit(height: int, band: int, size: LabelSize | None) -> None:
    """Reject content taller than an explicitly requested height band.

    The font auto-fit bottoms out at ``MIN_FONT_PX``, whose line height can
    still exceed a very small band -- so without this guard the "exact size"
    option could silently print content taller than requested. (Codex review,
    PR #3.) No-op when the height is auto (no explicit ``band_dots``).
    """
    if size is not None and size.band_dots is not None and height > band:
        raise ValueError(
            f"content does not fit the requested height of {band} dots "
            f"(~{band / DOTS_PER_MM:.1f}mm) even at the minimum font size -- "
            "increase the height or shorten the text"
        )


def _apply_length(canvas: Image.Image, size: LabelSize | None) -> Image.Image:
    """Center ``canvas`` in a label of exactly ``size.length_dots`` (or no-op)."""
    if size is None or size.length_dots is None:
        return canvas
    target = size.length_dots
    # Guard the allocation: a huge length would allocate a giant canvas here,
    # before raster_from_composed could enforce the cap. (Codex review, PR #3.)
    if target > MAX_RASTER_LINES:
        raise ValueError(
            f"requested label length {target} dots exceeds the safety cap of "
            f"{MAX_RASTER_LINES} raster lines (~5 MB)"
        )
    if canvas.width > target:
        raise ValueError(
            f"content is {canvas.width} dots (~{canvas.width / DOTS_PER_MM:.1f}mm) long, "
            f"exceeding the requested label length of {target} dots "
            f"(~{target / DOTS_PER_MM:.1f}mm). Widen the size or shorten the content "
            "(e.g. a smaller --font-size)."
        )
    out = Image.new("L", (target, PRINT_HEAD_DOTS), 255)
    out.paste(canvas, ((target - canvas.width) // 2, 0))
    return out

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
    size: LabelSize | None = None,
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
        size: An explicit :class:`LabelSize` to fit the text into instead of
            the full tape band; ``None`` keeps the auto behaviour.

    Returns:
        A Pillow ``Image`` (mode ``"L"``), ready for :func:`raster_from_composed`.
    """
    if orientation not in ("horizontal", "vertical"):
        raise ValueError(f"orientation must be 'horizontal' or 'vertical', got {orientation!r}")
    lines = _split_lines(text)
    band = _band_for(size, PRINT_HEAD_DOTS - 2 * VERTICAL_PADDING_DOTS)
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
    _require_band_fit(foot_h, band, size)

    width = foot_w + 2 * HORIZONTAL_PADDING_DOTS
    canvas = Image.new("L", (width, PRINT_HEAD_DOTS), 255)
    top = max(0, (PRINT_HEAD_DOTS - foot_h) // 2)
    canvas.paste(footprint, (HORIZONTAL_PADDING_DOTS, top))
    return _apply_length(canvas, size)


def compose_image(source: ImageSource, *, size: LabelSize | None = None) -> Image.Image:
    """Compose an image label into a ``length x 128`` ``"L"`` image.

    The image is converted to grayscale and scaled so its height fills the
    128-dot tape width (the cross-axis), preserving aspect ratio; the width
    becomes the label length. For a typical landscape image the long edge thus
    becomes the label length.

    Args:
        source: A file path or an already-open Pillow ``Image``.
        size: An explicit :class:`LabelSize`; its ``band_dots`` scales the image
            height to a sub-band (centered on the tape) and ``length_dots`` sets
            the exact label length. ``None`` keeps the auto behaviour.

    Returns:
        A Pillow ``Image`` (mode ``"L"``), ready for :func:`raster_from_composed`.
    """
    img = source if isinstance(source, Image.Image) else Image.open(source)
    img = img.convert("L")
    w, h = img.size
    if h == 0 or w == 0:
        raise ValueError("source image has a zero dimension")
    band = _band_for(size, PRINT_HEAD_DOTS)
    new_w = max(1, round(w * band / h))
    if (new_w, band) != (w, h):
        img = img.resize((new_w, band), Image.LANCZOS)
    if band != PRINT_HEAD_DOTS:
        canvas = Image.new("L", (new_w, PRINT_HEAD_DOTS), 255)
        canvas.paste(img, (0, (PRINT_HEAD_DOTS - band) // 2))
        img = canvas
    return _apply_length(img, size)


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


def image_to_raster(source: ImageSource, *, size: LabelSize | None = None) -> tuple[bytes, int]:
    """Render an image file/object straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_image(source, size=size))


def text_to_raster(
    text: str,
    *,
    font_path: str | None = None,
    font_size: int | None = None,
    orientation: str = "horizontal",
    size: LabelSize | None = None,
) -> tuple[bytes, int]:
    """Render text straight to ``(bitmap, raster_lines)``."""
    composed = compose_text(
        text, font_path=font_path, font_size=font_size, orientation=orientation, size=size
    )
    return raster_from_composed(composed)


def _text_block(text, font_path, font_size, max_h, default_size):
    """Render a horizontal multi-line text block whose height fits ``max_h``."""
    lines = _split_lines(text)
    base = font_size if font_size else default_size
    font = _fit_horizontal(lines, font_path, base, max_h)
    return _render_text_block(font, lines)


def _fit_code(code: Image.Image, is_square: bool, max_h: int, min_dots: int = MIN_CODE_DOTS) -> Image.Image:
    """Scale a code image to fit ``max_h`` dots tall.

    Square codes (QR/ArUco) scale by the largest *integer* factor so modules
    stay crisp; barcodes (whose height carries no data) are scaled to exactly
    ``max_h`` with their bar widths -- the data -- preserved.

    Raises ``ValueError`` when ``max_h`` is below ``min_dots`` (default
    :data:`MIN_CODE_DOTS`) -- e.g. when stacked text crowds the code out --
    rather than emitting an unscannably tiny code. Previously the barcode path
    clamped to ``max(1, max_h)`` and silently produced a 1-dot-high barcode
    (Codex review, PR #1). Nozzle markers pass a smaller ``min_dots`` because
    they are reproduced at the nozzle's real (sub-3mm) physical size.
    """
    w, h = code.size
    if max_h < min_dots:
        raise ValueError(
            f"only {max_h} dots remain for the code -- the accompanying text leaves "
            "too little room. Use fewer / shorter text lines, a smaller font size, "
            "or --layout side."
        )
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
        code = code.resize((w, max_h), Image.NEAREST)
    return code


def compose_code_label(
    code: Image.Image,
    *,
    is_square: bool,
    text: str | None = None,
    layout: str = "side",
    font_path: str | None = None,
    font_size: int | None = None,
    size: LabelSize | None = None,
    pad: int | None = None,
    min_code_dots: int | None = None,
    separator: bool = False,
    gap: int | None = None,
    sep_w: int | None = None,
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
        size: An explicit :class:`LabelSize` to fit into instead of the full
            tape band; ``None`` keeps the auto behaviour.
        pad: End padding (dots) at each end of the length; ``None`` uses the
            default ~2mm. Pass ``0`` for codes that supply their own quiet zone
            and set an exact length via ``size`` (e.g. nozzle markers).
        min_code_dots: Minimum code height (dots) before raising; ``None`` uses
            :data:`MIN_CODE_DOTS`. Nozzle markers pass a small value to allow
            reproduction at the nozzle's real (sub-3mm) size.
        separator: For the ``"side"`` layout with text, draw a vertical bar
            between the code and the text (matches the ``|`` divider on Bambu
            nozzle bands). Ignored without text or for ``"stack"``.
        gap: Spacing (dots) between code, separator, and text in the ``"side"``
            layout; ``None`` uses :data:`GAP_DOTS` (~1.7mm). Nozzle markers pass
            ~1 module to match the tight real spacing.
        sep_w: Divider bar width (dots); ``None`` auto-sizes from the band.

    Returns:
        A Pillow ``Image`` (mode ``"L"``), ready for :func:`raster_from_composed`.
    """
    if layout not in ("side", "stack"):
        raise ValueError(f"layout must be 'side' or 'stack', got {layout!r}")
    pad = HORIZONTAL_PADDING_DOTS if pad is None else pad
    mcd = MIN_CODE_DOTS if min_code_dots is None else min_code_dots
    g = GAP_DOTS if gap is None else gap
    text = text if (text and text.strip()) else None
    band = _band_for(size, PRINT_HEAD_DOTS - 2 * VERTICAL_PADDING_DOTS)

    if layout == "side":
        fitted = _fit_code(code, is_square, band, mcd)
        cw, ch = fitted.size
        block = _text_block(text, font_path, font_size, band, DEFAULT_FONT_SIZE) if text else None
        if block:
            _require_band_fit(block.height, band, size)
        # Optional vertical divider bar (the nozzle band's "|"), centered in the
        # gap between the code and the text, as tall as the code.
        sw = (sep_w if sep_w is not None else max(2, round(band * 0.05))) if (separator and block) else 0
        sep_gap = g if sw else 0
        text_w = (g + sw + sep_gap + block.width) if block else 0
        width = 2 * pad + cw + text_w
        canvas = Image.new("L", (width, PRINT_HEAD_DOTS), 255)
        canvas.paste(fitted, (pad, (PRINT_HEAD_DOTS - ch) // 2))
        if block:
            x = pad + cw + g
            if sw:
                bar = Image.new("L", (sw, ch), 0)
                canvas.paste(bar, (x, (PRINT_HEAD_DOTS - ch) // 2))
                x += sw + sep_gap
            canvas.paste(block, (x, (PRINT_HEAD_DOTS - block.height) // 2))
        return _apply_length(canvas, size)

    # stack: code on top, text below, sharing the band.
    if text:
        block = _text_block(text, font_path, font_size, round(band * STACK_TEXT_FRACTION), STACK_FONT_SIZE)
        fitted = _fit_code(code, is_square, band - block.height - GAP_DOTS, mcd)
        cw, ch = fitted.size
        content_h = ch + GAP_DOTS + block.height
        width = max(cw, block.width) + 2 * pad
        canvas = Image.new("L", (width, PRINT_HEAD_DOTS), 255)
        top = (PRINT_HEAD_DOTS - content_h) // 2
        canvas.paste(fitted, ((width - cw) // 2, top))
        canvas.paste(block, ((width - block.width) // 2, top + ch + GAP_DOTS))
        return _apply_length(canvas, size)

    fitted = _fit_code(code, is_square, band, mcd)
    cw, ch = fitted.size
    width = cw + 2 * pad
    canvas = Image.new("L", (width, PRINT_HEAD_DOTS), 255)
    canvas.paste(fitted, ((width - cw) // 2, (PRINT_HEAD_DOTS - ch) // 2))
    return _apply_length(canvas, size)


def compose_qr(
    data: str,
    *,
    error_correction: str = "M",
    version: int | None = None,
    text: str | None = None,
    layout: str = "side",
    font_path: str | None = None,
    font_size: int | None = None,
    size: LabelSize | None = None,
) -> Image.Image:
    """Compose a QR code (optionally with text) into a printable label image."""
    img = codes.qr_image(data, error_correction=error_correction, version=version)
    return compose_code_label(
        img, is_square=True, text=text, layout=layout,
        font_path=font_path, font_size=font_size, size=size,
    )


def compose_barcode(
    data: str,
    *,
    symbology: str = codes.DEFAULT_BARCODE_SYMBOLOGY,
    text: str | None = None,
    layout: str = "side",
    font_path: str | None = None,
    font_size: int | None = None,
    size: LabelSize | None = None,
) -> Image.Image:
    """Compose a 1D barcode (optionally with text) into a printable label image."""
    img = codes.barcode_image(data, symbology=symbology)
    return compose_code_label(
        img, is_square=False, text=text, layout=layout,
        font_path=font_path, font_size=font_size, size=size,
    )


def compose_aruco(
    marker_id: int,
    *,
    dictionary: str = codes.DEFAULT_ARUCO_DICT,
    text: str | None = None,
    layout: str = "side",
    font_path: str | None = None,
    font_size: int | None = None,
    size: LabelSize | None = None,
) -> Image.Image:
    """Compose an ArUco marker (optionally with text) into a printable label image."""
    img = codes.aruco_image(marker_id, dictionary=dictionary)
    return compose_code_label(
        img, is_square=True, text=text, layout=layout,
        font_path=font_path, font_size=font_size, size=size,
    )


def compose_nozzle(
    nozzle: str,
    *,
    text: str | None = None,
    invert: bool = True,
    quiet_zone_modules: int = 0,
    layout: str = "side",
    separator: bool = True,
    font_path: str | None = None,
    font_size: int | None = None,
    size: LabelSize | None = None,
) -> Image.Image:
    """Compose a Bambu nozzle marker (optionally with text) into a printable label.

    The nozzle marker is physically white-on-black (white modules on the black
    heat-sink), so by default the whole composed label is **inverted** -- a white
    marker (and white text) on a solid black field -- to match the nozzle when
    printed on ordinary black-on-white tape. Pass ``invert=False`` for
    white-on-black tape (already black where the tape shows through).

    Because the marker is only a few millimetres on the nozzle, you almost always
    want an explicit ``size`` (e.g. ``LabelSize.from_mm(...)``); the auto fill
    would scale it to the full tape width.

    Args:
        nozzle: A nozzle name; see :func:`brother_ptouch.codes.normalize_nozzle`.
        text: Optional text printed alongside the marker (e.g. ``"WC.4"``).
        invert: Invert the composed label to white-on-black (default ``True``).
        quiet_zone_modules: Black border around the marker, in marker modules
            (default 0 -- the inverted black field / black tape is the surround).
        layout: ``"side"`` (text beside the marker) or ``"stack"`` (text below).
        separator: Draw the ``|`` divider between marker and text (the real
            nozzle band has one); only applies to the ``"side"`` layout with text.
        font_path, font_size: Font for the accompanying text.
        size: An explicit :class:`LabelSize`; ``None`` keeps the auto behaviour.

    Returns:
        A Pillow ``Image`` (mode ``"L"``), ready for :func:`raster_from_composed`.
    """
    img = codes.nozzle_image(nozzle, quiet_zone_modules=quiet_zone_modules)
    # Module size (dots) the marker will scale to, so gaps/divider track the real
    # band: ~1 module between marker, "|", and text; divider ~0.4 module wide.
    band = _band_for(size, PRINT_HEAD_DOTS - 2 * VERTICAL_PADDING_DOTS)
    module = max(1, band // img.height)
    composed = compose_code_label(
        img, is_square=True, text=text, layout=layout, separator=separator,
        font_path=font_path, font_size=font_size, size=size,
        # The marker carries its own (module-scaled) quiet zone and a sized
        # nozzle label sets its exact length, so skip the ~2mm end padding that
        # would otherwise fight a small physical size.
        pad=0,
        # The nozzle marker is reproduced at the nozzle's real size (~2.2mm /
        # ~16 dots tall), well under the QR/barcode MIN_CODE_DOTS floor; the
        # marker height itself is the only real lower bound.
        min_code_dots=img.height,
        # Tight, real-band spacing instead of the default ~2mm code gap.
        gap=module,
        sep_w=max(1, round(0.4 * module)),
    )
    if invert:
        composed = ImageOps.invert(composed if composed.mode == "L" else composed.convert("L"))
    return composed


def qr_to_raster(data: str, **kwargs) -> tuple[bytes, int]:
    """Render a QR label straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_qr(data, **kwargs))


def barcode_to_raster(data: str, **kwargs) -> tuple[bytes, int]:
    """Render a barcode label straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_barcode(data, **kwargs))


def aruco_to_raster(marker_id: int, **kwargs) -> tuple[bytes, int]:
    """Render an ArUco label straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_aruco(marker_id, **kwargs))


def nozzle_to_raster(nozzle: str, **kwargs) -> tuple[bytes, int]:
    """Render a Bambu nozzle-marker label straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_nozzle(nozzle, **kwargs))


# Public-API aliases matching the package's documented surface.
render_image = image_to_raster
render_text = text_to_raster
