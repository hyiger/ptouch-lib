"""Config loading + CLI precedence coverage."""

import pytest

import brother_ptouch.transport as transport
from brother_ptouch.cli import main
from brother_ptouch.config import (
    Config,
    ConfigError,
    find_config_path,
    load_config,
    resolve_config,
)
from brother_ptouch.simulator import decode


def _write(tmp_path, body):
    p = tmp_path / "ptouch.toml"
    p.write_text(body)
    return str(p)


# --------------------------------------------------------------------------- #
# load_config
# --------------------------------------------------------------------------- #


def test_load_full_config(tmp_path):
    path = _write(
        tmp_path,
        'printer = "usb://Brother/PT-P710BT?serial=X"\n'
        "tape = 18\n"
        "auto_cut = false\n"
        "margin_dots = 20\n"
        'font = "/fonts/Arial.ttf"\n'
        "font_size = 36\n"
        'orientation = "vertical"\n',
    )
    cfg = load_config(path)
    assert cfg.printer == "usb://Brother/PT-P710BT?serial=X"
    assert cfg.tape == 18.0
    assert cfg.auto_cut is False
    assert cfg.margin_dots == 20
    assert cfg.font == "/fonts/Arial.ttf"
    assert cfg.font_size == 36
    assert cfg.orientation == "vertical"
    assert cfg.source == path


def test_partial_config_leaves_rest_none(tmp_path):
    cfg = load_config(_write(tmp_path, "font_size = 50\n"))
    assert cfg.font_size == 50
    assert cfg.printer is None and cfg.tape is None and cfg.auto_cut is None


def test_missing_explicit_file_errors():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/no/such/ptouch.toml")


def test_unknown_key_errors(tmp_path):
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(_write(tmp_path, "fnotsize = 40\n"))


def test_invalid_toml_errors(tmp_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(_write(tmp_path, "font_size = = 40\n"))


@pytest.mark.parametrize(
    "body,msg",
    [
        ("tape = true\n", "'tape' must be a number"),
        ("tape = -5\n", "'tape' must be positive"),
        ("auto_cut = 1\n", "'auto_cut' must be true or false"),
        ("margin_dots = 1.5\n", "'margin_dots' must be an integer"),
        ("margin_dots = 70000\n", "must be in"),
        ("font_size = 0\n", "'font_size' must be positive"),
        ('orientation = "sideways"\n', "orientation"),
        ("printer = 5\n", "'printer' must be a string"),
        ('layout = "diagonal"\n', "'layout' must be"),
        ('qr_ec = "Z"\n', "'qr_ec' must be"),
    ],
)
def test_value_validation(tmp_path, body, msg):
    with pytest.raises(ConfigError, match=msg):
        load_config(_write(tmp_path, body))


def test_load_code_defaults(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            'layout = "stack"\nqr_ec = "h"\nbarcode_symbology = "ean13"\naruco_dict = "5X5_100"\n',
        )
    )
    assert cfg.layout == "stack"
    assert cfg.qr_ec == "H"  # normalized to upper
    assert cfg.barcode_symbology == "ean13"
    assert cfg.aruco_dict == "5X5_100"


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_find_config_path_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PTOUCH_CONFIG", "/explicit/ptouch.toml")
    assert find_config_path() == "/explicit/ptouch.toml"


def test_find_config_path_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("PTOUCH_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # keep ~ candidates out of the way
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ptouch.toml").write_text("tape = 12\n")
    assert find_config_path() == str(tmp_path / "ptouch.toml")


def test_resolve_config_returns_empty_when_none(tmp_path, monkeypatch):
    monkeypatch.delenv("PTOUCH_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.chdir(tmp_path)
    assert resolve_config(None) == Config()


# --------------------------------------------------------------------------- #
# CLI precedence
# --------------------------------------------------------------------------- #


def test_cli_uses_config_defaults(tmp_path):
    path = _write(tmp_path, "tape = 24\nauto_cut = false\nmargin_dots = 30\n")
    out = tmp_path / "o.bin"
    rc = main(["text", "--text", "Hi", "--config", path, "--out", str(out)])
    assert rc == 0
    d = decode(out.read_bytes())
    assert d.auto_cut is False  # from config
    assert d.tape_width_mm == 24
    assert d.margin_dots == 30


def test_cli_flag_overrides_config(tmp_path):
    path = _write(tmp_path, "auto_cut = false\nmargin_dots = 30\n")
    out = tmp_path / "o.bin"
    rc = main(["text", "--text", "Hi", "--config", path, "--cut", "--margin-dots", "10", "--out", str(out)])
    assert rc == 0
    d = decode(out.read_bytes())
    assert d.auto_cut is True  # --cut overrides config auto_cut=false
    assert d.margin_dots == 10


def test_cli_config_printer_used_when_no_target(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(transport, "print_raster", lambda target, data: captured.update(target=target))
    path = _write(tmp_path, 'printer = "usb://Brother/PT-P710BT?serial=CFG"\n')
    rc = main(["text", "--text", "Hi", "--config", path])
    assert rc == 0
    assert captured["target"] == "usb://Brother/PT-P710BT?serial=CFG"


def test_cli_printer_flag_overrides_config_printer(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(transport, "print_raster", lambda target, data: captured.update(target=target))
    path = _write(tmp_path, 'printer = "usb://Brother/PT-P710BT?serial=CFG"\n')
    rc = main(["text", "--text", "Hi", "--config", path, "--printer", "MyQueue"])
    assert rc == 0
    assert captured["target"] == "MyQueue"


def test_cli_bad_config_reports_error(tmp_path, capsys):
    path = _write(tmp_path, "tape = -1\n")
    rc = main(["text", "--text", "Hi", "--config", path, "--out", str(tmp_path / "o.bin")])
    assert rc == 1
    assert "must be positive" in capsys.readouterr().err
