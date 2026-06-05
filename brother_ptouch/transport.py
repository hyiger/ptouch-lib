"""Brother PT-P710BT transport -- print via the OS print system.

The PT-P710BT's Bluetooth is iOS/Android only per Brother; on a desktop it
enumerates as a USB **printer-class** device, reachable through the OS print
stack -- NOT a serial port, and NOT via libusb (the kernel print driver owns
the interface and libusb would fight it). This module hands the raster byte
stream to the platform print system:

  - macOS / Linux -> CUPS. ``lp -o raw`` to a print queue. When the target is a
    raw ``usb://...`` device with no installed queue, a hidden raw queue
    (``PTouch_Label``) is auto-managed and bound to it.
  - Windows -> the print spooler. The ``RAW`` datatype is sent to the printer
    via ``win32print`` (pywin32) if available, else a PowerShell P/Invoke
    fallback calling winspool ``WritePrinter``.

This is a port of ``electron/label-printer.ts``. It was hardware-validated on
macOS over USB; Windows and Linux paths are implemented but marked
needs-hardware-confirmation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

__all__ = ["PrinterDevice", "list_printers", "has_print_system", "print_raster", "PrintError"]

_log = logging.getLogger(__name__)

#: Heuristic match for "this printer/device is a PT-series label printer".
_PRINTER_PATTERN = re.compile(r"pt-?p710bt|p-?touch|brother", re.IGNORECASE)

#: Name of the hidden raw CUPS queue we manage for raw ``usb://`` devices not
#: already installed as a queue. CUPS names allow only letters/digits/_.
_MANAGED_QUEUE = "PTouch_Label"

#: Per-subprocess timeout (seconds).
_EXEC_TIMEOUT_S = 15

#: On Linux a GUI/non-login PATH often omits /usr/sbin where lpadmin/lpinfo
#: live, so we try the bare name first then the sbin path.
_SBIN = "/usr/sbin"


class PrintError(RuntimeError):
    """Raised when a print or device-listing operation fails."""


@dataclass
class PrinterDevice:
    """A printer/device the OS print system can reach."""

    #: Opaque print target: a CUPS queue name, a ``usb://...`` device URI, or a
    #: Windows printer name. Pass back to :func:`print_raster`.
    path: str
    #: Human-readable name for a picker.
    friendly_name: str
    #: True when the target matches a PT-series printer.
    looks_like_printer: bool


def _is_cups() -> bool:
    return sys.platform in ("darwin", "linux")


def has_print_system() -> bool:
    """True if a supported print system is available (CUPS or the Windows spooler).

    Lets callers tell "no print system" apart from "system reachable but no
    printers" instead of conflating both as an empty list. On macOS/Linux this
    actually probes for the CUPS client tools (a platform check alone would say
    True on a host that has no CUPS installed). A present-but-stopped cupsd still
    reports True -- that case surfaces as the "check the system is running" hint.
    """
    if sys.platform == "win32":
        return True
    if _is_cups():
        return any(
            shutil.which(t) or shutil.which(t, path=_SBIN) for t in ("lpstat", "lp")
        )
    return False


def _run_cups_tool(tool: str, args: list[str]) -> str:
    """Run a CUPS tool, falling back to /usr/sbin when it isn't on PATH."""
    try:
        return subprocess.run(
            [tool, *args],
            capture_output=True,
            text=True,
            timeout=_EXEC_TIMEOUT_S,
            check=True,
        ).stdout
    except FileNotFoundError:
        return subprocess.run(
            [os.path.join(_SBIN, tool), *args],
            capture_output=True,
            text=True,
            timeout=_EXEC_TIMEOUT_S,
            check=True,
        ).stdout


def _prettify_usb_uri(uri: str) -> str:
    """``usb://Brother/PT-P710BT?serial=000M...`` -> ``Brother PT-P710BT (USB)``."""
    try:
        from urllib.parse import unquote

        path = re.sub(r"^usb://", "", uri, flags=re.IGNORECASE).split("?")[0]
        parts = [unquote(p) for p in path.split("/") if p]
        return f"{' '.join(parts)} (USB)" if parts else uri
    except Exception:
        return uri


def list_printers() -> list[PrinterDevice]:
    """List printers/devices the OS print system can reach.

    Never raises -- returns ``[]`` on any failure so a caller's empty state
    just shows nothing.
    """
    try:
        if sys.platform == "win32":
            return _list_windows_printers()
        if _is_cups():
            return _list_cups_printers()
        return []
    except Exception as err:  # never raises -- but record why for diagnostics
        _log.warning("could not query the print system: %s: %s", type(err).__name__, err)
        return []


def _list_cups_printers() -> list[PrinterDevice]:
    devices: list[PrinterDevice] = []
    seen_uris: set[str] = set()

    # 1. Installed queues. `lpstat -v` lines look like:
    #    "device for NAME: usb://Brother/PT-P710BT?serial=...".
    try:
        stdout = _run_cups_tool("lpstat", ["-v"])
        for line in stdout.splitlines():
            m = re.match(r"^device for (.+?):\s*(.+)$", line)
            if not m:
                continue
            name, uri = m.group(1).strip(), m.group(2).strip()
            seen_uris.add(uri)
            if name == _MANAGED_QUEUE:
                # Our managed queue is an implementation detail -- surface it as
                # the underlying USB device so selecting + printing both route
                # through _ensure_managed_queue idempotently.
                devices.append(
                    PrinterDevice(uri, _prettify_usb_uri(uri), bool(_PRINTER_PATTERN.search(uri)))
                )
                continue
            devices.append(
                PrinterDevice(name, name, bool(_PRINTER_PATTERN.search(f"{name} {uri}")))
            )
    except Exception as err:
        _log.warning("lpstat -v failed: %s: %s", type(err).__name__, err)  # fall through

    # 2. Available USB devices not already installed as a queue.
    #    Scope to the usb scheme: a bare `lpinfo -v` also runs the snmp/dnssd
    #    network backends, which probe the LAN ~13s and blow the timeout
    #    (GH #590). We only parse usb:// lines anyway.
    try:
        stdout = _run_cups_tool("lpinfo", ["--include-schemes", "usb", "-v"])
        for line in stdout.splitlines():
            m = re.match(r"^\w+\s+(usb://\S+)$", line.strip())
            if not m:
                continue
            uri = m.group(1).strip()
            if uri in seen_uris:
                continue
            seen_uris.add(uri)
            devices.append(
                PrinterDevice(uri, _prettify_usb_uri(uri), bool(_PRINTER_PATTERN.search(uri)))
            )
    except Exception as err:  # lpinfo may need elevated privileges on some distros
        _log.warning("lpinfo failed: %s: %s", type(err).__name__, err)

    return devices


def _list_windows_printers() -> list[PrinterDevice]:
    # Prefer pywin32 when present; fall back to PowerShell Get-Printer.
    try:
        import win32print  # type: ignore

        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        names = [p[2] for p in win32print.EnumPrinters(flags, None, 1)]
        return [
            PrinterDevice(n, n, bool(_PRINTER_PATTERN.search(n))) for n in names if n
        ]
    except Exception:
        pass

    script = "@(Get-Printer | Select-Object Name) | ConvertTo-Json -Compress"
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=_EXEC_TIMEOUT_S,
    ).stdout.strip()
    if not out:
        return []
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return []
    rows = parsed if isinstance(parsed, list) else [parsed]
    devices: list[PrinterDevice] = []
    for row in rows:
        name = row.get("Name") if isinstance(row, dict) else None
        if isinstance(name, str) and name:
            devices.append(PrinterDevice(name, name, bool(_PRINTER_PATTERN.search(name))))
    return devices


def _is_legacy_serial_target(target: str) -> bool:
    """A leftover serial-port setting from the pre-USB Bluetooth transport:
    a macOS ``/dev/tty.*`` / ``/dev/cu.*`` path, a Linux ``/dev/rfcomm*``, or a
    Windows ``COMn``. None are valid OS print targets now."""
    return bool(re.match(r"^/dev/", target) or re.match(r"^COM\d+$", target, re.IGNORECASE))


def print_raster(target: str, data: bytes, timeout: float = _EXEC_TIMEOUT_S) -> None:
    """Send the raster byte stream to a print target.

    Args:
        target: A CUPS queue name, a ``usb://...`` device URI, or a Windows
            printer name (as returned by :func:`list_printers`).
        data: The Brother raster command byte stream.
        timeout: Per-subprocess timeout (seconds).

    Raises:
        PrintError: with a descriptive message (including printer stderr) on
            any failure.
    """
    if _is_legacy_serial_target(target):
        raise PrintError(
            f'"{target}" is a serial-port setting from an older Bluetooth transport. '
            "The PT-P710BT prints over USB -- run `ptouch list` and select a printer again."
        )
    if sys.platform == "win32":
        return _print_windows(target, data, timeout)
    if _is_cups():
        return _print_cups(target, data, timeout)
    raise PrintError(f'Label printing is not supported on platform "{sys.platform}".')


def _print_cups(target: str, data: bytes, timeout: float) -> None:
    # A target with a scheme ("usb://...") is a raw device -> route through our
    # managed raw queue. Otherwise it's an installed queue name.
    queue = target
    if re.match(r"^[a-z]+://", target, re.IGNORECASE):
        _ensure_managed_queue(target, timeout)
        queue = _MANAGED_QUEUE

    # `-o raw` sends the file unfiltered so the Brother raster stream reaches
    # the print head verbatim, regardless of any driver on the queue.
    try:
        proc = subprocess.run(
            ["lp", "-d", queue, "-o", "raw"],
            input=data,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as err:
        raise PrintError(
            f"lp timed out after {timeout}s -- power-cycle the printer and try again."
        ) from err
    except FileNotFoundError as err:
        raise PrintError("`lp` not found -- is CUPS installed?") from err
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise PrintError(f"lp exited with code {proc.returncode}{': ' + stderr if stderr else ''}")


def _ensure_managed_queue(uri: str, timeout: float) -> None:
    """Ensure the hidden raw queue exists and points at ``uri``. Idempotent."""
    current: str | None = None
    try:
        stdout = _run_cups_tool("lpstat", ["-v", _MANAGED_QUEUE])
        m = re.search(r"^device for .+?:\s*(.+)$", stdout, re.MULTILINE)
        current = m.group(1).strip() if m else None
    except Exception:
        current = None  # queue doesn't exist yet
    if current == uri:
        return

    # No `-m <model>` -> CUPS creates a *raw* queue (no PPD): bytes pass
    # straight through. lpadmin needs admin rights.
    args = [
        "-p", _MANAGED_QUEUE,
        "-v", uri,
        "-E",
        "-D", "Brother P-touch Label Printer",
        "-o", "printer-is-shared=false",
    ]
    try:
        _run_cups_tool("lpadmin", args)
    except Exception as err:
        raise PrintError(
            f"Could not set up the print queue for {_prettify_usb_uri(uri)}. "
            "You may need to add the printer in your system print settings first. "
            f"({err})"
        ) from err


def _print_windows(printer_name: str, data: bytes, timeout: float) -> None:
    # Prefer pywin32 -- a direct WritePrinter with the RAW datatype.
    try:
        import win32print  # type: ignore

        handle = win32print.OpenPrinter(printer_name)
        try:
            win32print.StartDocPrinter(handle, 1, ("Brother P-touch Label", None, "RAW"))
            try:
                win32print.StartPagePrinter(handle)
                win32print.WritePrinter(handle, data)
                win32print.EndPagePrinter(handle)
            finally:
                win32print.EndDocPrinter(handle)
        finally:
            win32print.ClosePrinter(handle)
        return
    except ImportError:
        pass  # fall back to the PowerShell P/Invoke path
    except Exception as err:
        raise PrintError(f"Windows raw print failed: {err}") from err

    _print_windows_powershell(printer_name, data, timeout)


def _print_windows_powershell(printer_name: str, data: bytes, timeout: float) -> None:
    dir_ = tempfile.mkdtemp(prefix="ptouch-label-")
    data_path = os.path.join(dir_, "label.bin")
    script_path = os.path.join(dir_, "print.ps1")
    try:
        with open(data_path, "wb") as fh:
            fh.write(data)
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(_windows_raw_print_script())
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", script_path,
                "-PrinterName", printer_name,
                "-FilePath", data_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise PrintError(
                f"Windows raw print failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )
    except subprocess.TimeoutExpired as err:
        raise PrintError(f"Windows raw print timed out after {timeout}s.") from err
    finally:
        import shutil

        shutil.rmtree(dir_, ignore_errors=True)


def _windows_raw_print_script() -> str:
    """Load the bundled PowerShell that sends bytes to a printer with the
    spooler RAW datatype (winspool.drv WritePrinter) -- the Windows equivalent
    of ``lp -o raw``."""
    from importlib.resources import files

    return files(__package__).joinpath("_win_raw_print.ps1").read_text(encoding="utf-8")
