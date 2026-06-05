"""Unit tests for transport.py — the non-hardware logic (issue #16).

Covers target validation, URI prettifying, the CUPS list parsers, the
`lp -o raw` dispatch / managed-queue idempotency, and the error branches —
all with subprocess / CUPS tools mocked, so nothing hits real hardware.
"""

import subprocess

import pytest

from brother_ptouch import transport


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- target validation --------------------------------------------------------

@pytest.mark.parametrize(
    "target,expected",
    [
        ("/dev/tty.Bluetooth-Incoming", True),
        ("/dev/cu.usbmodem1", True),
        ("/dev/rfcomm0", True),
        ("COM3", True),
        ("com10", True),
        ("usb://Brother/PT-P710BT?serial=000M", False),
        ("MyQueue", False),
        ("PTouch_Label", False),
    ],
)
def test_is_legacy_serial_target(target, expected):
    assert transport._is_legacy_serial_target(target) is expected


def test_print_raster_rejects_legacy_serial():
    with pytest.raises(transport.PrintError, match="serial-port"):
        transport.print_raster("/dev/tty.X", b"data")


def test_print_raster_unsupported_platform(monkeypatch):
    monkeypatch.setattr(transport, "_is_cups", lambda: False)
    monkeypatch.setattr(transport.sys, "platform", "sunos5")
    with pytest.raises(transport.PrintError, match="not supported"):
        transport.print_raster("queue", b"data")


# --- URI prettify -------------------------------------------------------------

def test_prettify_usb_uri():
    assert (
        transport._prettify_usb_uri("usb://Brother/PT-P710BT?serial=000M")
        == "Brother PT-P710BT (USB)"
    )
    assert transport._prettify_usb_uri("usb://") == "usb://"  # nothing after scheme


# --- CUPS list parser ---------------------------------------------------------

def test_list_cups_parses_lpstat_and_lpinfo(monkeypatch):
    def fake_tool(tool, args):
        if tool == "lpstat":
            return (
                "device for Brother_PT: usb://Brother/PT-P710BT?serial=ABC\n"
                "device for Office: ipp://host/printers/q\n"
            )
        if tool == "lpinfo":
            return "direct usb://Brother/PT-P710BT?serial=NEW\nnetwork dnssd://x\n"
        return ""

    monkeypatch.setattr(transport, "_run_cups_tool", fake_tool)
    by_path = {d.path: d for d in transport._list_cups_printers()}
    assert by_path["Brother_PT"].looks_like_printer is True       # matched by name+uri
    assert by_path["Office"].looks_like_printer is False
    assert "usb://Brother/PT-P710BT?serial=NEW" in by_path        # new device from lpinfo
    assert "usb://Brother/PT-P710BT?serial=ABC" not in by_path    # already an installed queue


# --- lp -o raw dispatch -------------------------------------------------------

def test_print_cups_builds_lp_raw_argv(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(
        transport.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or _FakeProc()
    )
    transport.print_raster("MyQueue", b"\x01\x02")
    cmd, kw = calls[-1]
    assert cmd == ["lp", "-d", "MyQueue", "-o", "raw"]
    assert kw.get("input") == b"\x01\x02"


def test_print_cups_raw_usb_routes_through_managed_queue(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "linux")
    monkeypatch.setattr(transport, "_ensure_managed_queue", lambda uri, t: None)
    calls = []
    monkeypatch.setattr(
        transport.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _FakeProc()
    )
    transport.print_raster("usb://Brother/PT-P710BT?serial=ABC", b"d")
    assert calls[-1] == ["lp", "-d", transport._MANAGED_QUEUE, "-o", "raw"]


def test_print_cups_raises_on_nonzero(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "linux")
    monkeypatch.setattr(
        transport.subprocess, "run", lambda cmd, **kw: _FakeProc(returncode=1, stderr=b"boom")
    )
    with pytest.raises(transport.PrintError, match="boom"):
        transport.print_raster("Q", b"d")


def test_print_cups_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "linux")

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(transport.subprocess, "run", boom)
    with pytest.raises(transport.PrintError, match="timed out"):
        transport.print_raster("Q", b"d")


def test_print_cups_raises_when_lp_missing(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "linux")

    def boom(cmd, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(transport.subprocess, "run", boom)
    with pytest.raises(transport.PrintError, match="not found"):
        transport.print_raster("Q", b"d")


# --- managed-queue idempotency ------------------------------------------------

def test_ensure_managed_queue_skips_rebind_when_bound(monkeypatch):
    uri = "usb://Brother/PT-P710BT?serial=ABC"
    calls = []

    def fake_tool(tool, args):
        calls.append(tool)
        if tool == "lpstat":
            return f"device for {transport._MANAGED_QUEUE}: {uri}\n"
        return ""

    monkeypatch.setattr(transport, "_run_cups_tool", fake_tool)
    transport._ensure_managed_queue(uri, 5)
    assert "lpadmin" not in calls  # already bound -> no re-bind


def test_ensure_managed_queue_binds_when_absent(monkeypatch):
    calls = []

    def fake_tool(tool, args):
        calls.append(tool)
        if tool == "lpstat":
            raise RuntimeError("no such queue")  # not present yet
        return ""

    monkeypatch.setattr(transport, "_run_cups_tool", fake_tool)
    transport._ensure_managed_queue("usb://x", 5)
    assert "lpadmin" in calls
