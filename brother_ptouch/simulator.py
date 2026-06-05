"""Brother PT-P710BT label-print simulator.

Companion to the encoder. Reads a Brother raster command byte stream, decodes
every command, validates structure, reconstructs the bitmap, and can write a
preview PNG -- so the wire format can be verified in CI with no hardware.

This is a port of ``print-label-sim.ts`` from the filament-db project.

WHAT GETS CHECKED
    - Invalidate prefix (>= 64 x 0x00)
    - Initialize: 0x1B 0x40
    - Raster mode: 0x1B 0x69 0x61 0x01
    - Print info: 0x1B 0x69 0x7A + 10 bytes
    - Mode bits / expansion / margin / compression
    - Raster lines: G + LE length + payload, total count matches print info
    - Terminator: 0x1A (print + cut) or 0x0C (print, no cut)
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "PRINT_HEAD_DOTS",
    "DecodeResult",
    "DecodeError",
    "decode",
    "to_preview_image",
    "decode_file_to_png",
]

PRINT_HEAD_DOTS = 128


class DecodeError(ValueError):
    """Raised when the byte stream is structurally invalid (a missing or
    malformed command), as opposed to a soft :attr:`DecodeResult.warnings`
    note about a recoverable oddity."""


@dataclass
class DecodeResult:
    """Structured result of decoding a Brother raster command stream."""

    tape_width_mm: int
    media_type: int
    raster_line_count: int  # as declared in the print-info header
    auto_cut: bool
    compression: int
    margin_dots: int
    #: One bytes object per decoded raster line, each ``PRINT_HEAD_DOTS`` long,
    #: expanded to 1 byte per dot (0 = black, 255 = white), in printer feed
    #: order (the order they appear in the stream).
    raster_lines: list[bytes] = field(default_factory=list)
    trailer: str = "unknown"  # "cut" | "no-cut" | "unknown"
    warnings: list[str] = field(default_factory=list)


class _StreamReader:
    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def remaining(self) -> int:
        return len(self.buf) - self.pos

    def _need(self, n: int) -> None:
        """Raise DecodeError (not IndexError) if fewer than ``n`` bytes remain."""
        if self.remaining() < n:
            raise DecodeError(
                f"unexpected end of stream at offset {self.pos}: "
                f"need {n} more byte(s), have {self.remaining()}"
            )

    def peek(self, n: int) -> bytes:
        return self.buf[self.pos : self.pos + n]

    def read(self, n: int) -> bytes:
        self._need(n)
        out = self.buf[self.pos : self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        self._need(1)
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def u16le(self) -> int:
        self._need(2)
        v = int.from_bytes(self.buf[self.pos : self.pos + 2], "little")
        self.pos += 2
        return v

    def u32le(self) -> int:
        self._need(4)
        v = int.from_bytes(self.buf[self.pos : self.pos + 4], "little")
        self.pos += 4
        return v

    def matches(self, seq: bytes) -> bool:
        return self.peek(len(seq)) == seq

    def expect(self, seq: bytes, label: str) -> None:
        if not self.matches(seq):
            got = self.peek(len(seq)).hex()
            want = seq.hex()
            raise DecodeError(
                f"expected {label} ({want}) at offset {self.pos}, got {got}"
            )
        self.pos += len(seq)


def decode(data: bytes, verbose: bool = False) -> DecodeResult:
    """Decode a Brother raster command byte stream.

    Args:
        data: The raw byte stream (as produced by
            :func:`brother_ptouch.encoder.encode_label`).
        verbose: When True, print a per-command trace to stdout.

    Returns:
        A :class:`DecodeResult`. Structural problems raise
        :class:`DecodeError`; recoverable oddities are collected in
        :attr:`DecodeResult.warnings`.
    """
    warnings: list[str] = []
    r = _StreamReader(data)

    def trace(msg: str) -> None:
        if verbose:
            print(f"  {msg}")

    # 1. Invalidate prefix -- count leading 0x00 bytes.
    zeros = 0
    while r.remaining() > 0 and r.buf[r.pos] == 0x00:
        zeros += 1
        r.pos += 1
    trace(f"invalidate prefix: {zeros} x 0x00")
    if zeros < 64:
        warnings.append(f"invalidate prefix is only {zeros} bytes, recommended >= 64")

    # 2. Initialize: ESC @
    r.expect(b"\x1b\x40", "ESC @ (initialize)")
    trace("initialize (ESC @)")

    # 3. Switch to raster mode: ESC i a 01
    r.expect(b"\x1b\x69\x61\x01", "ESC i a 01 (raster mode)")
    trace("switch to raster mode")

    # 4. Print info: ESC i z + 10 bytes
    r.expect(b"\x1b\x69\x7a", "ESC i z (print info)")
    flags = r.u8()
    media_type = r.u8()
    tape_width_mm = r.u8()
    tape_length_mm = r.u8()
    raster_line_count = r.u32le()
    starting_page = r.u8()
    reserved = r.u8()
    trace(
        f"print info: flags=0x{flags:02x} media=0x{media_type:02x} "
        f"width={tape_width_mm}mm length={tape_length_mm}mm "
        f"lines={raster_line_count} page={starting_page} reserved={reserved}"
    )
    if media_type != 0x01:
        warnings.append(
            f"media type is 0x{media_type:02x}, expected 0x01 (laminated)"
        )

    # 5. Mode bits: ESC i M
    r.expect(b"\x1b\x69\x4d", "ESC i M (mode)")
    mode_bits = r.u8()
    auto_cut = (mode_bits & 0x40) != 0
    trace(f"mode bits: 0x{mode_bits:02x} (auto-cut {'ON' if auto_cut else 'OFF'})")

    # 6. Expansion: ESC i K
    r.expect(b"\x1b\x69\x4b", "ESC i K (expansion)")
    expansion = r.u8()
    trace(f"expansion: 0x{expansion:02x}")

    # 7. Margin: ESC i d
    r.expect(b"\x1b\x69\x64", "ESC i d (margin)")
    margin = r.u16le()
    trace(f"margin: {margin} dots")

    # 8. Compression: M
    r.expect(b"\x4d", "M (compression mode)")
    compression = r.u8()
    label = "uncompressed" if compression == 0 else "packbits" if compression == 2 else "unknown"
    trace(f"compression: 0x{compression:02x} ({label})")
    if compression not in (0, 2):
        warnings.append(f"unrecognized compression mode 0x{compression:02x}")
    if compression == 2:
        warnings.append(
            "packbits decoding not implemented in this simulator -- bitmap will be wrong"
        )

    # 9. Raster lines: G + LE length + payload, repeated.
    raster_lines: list[bytes] = []
    while r.remaining() > 0 and r.buf[r.pos] == 0x47:
        r.pos += 1  # consume G
        length = r.u16le()
        if length > r.remaining():
            raise DecodeError(
                f"raster line {len(raster_lines)} claims {length} bytes "
                f"but only {r.remaining()} remain"
            )
        payload = r.read(length)
        # Expand 1-bit-per-dot MSB-first into 1-byte-per-dot grayscale.
        grayscale = bytearray(b"\xff" * PRINT_HEAD_DOTS)
        dots_this_line = min(PRINT_HEAD_DOTS, length * 8)
        for dot in range(dots_this_line):
            bit = (payload[dot >> 3] >> (7 - (dot & 7))) & 1
            grayscale[dot] = 0 if bit else 255
        raster_lines.append(bytes(grayscale))
    trace(f"decoded {len(raster_lines)} raster lines")
    if len(raster_lines) != raster_line_count:
        warnings.append(
            f"raster line count mismatch: header declared {raster_line_count}, "
            f"payload contained {len(raster_lines)}"
        )

    # 10. Terminator.
    trailer = "unknown"
    if r.remaining() > 0:
        term = r.u8()
        if term == 0x1A:
            trailer = "cut"
            trace("trailer: 0x1A (print + cut)")
        elif term == 0x0C:
            trailer = "no-cut"
            trace("trailer: 0x0C (print, no cut)")
        else:
            warnings.append(
                f"trailer byte 0x{term:02x} not recognized (expected 0x1A or 0x0C)"
            )
        if r.remaining() > 0:
            warnings.append(f"{r.remaining()} unexpected trailing bytes after terminator")
    else:
        warnings.append("no terminator byte -- printer would never fire the page")

    return DecodeResult(
        tape_width_mm=tape_width_mm,
        media_type=media_type,
        raster_line_count=raster_line_count,
        auto_cut=auto_cut,
        compression=compression,
        margin_dots=margin,
        raster_lines=raster_lines,
        trailer=trailer,
        warnings=warnings,
    )


def to_preview_image(decoded: DecodeResult):
    """Reconstruct the physical, human-readable label from a decode result.

    Each decoded raster line is one 128-dot-wide row in the printer's feed
    direction. The render pipeline rotated the label 90 degrees CW and then
    **reversed the raster-line order** (the #587 mirror fix -- the printer's
    physical feed direction is opposite the raster-line order). To model what
    the tape physically shows we undo both steps: reverse the line order back
    (``ImageOps.flip``), then rotate 90 degrees CCW. The result is the exact
    image that was composed, so an encode -> decode round trip is the identity
    on the rendered pixels.

    (The original TS simulator only rotated, so its preview came out mirrored
    relative to the physical label; modelling the feed-reversal here makes the
    preview match what actually prints.)

    Returns:
        A Pillow ``Image`` in mode ``"L"``.

    Raises:
        DecodeError: if no raster lines were decoded.
    """
    from PIL import Image, ImageOps

    if not decoded.raster_lines:
        raise DecodeError("no raster lines decoded -- refusing to build empty preview")
    height = len(decoded.raster_lines)
    raw = b"".join(decoded.raster_lines)
    stream_img = Image.frombytes("L", (PRINT_HEAD_DOTS, height), raw)
    return ImageOps.flip(stream_img).rotate(90, expand=True)


def decode_file_to_png(in_path: str, out_path: str | None = None, verbose: bool = False) -> DecodeResult:
    """Decode a ``.bin`` byte stream from disk and write a preview PNG.

    Args:
        in_path: Path to the raw byte stream.
        out_path: Where to write the preview PNG. Defaults to ``in_path`` with
            its ``.bin`` suffix replaced by ``-decoded.png``.
        verbose: Pass through to :func:`decode` for a command trace.

    Returns:
        The :class:`DecodeResult`.
    """
    with open(in_path, "rb") as fh:
        data = fh.read()
    decoded = decode(data, verbose=verbose)
    if out_path is None:
        out_path = (in_path[:-4] if in_path.endswith(".bin") else in_path) + "-decoded.png"
    to_preview_image(decoded).save(out_path)
    return decoded
