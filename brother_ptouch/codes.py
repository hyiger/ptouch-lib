"""Code generators: QR, 1D barcode, and ArUco markers.

Each function returns a black-on-white Pillow ``"L"`` image at a "natural"
resolution (QR/ArUco: 1 px per module/bit; barcode: rendered at the printer's
180 dpi). The render layer (:mod:`brother_ptouch.render`) scales and composes
them onto the 128-dot tape, optionally beside or above a text string.

These rely on popular third-party libraries, installed as optional extras so
the core stays Pillow-only:

    pip install 'brother-ptouch[qr]'        # qrcode
    pip install 'brother-ptouch[barcode]'   # python-barcode
    pip install 'brother-ptouch[aruco]'     # opencv-contrib-python(-headless)
    pip install 'brother-ptouch[codes]'     # all three

The libraries are imported lazily so importing this module never requires them.
"""

from __future__ import annotations

from importlib import resources

from PIL import Image, ImageOps

__all__ = [
    "QR_ERROR_CORRECTIONS",
    "DEFAULT_BARCODE_SYMBOLOGY",
    "DEFAULT_ARUCO_DICT",
    "NOZZLE_MARKERS",
    "qr_image",
    "barcode_image",
    "aruco_image",
    "nozzle_image",
    "nozzle_band_image",
    "nozzle_text",
    "normalize_nozzle",
]

#: 180 dpi print head -> dots per mm (python-barcode wants mm + dpi).
_DPI = 180
_MM_PER_DOT = 25.4 / _DPI

QR_ERROR_CORRECTIONS = ("L", "M", "Q", "H")
DEFAULT_BARCODE_SYMBOLOGY = "code128"
DEFAULT_ARUCO_DICT = "4X4_50"


def _missing(pkg: str, extra: str) -> ImportError:
    return ImportError(
        f"{pkg} is required for this feature. "
        f"Install it with: pip install 'brother-ptouch[{extra}]'"
    )


def qr_image(
    data: str,
    *,
    error_correction: str = "M",
    version: int | None = None,
    border: int = 4,
) -> Image.Image:
    """Render a QR code to a black-on-white ``"L"`` image at 1 px per module.

    Args:
        data: The payload to encode.
        error_correction: One of ``"L"``, ``"M"``, ``"Q"``, ``"H"``.
        version: QR version 1-40, or ``None`` (default) to auto-pick the
            smallest version that fits the data -- "all versions that fit"
            are supported; the physical fit on 24mm tape is enforced later by
            the renderer.
        border: Quiet-zone width in modules (spec minimum is 4; keep it for
            scannability).

    Returns:
        A square Pillow ``Image`` (mode ``"L"``); ``img.info["qr_version"]``
        records the version used.
    """
    try:
        import qrcode
        from qrcode.constants import (
            ERROR_CORRECT_H,
            ERROR_CORRECT_L,
            ERROR_CORRECT_M,
            ERROR_CORRECT_Q,
        )
    except ImportError as err:  # pragma: no cover - exercised via monkeypatch
        raise _missing("qrcode", "qr") from err

    ec_map = {
        "L": ERROR_CORRECT_L,
        "M": ERROR_CORRECT_M,
        "Q": ERROR_CORRECT_Q,
        "H": ERROR_CORRECT_H,
    }
    key = (error_correction or "M").upper()
    if key not in ec_map:
        raise ValueError(f"error_correction must be one of L, M, Q, H (got {error_correction!r})")

    qr = qrcode.QRCode(
        version=version,
        error_correction=ec_map[key],
        box_size=1,
        border=border,
    )
    qr.add_data(data)
    try:
        qr.make(fit=version is None)
    except qrcode.exceptions.DataOverflowError as err:
        raise ValueError(
            f"data too long for QR version {version}: {err}. "
            "Use a higher --qr-version or omit it to auto-fit."
        ) from err

    matrix = qr.get_matrix()  # includes the quiet-zone border
    n = len(matrix)
    img = Image.new("L", (n, n), 255)
    img.putdata([0 if cell else 255 for row in matrix for cell in row])
    img.info["qr_version"] = qr.version
    return img


def barcode_image(
    data: str,
    *,
    symbology: str = DEFAULT_BARCODE_SYMBOLOGY,
    module_width_dots: int = 3,
    height_dots: int = 110,
    quiet_zone_dots: int = 12,
) -> Image.Image:
    """Render a 1D barcode to a black-on-white ``"L"`` image at 180 dpi.

    The human-readable text python-barcode normally draws is disabled -- pair
    the code with a text string via the renderer instead.

    Args:
        data: The barcode payload (must be valid for ``symbology``).
        symbology: A python-barcode class name, e.g. ``"code128"``,
            ``"code39"``, ``"ean13"``, ``"ean8"``, ``"upca"``, ``"isbn13"``.
        module_width_dots: Narrow-bar width in dots (>= 2 recommended).
        height_dots: Bar height in dots.
        quiet_zone_dots: Left/right quiet zone in dots.

    Returns:
        A Pillow ``Image`` (mode ``"L"``). Width is data-driven (the label
        length); height is ``height_dots`` plus python-barcode's small margins.
    """
    try:
        from barcode import get_barcode_class
        from barcode.writer import ImageWriter
    except ImportError as err:  # pragma: no cover - exercised via monkeypatch
        raise _missing("python-barcode", "barcode") from err

    try:
        cls = get_barcode_class(symbology)
    except Exception as err:
        raise ValueError(f"unknown barcode symbology {symbology!r}") from err

    try:
        bc = cls(data, writer=ImageWriter())
    except Exception as err:
        raise ValueError(f"invalid data for barcode {symbology!r}: {err}") from err

    options = {
        "module_width": module_width_dots * _MM_PER_DOT,
        "module_height": height_dots * _MM_PER_DOT,
        "quiet_zone": quiet_zone_dots * _MM_PER_DOT,
        "write_text": False,
        "dpi": _DPI,
        "background": "white",
        "foreground": "black",
    }
    return bc.render(writer_options=options).convert("L")


def aruco_image(
    marker_id: int,
    *,
    dictionary: str = DEFAULT_ARUCO_DICT,
    quiet_zone_bits: int = 2,
) -> Image.Image:
    """Render an ArUco marker to a black-on-white ``"L"`` image at 1 px/bit.

    A white quiet zone is baked in -- ArUco detectors need surrounding white
    to find the marker.

    Args:
        marker_id: The marker id (must be in range for the dictionary).
        dictionary: An OpenCV predefined dictionary, with or without the
            ``DICT_`` prefix, e.g. ``"4X4_50"``, ``"5X5_100"``, ``"6X6_250"``,
            ``"7X7_1000"``, ``"ARUCO_ORIGINAL"``, ``"APRILTAG_36h11"``.
        quiet_zone_bits: White border width in marker bits.

    Returns:
        A square Pillow ``Image`` (mode ``"L"``).
    """
    try:
        import cv2
        import numpy as np
    except ImportError as err:  # pragma: no cover - exercised via monkeypatch
        raise _missing("opencv-contrib-python", "aruco") from err

    name = (dictionary or DEFAULT_ARUCO_DICT).upper()
    const = name if name.startswith("DICT_") else f"DICT_{name}"
    dict_id = getattr(cv2.aruco, const, None)
    if dict_id is None:
        raise ValueError(f"unknown ArUco dictionary {dictionary!r}")
    d = cv2.aruco.getPredefinedDictionary(dict_id)

    side = d.markerSize + 2  # marker grid + 1-bit black border each side
    try:
        if hasattr(cv2.aruco, "generateImageMarker"):  # OpenCV >= 4.7
            arr = cv2.aruco.generateImageMarker(d, int(marker_id), side, borderBits=1)
        else:  # pragma: no cover - very old OpenCV
            arr = cv2.aruco.drawMarker(d, int(marker_id), side)  # type: ignore[attr-defined]
    except cv2.error as err:
        raise ValueError(
            f"invalid marker id {marker_id} for dictionary {dictionary!r}: {err}"
        ) from err

    img = Image.fromarray(np.asarray(arr)).convert("L")
    if quiet_zone_bits > 0:
        img = ImageOps.expand(img, border=quiet_zone_bits, fill=255)
    return img


# --------------------------------------------------------------------------- #
# Bambu Lab nozzle markers
# --------------------------------------------------------------------------- #
#
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
    :func:`brother_ptouch.render.compose_nozzle`.

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
    Scale it to a printable label with :func:`brother_ptouch.render.compose_nozzle`.

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
