"""Bambu Lab nozzle marker bands — decode table, generators, and compose.

This is the **Bambu-specific** corner of the library, kept separate from the
generic code generators (:mod:`brother_ptouch.codes`) and the shared render
pipeline (:mod:`brother_ptouch.render`). It covers:

- :data:`NOZZLE_MARKERS` — the decoded 3x7 marker grids for all 13 nozzles
- :func:`normalize_nozzle` / :func:`nozzle_text` — name parsing + printed text
- :func:`nozzle_image` — generate the marker grid (Pillow only)
- :func:`nozzle_band_image` — load a bundled photo-derived band (exact)
- :func:`compose_nozzle` / :func:`nozzle_to_raster` — build a printable label

The H2D/H2C hot-end camera identifies the installed nozzle from a small marker
on the matte-black heat-sink face. Reproducing it lets you relabel a nozzle
(e.g. make a third-party Diamondback read as ``WC.4`` — hardware-confirmed).
"""

from __future__ import annotations

from importlib import resources

from PIL import Image, ImageOps

from .render import (
    PRINT_HEAD_DOTS,
    VERTICAL_PADDING_DOTS,
    LabelSize,
    _band_for,
    compose_code_label,
    compose_image,
    raster_from_composed,
)

__all__ = [
    "NOZZLE_MARKERS",
    "normalize_nozzle",
    "nozzle_text",
    "nozzle_image",
    "nozzle_band_image",
    "compose_nozzle",
    "nozzle_to_raster",
]

# The Bambu H2D/H2C hot-end camera identifies the installed nozzle from a small
# marker on the matte-black heat-sink face. Unlike Bambu's *build-plate* markers
# (standard OpenCV ArUco), the nozzle markers are a **custom Bambu code**: a
# 3-row x 7-column grid of square modules -- two 3x3 glyphs split by a blank
# column -- with a constant finder row 0 (`#...##.`) on every nozzle. `#` is a
# white module (the only part visible against the black heat-sink); `.` is black.
#
# Decoded 2026-06-04 from Bambu's catalog nozzle photos by grid registration
# (two independent fitters + visual QC), then confirmed against close-up macro
# shots of physical nozzles: WC0.4/0.6/0.8 matched Diamondback DB.4/.6/.8 nozzles
# bit-for-bit (they carry the identical glyph), and HFWC0.4/0.6/0.8 matched
# directly -- so all 13 are verified. (The WC glyph being shared by third-party
# hardened nozzles like Diamondback is what lets a WC label make the H2D accept a
# DB nozzle.)
NOZZLE_MARKERS: dict[str, tuple[str, ...]] = {
    "0.2":     ("#...##.", "###..##", "#.#..##"),
    "0.4":     ("#...##.", "#....#.", "..#.###"),
    "0.6":     ("#...##.", "#.#..#.", ".#...##"),
    "0.8":     ("#...##.", "#.#...#", "..#.###"),
    "HF0.4":   ("#...##.", ".....#.", "###.###"),
    "HF0.6":   ("#...##.", ".....#.", ".##..##"),
    "HF0.8":   ("#...##.", ".##...#", "###.###"),
    "WC0.4":   ("#...##.", ".##..##", "###.#.#"),
    "WC0.6":   ("#...##.", ".##..##", ".##...#"),
    "WC0.8":   ("#...##.", ".#...#.", "###.#.#"),
    "HFWC0.4": ("#...##.", ".##..##", ".##.###"),
    "HFWC0.6": ("#...##.", "..#...#", "###.#.#"),
    "HFWC0.8": ("#...##.", ".#...#.", ".##...#"),
}


def normalize_nozzle(nozzle: str) -> str:
    """Resolve a user-typed nozzle name to a canonical :data:`NOZZLE_MARKERS` key.

    Accepts forms like ``"WC0.4"``, ``"wc.4"``, ``"WC 0.4"``, ``"wc4"`` (-> the
    canonical ``"WC0.4"``) and ``"0.4"``, ``".4"``, ``"4"`` (-> ``"0.4"``).
    Materials: none (stainless), ``HF`` (high flow), ``WC`` (tungsten carbide),
    ``HFWC`` (high-flow tungsten carbide). Diameters: 0.2/0.4/0.6/0.8 (only 0.2
    for stainless).

    Raises:
        ValueError: if the name has no single 2/4/6/8 diameter digit or names a
            nozzle that does not exist (e.g. ``HF0.2``).
    """
    # Drop spaces / separators (".", "/", "_", ...) so "HF/WC.8", "wc 0.4",
    # "wc.4" and "wc4" all reduce to letters + the diameter digit.
    s = "".join(ch for ch in str(nozzle).upper() if ch.isalnum())
    material = ""
    for prefix in ("HFWC", "HF", "WC"):
        if s.startswith(prefix):
            material, s = prefix, s[len(prefix):]
            break
    digits = [c for c in s if c in "2468"]
    if len(digits) != 1:
        raise ValueError(
            f"can't read a nozzle diameter from {nozzle!r}; expected one of "
            f"0.2/0.4/0.6/0.8. Known nozzles: {', '.join(NOZZLE_MARKERS)}"
        )
    key = f"{material}0.{digits[0]}"
    if key not in NOZZLE_MARKERS:
        raise ValueError(
            f"unknown nozzle {nozzle!r} (resolved to {key!r}). "
            f"Known nozzles: {', '.join(NOZZLE_MARKERS)}"
        )
    return key


def nozzle_text(nozzle: str) -> str:
    """The human-readable text printed on that nozzle (e.g. ``WC0.6`` -> ``"WC.6"``).

    Matches the text on the physical nozzle so a replica label's text agrees with
    its marker (the camera check reportedly compares both). HF-WC nozzles print it
    as two lines (``HF`` over ``WC.x``), returned with an embedded newline.
    """
    key = normalize_nozzle(nozzle)
    material, _, diameter = key.partition("0.")
    if not material:
        return f"0.{diameter}"
    if material == "HFWC":
        return f"HF\nWC.{diameter}"
    return f"{material}.{diameter}"


def nozzle_image(nozzle: str, *, quiet_zone_modules: int = 1) -> Image.Image:
    """Render a Bambu nozzle marker to a black-on-white ``"L"`` image at 1 px/module.

    The grid is drawn "positive" (white module -> black pixel, like the other
    code generators), with a white quiet zone. The nozzle marker is physically
    white-on-black, so the renderer inverts it for printing -- see
    :func:`compose_nozzle`.

    Args:
        nozzle: A nozzle name; see :func:`normalize_nozzle` for accepted forms.
        quiet_zone_modules: White border width in modules.

    Returns:
        A Pillow ``Image`` (mode ``"L"``), 7+2q wide x 3+2q tall modules.
    """
    grid = NOZZLE_MARKERS[normalize_nozzle(nozzle)]
    rows, cols = len(grid), len(grid[0])
    img = Image.new("L", (cols, rows), 255)
    img.putdata([0 if ch == "#" else 255 for row in grid for ch in row])
    if quiet_zone_modules > 0:
        img = ImageOps.expand(img, border=quiet_zone_modules, fill=255)
    return img


def nozzle_band_image(nozzle: str) -> Image.Image:
    """Load the photo-derived band image for a nozzle (white-on-black ``"L"``).

    Unlike :func:`nozzle_image` (which *generates* just the marker from the grid
    table), this is the full ``[marker] | [text]`` band -- the exact Bambu marker,
    typeface, and spacing -- cleaned from real nozzle photos and bundled as
    package data under ``nozzle_bands/`` at the 16x5mm heat-sink-face proportions.
    Scale it to a printable label with :func:`compose_nozzle`.

    Args:
        nozzle: A nozzle name; see :func:`normalize_nozzle` for accepted forms.

    Returns:
        A Pillow ``Image`` (mode ``"L"``), white content on a black field.

    Raises:
        ValueError: if no band image is bundled for that nozzle.
    """
    key = normalize_nozzle(nozzle)
    ref = resources.files("brother_ptouch").joinpath("nozzle_bands").joinpath(f"{key}.png")
    try:
        with resources.as_file(ref) as path:
            return Image.open(path).convert("L")
    except (FileNotFoundError, OSError) as err:
        raise ValueError(
            f"no band image bundled for nozzle {key!r}; use the generated "
            "renderer instead (ptouch nozzle ... --generated)"
        ) from err


def compose_nozzle(
    nozzle: str,
    *,
    source: str = "photo",
    text: str | None = None,
    invert: bool = True,
    quiet_zone_modules: int = 0,
    layout: str = "side",
    separator: bool = True,
    font_path: str | None = None,
    font_size: int | None = None,
    size: LabelSize | None = None,
) -> Image.Image:
    """Compose a Bambu nozzle label, ready for :func:`raster_from_composed`.

    Two sources:

    - ``source="photo"`` (default) reproduces the **exact** band -- Bambu's real
      marker, typeface, and spacing -- from the bundled photo-derived band image
      (:func:`nozzle_band_image`). The band is the 16x5mm heat-sink face, so
      ``size=LabelSize.from_mm(16, 5)`` prints it at true size. The
      ``text``/``layout``/``separator``/font/``quiet_zone_modules`` args are
      ignored -- they are baked into the photo.
    - ``source="generated"`` builds the label from the decoded marker grid plus a
      system font; use it for marker-only labels, custom text, or nozzles with no
      bundled band. Here ``size``'s height is the marker grid height.

    The nozzle band is physically white-on-black, so by default the result is
    arranged for ordinary **black-on-white tape** (the printer lays down the black
    field; the marker/text stay white). Pass ``invert=False`` for **white-on-black
    tape** (the tape is the black background; only the marker/text print, in white).

    Args:
        nozzle: A nozzle name; see :func:`normalize_nozzle`.
        source: ``"photo"`` (exact, default) or ``"generated"``.
        text: (generated only) text beside the marker; ``None`` for none.
        invert: ``True`` (default) for black-on-white tape, ``False`` for black tape.
        quiet_zone_modules: (generated only) black border around the marker.
        layout, separator, font_path, font_size: (generated only) text layout/font.
        size: An explicit :class:`LabelSize`; ``None`` keeps the auto behaviour.

    Returns:
        A Pillow ``Image`` (mode ``"L"``), ready for :func:`raster_from_composed`.
    """
    if source not in ("photo", "generated"):
        raise ValueError(f"source must be 'photo' or 'generated', got {source!r}")

    if source == "photo":
        band = nozzle_band_image(nozzle)  # white content on a black field
        # The printer prints dark pixels. Black-on-white tape: feed the band as-is
        # (white-on-black) so the black field is inked and the content is bare tape.
        # White-on-black tape: feed the inverse so only the content prints (white).
        return compose_image(band if invert else ImageOps.invert(band), size=size)

    img = nozzle_image(nozzle, quiet_zone_modules=quiet_zone_modules)
    # Module size (dots) the marker will scale to, so gaps/divider track the real
    # band: ~1 module between marker, "|", and text; divider ~0.4 module wide.
    band_dots = _band_for(size, PRINT_HEAD_DOTS - 2 * VERTICAL_PADDING_DOTS)
    module = max(1, band_dots // img.height)
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


def nozzle_to_raster(nozzle: str, **kwargs) -> tuple[bytes, int]:
    """Render a Bambu nozzle-marker label straight to ``(bitmap, raster_lines)``."""
    return raster_from_composed(compose_nozzle(nozzle, **kwargs))
