"""CLI/transport hardening tests (issues #13-#15)."""

import tempfile

from brother_ptouch.cli import main

# --- #13: unique, per-user temp fallback (not predictable /tmp/label.bin) -----

def test_temp_fallback_is_unique_not_predictable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray ./ptouch.toml
    monkeypatch.delenv("PTOUCH_CONFIG", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert main(["text", "--text", "A"]) == 0
    assert main(["text", "--text", "B"]) == 0
    files = sorted(tmp_path.glob("ptouch-label-*.bin"))
    assert len(files) == 2  # two distinct, unpredictable names
    assert not (tmp_path / "label.bin").exists()  # not the old predictable name


# --- #14: errors show the exception type; PTOUCH_DEBUG adds a traceback -------

def test_error_includes_exception_type(capsys, monkeypatch):
    monkeypatch.delenv("PTOUCH_DEBUG", raising=False)
    rc = main(["nozzle", "ZZ9"])  # invalid name -> ValueError
    assert rc == 1
    err = capsys.readouterr().err
    assert "error: ValueError:" in err
    assert "Traceback" not in err


def test_ptouch_debug_prints_traceback(capsys, monkeypatch):
    monkeypatch.setenv("PTOUCH_DEBUG", "1")
    rc = main(["nozzle", "ZZ9"])
    assert rc == 1
    assert "Traceback" in capsys.readouterr().err


# --- #15: list distinguishes "no print system" from "no printers" ------------

def test_list_reports_missing_print_system(capsys, monkeypatch):
    monkeypatch.setattr("brother_ptouch.transport.list_printers", lambda: [])
    monkeypatch.setattr("brother_ptouch.transport.has_print_system", lambda: False)
    assert main(["list"]) == 0
    assert "No supported print system" in capsys.readouterr().err


def test_list_reports_no_printers_when_system_present(capsys, monkeypatch):
    monkeypatch.setattr("brother_ptouch.transport.list_printers", lambda: [])
    monkeypatch.setattr("brother_ptouch.transport.has_print_system", lambda: True)
    assert main(["list"]) == 0
    assert "No printers found" in capsys.readouterr().err
