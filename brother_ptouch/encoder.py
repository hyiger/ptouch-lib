"""Brother PT-P710BT raster command encoder.

Pure-Python, **zero dependencies**, byte-exact. This is a faithful port of
the hardware-validated TypeScript implementation (``labelEncoder.ts`` in the
filament-db project). The byte sequence it emits has been confirmed on a real
PT-P710BT over USB; do not "improve" the wire format without re-validating on
hardware.

The encoder takes a row-major **1-bit-per-dot** bitmap (one bit per print
dot, MSB-first across each row, exactly 16 bytes / 128 dots per raster line)
and wraps it in the byte stream the printer expects. The caller is
responsible for rendering the bitmap (see :mod:`brother_ptouch.render`); this
module only handles serialization.

PROTOCOL REFERENCE
    Brother PT-E550W/P750W/P710BT Software Developer's Manual,
    "Raster Command Reference" (cv_pte550wp750wp710bt_eng_raster_102.pdf).

BITMAP CONTRACT
    - ``bitmap`` is row-major: row 0 is the first raster line printed.
    - Each row is exactly ``BYTES_PER_RASTER_LINE`` (= 16) bytes.
    - Bit 7 of byte 0 is the leftmost dot of the print head; bit 0 of byte 15
      is the rightmost dot. Black = 1, white = 0.
    - ``len(bitmap)`` must be ``raster_lines * BYTES_PER_RASTER_LINE``.

Callers rendering from a grayscale buffer should use :func:`pack_grayscale_row`
/ :func:`pack_grayscale_bitmap`, which handle the threshold + bit packing.
"""

from __future__ import annotations

import math

__all__ = [
    "PRINT_HEAD_DOTS",
    "BYTES_PER_RASTER_LINE",
    "VALID_TAPE_WIDTHS_MM",
    "MAX_LABEL_BYTES",
    "encode_label",
    "pack_grayscale_row",
    "pack_grayscale_bitmap",
]

#: Brother PT-series print head: 128 dots x 180 dpi. The same for every TZe
#: tape width; narrower tapes simply mask off dots at the edges.
PRINT_HEAD_DOTS = 128

#: 128 dots / 8 bits/byte = 16 bytes per raster line (uncompressed).
BYTES_PER_RASTER_LINE = PRINT_HEAD_DOTS // 8

#: Tape widths the printer accepts (mm). Only 24mm is exercised end-to-end,
#: but the encoder accepts all six standard widths.
VALID_TAPE_WIDTHS_MM = (3.5, 6, 9, 12, 18, 24)

#: Defensive cap on the encoded byte stream (~5 MB). A maxed ~200mm label is
#: only ~270 KB, so anything past this is a runaway render, not a real label.
MAX_LABEL_BYTES = 5 * 1024 * 1024


def _round_half_up(value: float) -> int:
    """Round half away from zero, matching JavaScript's ``Math.round``.

    Python's built-in ``round`` uses banker's rounding (round-half-to-even),
    which would encode some fractional widths differently than the reference
    TS implementation. Only 3.5mm tape is fractional in practice (-> 4), but
    we match ``Math.round`` exactly so other widths can't surprise us.
    """
    return int(math.floor(value + 0.5))


def encode_label(
    bitmap: bytes,
    raster_lines: int,
    tape_width_mm: float = 24,
    auto_cut: bool = True,
    margin_dots: int = 14,
) -> bytes:
    """Encode a label's bitmap into the Brother raster byte stream.

    The byte sequence, per Brother Raster Command Reference section 2:

      1. Invalidate (100 x 0x00) -- clears any half-finished command in the
         printer's input buffer.
      2. Initialize:     0x1B 0x40            (ESC @)
      3. Raster mode:    0x1B 0x69 0x61 0x01  (ESC i a 01)
      4. Print info:     0x1B 0x69 0x7A + 10-byte payload
                         (flags, media type, width-mm, length-mm,
                          raster-line count LE u32, page, reserved)
      5. Mode bits:      0x1B 0x69 0x4D <bits> -- 0x40 = auto-cut
      6. Expansion:      0x1B 0x69 0x4B <bits> -- bit 3 (0x08) = no-chain
      7. Margin:         0x1B 0x69 0x64 <dots LE u16>
      8. Compression:    0x4D 0x00 -- uncompressed
      9. Per raster line: 0x47 0x10 0x00 + 16 data bytes
     10. Trailer:        0x1A (print + cut) or 0x0C (print, no cut).

    Args:
        bitmap: Row-major packed 1-bit bitmap. See BITMAP CONTRACT in the
            module docstring.
        raster_lines: Number of raster lines in the bitmap.
            ``len(bitmap)`` must equal ``raster_lines * BYTES_PER_RASTER_LINE``.
        tape_width_mm: Tape width in mm. Currently only 24mm is exercised
            end-to-end, but all six standard widths encode correctly.
        auto_cut: When True (default), emit mode bit 0x40 + expansion bit 0x08
            + terminator 0x1A so the printer feeds and fires the cutter at end
            of job. Set False for chain printing (caller issues a cut later).
        margin_dots: Leading feed before the print, in dots. Brother's
            documented minimum is 14 dots (~2mm at 180 dpi). Default 14.

    Returns:
        The raw Brother raster command byte stream as ``bytes``.

    Raises:
        ValueError: on an unsupported ``tape_width_mm``, bad ``raster_lines``, a
            bitmap-length mismatch, a ``margin_dots`` out of u16 range, or an
            output exceeding :data:`MAX_LABEL_BYTES`.
    """
    if tape_width_mm not in VALID_TAPE_WIDTHS_MM:
        raise ValueError(
            f"tape_width_mm {tape_width_mm} is not a supported TZe width; "
            f"expected one of {VALID_TAPE_WIDTHS_MM}"
        )
    if raster_lines < 1:
        raise ValueError("raster_lines must be >= 1")
    if len(bitmap) != raster_lines * BYTES_PER_RASTER_LINE:
        raise ValueError(
            f"bitmap length {len(bitmap)} does not match "
            f"raster_lines ({raster_lines}) x BYTES_PER_RASTER_LINE "
            f"({BYTES_PER_RASTER_LINE}) = {raster_lines * BYTES_PER_RASTER_LINE}"
        )
    if margin_dots < 0 or margin_dots > 0xFFFF:
        raise ValueError(f"margin_dots {margin_dots} out of range [0, 65535]")

    total_len = (
        100  # invalidate
        + 2  # ESC @
        + 4  # ESC i a 01
        + 3 + 10  # ESC i z + 10 bytes
        + 4  # ESC i M
        + 4  # ESC i K
        + 5  # ESC i d
        + 2  # M 00
        + raster_lines * (3 + BYTES_PER_RASTER_LINE)  # raster lines
        + 1  # trailer
    )
    if total_len > MAX_LABEL_BYTES:
        raise ValueError(
            f"encoded stream would be {total_len} bytes, exceeding the "
            f"{MAX_LABEL_BYTES}-byte safety cap ({raster_lines} raster lines)"
        )

    out = bytearray()

    # 1. Invalidate -- 100 bytes (well over Brother's documented 64-byte
    # minimum). Clears any half-finished command in the input buffer.
    out += b"\x00" * 100

    # 2. Initialize.
    out += b"\x1b\x40"

    # 3. Switch to raster mode.
    out += b"\x1b\x69\x61\x01"

    # 4. Print info.
    #    flags=0x84 marks media-type + media-width + raster-line-count as
    #    valid (the printer ignores the corresponding bytes when their flag
    #    bit is unset). media=0x01 = laminated tape (the only kind PT-P710BT
    #    cartridges come in). length=0 = "auto from line count".
    out += b"\x1b\x69\x7a"
    out += b"\x84"  # flags
    out += b"\x01"  # media type: laminated tape
    # Media-width byte is rounded -- 3.5mm tape is encoded as 4 (Brother's
    # wire convention); every other width is an integer and round-trips
    # verbatim. See trap 4.3.
    out.append(_round_half_up(tape_width_mm) & 0xFF)
    out += b"\x00"  # media length (0 = auto from line count)
    # raster line count, little-endian u32
    out += int(raster_lines).to_bytes(4, "little")
    out += b"\x00"  # starting page
    out += b"\x00"  # reserved

    # 5. Mode bits -- 0x40 enables auto-cut at end of job.
    out += b"\x1b\x69\x4d"
    out.append(0x40 if auto_cut else 0x00)

    # 6. Various-mode flags (ESC i K). Bit 3 (0x08) controls chain printing:
    #      bit 3 = 1 -> no chain  (feed + cut after the LAST label)
    #      bit 3 = 0 -> chain     (printer holds the label, expecting another)
    #    Tie it to auto_cut: a one-shot print wants no-chain (0x08) so the
    #    label feeds out and gets cut; a deliberate chain print
    #    (auto_cut=False) wants chain mode (0x00). Trap 4.4: with 0x00 the
    #    auto-cut bit above still issues a cut command but the tape isn't fed
    #    through the cutter, so the label stays stuck in the head.
    out += b"\x1b\x69\x4b"
    out.append(0x08 if auto_cut else 0x00)

    # 7. Margin (leading feed before the printed area), in dots, LE u16.
    out += b"\x1b\x69\x64"
    out += int(margin_dots).to_bytes(2, "little")

    # 8. Compression: 0x00 = uncompressed. (0x02 = packbits is supported by
    # the printer but we deliberately don't use it -- trap, brief section 4.1.)
    out += b"\x4d\x00"

    # 9. Raster lines. Each one is G + length-LE-u16 (= 16) + 16 data bytes.
    for i in range(raster_lines):
        out += b"\x47"
        out += BYTES_PER_RASTER_LINE.to_bytes(2, "little")  # 0x10 0x00
        src = i * BYTES_PER_RASTER_LINE
        out += bitmap[src : src + BYTES_PER_RASTER_LINE]

    # 10. Trailer -- 0x1A = print with feed (and cut, if the auto-cut bit was
    # set above); 0x0C = print without feed (for chain printing).
    out.append(0x1A if auto_cut else 0x0C)

    if len(out) != total_len:
        # Should never happen; defensive check in case the header math drifts.
        raise RuntimeError(
            f"internal error: wrote {len(out)} bytes but expected {total_len}"
        )
    return bytes(out)


def pack_grayscale_row(gray_row: bytes) -> bytes:
    """Pack one row of grayscale (1 byte/dot, 0 = black, 255 = white) into
    16 bytes of MSB-first 1-bit packed data.

    Black dots (gray < 128) become 1 bits; the bit position is MSB-of-byte-0
    = the leftmost dot of the print head.

    Args:
        gray_row: A ``PRINT_HEAD_DOTS``-length bytes-like grayscale row.

    Returns:
        ``BYTES_PER_RASTER_LINE`` packed bytes.

    Raises:
        ValueError: if ``gray_row`` is not exactly ``PRINT_HEAD_DOTS`` long.
    """
    if len(gray_row) != PRINT_HEAD_DOTS:
        raise ValueError(
            f"pack_grayscale_row: row length {len(gray_row)} != {PRINT_HEAD_DOTS}"
        )
    out = bytearray(BYTES_PER_RASTER_LINE)
    for dot in range(PRINT_HEAD_DOTS):
        if gray_row[dot] < 128:
            out[dot >> 3] |= 1 << (7 - (dot & 7))
    return bytes(out)


def pack_grayscale_bitmap(grayscale: bytes, raster_lines: int) -> bytes:
    """Pack a row-major grayscale buffer (1 byte/dot, ``raster_lines`` rows x
    ``PRINT_HEAD_DOTS`` cols) into the row-major 1-bit bitmap the encoder wants.

    Args:
        grayscale: Row-major grayscale, 1 byte per dot (0 = black, 255 = white),
            length ``raster_lines * PRINT_HEAD_DOTS``.
        raster_lines: Number of raster lines (rows).

    Returns:
        ``raster_lines * BYTES_PER_RASTER_LINE`` packed bytes.

    Raises:
        ValueError: on a buffer-length / ``raster_lines`` mismatch.
    """
    if len(grayscale) != raster_lines * PRINT_HEAD_DOTS:
        raise ValueError(
            f"pack_grayscale_bitmap: buffer length {len(grayscale)} != "
            f"raster_lines ({raster_lines}) x {PRINT_HEAD_DOTS}"
        )
    packed = bytearray(raster_lines * BYTES_PER_RASTER_LINE)
    for row in range(raster_lines):
        src = row * PRINT_HEAD_DOTS
        dst = row * BYTES_PER_RASTER_LINE
        for dot in range(PRINT_HEAD_DOTS):
            if grayscale[src + dot] < 128:
                packed[dst + (dot >> 3)] |= 1 << (7 - (dot & 7))
    return bytes(packed)
