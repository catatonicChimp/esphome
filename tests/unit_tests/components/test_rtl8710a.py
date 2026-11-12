"""RTL8710A registration and SRAM linker placement regression tests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from esphome.components import bk72xx, libretiny, rtl87xx
from esphome.components.libretiny.const import (
    FAMILY_RTL8710A,
    KEY_FAMILY,
    KEY_LIBRETINY,
)
import esphome.config_validation as cv
from esphome.const import CONF_BOARD, CONF_FAMILY
from esphome.core import CORE, KEY_CORE


def test_board_family() -> None:
    CORE.data[KEY_CORE] = {}
    config = rtl87xx.CONFIG_SCHEMA({CONF_BOARD: "generic-rtl8711am-8mb"})
    assert config[CONF_FAMILY] == FAMILY_RTL8710A
    assert CORE.data[KEY_LIBRETINY][KEY_FAMILY] == FAMILY_RTL8710A
    assert CORE.target_platform == "rtl87xx"


def test_unknown_board_family_override() -> None:
    CORE.data[KEY_CORE] = {}
    config = rtl87xx.CONFIG_SCHEMA({CONF_BOARD: "custom-amb1", CONF_FAMILY: "rtl8710a"})
    assert config[CONF_FAMILY] == FAMILY_RTL8710A


def test_wrong_component_rejected() -> None:
    CORE.data[KEY_CORE] = {}
    with pytest.raises(cv.Invalid, match="doesn't belong to 'bk72xx'"):
        bk72xx.CONFIG_SCHEMA({CONF_BOARD: "custom-amb1", CONF_FAMILY: FAMILY_RTL8710A})


def test_mismatched_family_rejected() -> None:
    CORE.data[KEY_CORE] = {}
    with pytest.raises(cv.Invalid, match="does not match selected board"):
        rtl87xx.CONFIG_SCHEMA(
            {CONF_BOARD: "generic-rtl8711am-8mb", CONF_FAMILY: "RTL8710B"}
        )


@pytest.mark.parametrize(
    ("pin", "number"),
    [
        ("PA06", 6),
        ("PA6", 6),
        ("PA07", 7),
        ("PA7", 7),
        ("PB00", 16),
        ("PB0", 16),
        ("PB01", 17),
        ("PB1", 17),
        ("PC00", 32),
        ("PC0", 32),
        ("PC02", 34),
        ("PC2", 34),
        ("PC03", 35),
        ("PC3", 35),
        ("SERIAL0_RX", 6),
        ("SERIAL0_TX", 7),
        ("SERIAL2_RX", 17),
        ("SERIAL2_TX", 16),
    ],
)
def test_board_pins(pin: str, number: int) -> None:
    CORE.data[KEY_CORE] = {}
    rtl87xx.CONFIG_SCHEMA({CONF_BOARD: "generic-rtl8711am-8mb"})
    assert libretiny.gpio.validate_gpio_pin(pin) == number


@pytest.mark.parametrize("pin", [0, 33, "PC1", "PA0"])
def test_unavailable_pins_rejected(pin: str | int) -> None:
    CORE.data[KEY_CORE] = {}
    rtl87xx.CONFIG_SCHEMA({CONF_BOARD: "generic-rtl8711am-8mb"})
    with pytest.raises(cv.Invalid):
        libretiny.gpio.validate_gpio_pin(pin)


def load_linker_script(variant: str) -> SimpleNamespace:
    """Load the SCons script without requiring PlatformIO or a toolchain."""
    env = Mock()
    env.get.side_effect = {
        "CPPDEFINES": [f"USE_LIBRETINY_VARIANT_{variant}"],
    }.get
    script = Path(libretiny.__file__).with_name("patch_linker.py.script")
    namespace = {"Import": Mock(), "env": env}
    exec(compile(script.read_text(), str(script), "exec"), namespace)
    return SimpleNamespace(**namespace)


@pytest.mark.parametrize(
    ("variant", "section"),
    [("RTL8710A", ".ram_image2.text"), ("RTL8710B", ".ram_image2.data")],
)
def test_sram_placement(variant: str, section: str) -> None:
    script = load_linker_script(variant)
    original = f"{section} :\n{{\n    *(.text*)\n}} > SRAM\n"
    (patcher,) = script._patchers_for(variant)
    patched = patcher(original)
    assert "KEEP(*(.sram.text*))" in patched
    assert patched.index("KEEP(*(.sram.text*))") < patched.index("*(.text*)")
    assert patcher(patched) == patched
    script.env.AddPreAction.assert_called_once()


def test_amb1_does_not_use_external_ram() -> None:
    script = load_linker_script("RTL8710A")
    (patcher,) = script._patchers_for("RTL8710A")
    original = ".sdram.text :\n{\n    *(.text*)\n} > SDRAM\n"
    assert patcher(original) == original


def test_amb1_missing_sram_section_fails(tmp_path: Path) -> None:
    script = load_linker_script("RTL8710A")
    script.env.subst.return_value = str(tmp_path)
    (tmp_path / "image2.ld").write_text(".sdram.text :\n{\n}\n")
    with pytest.raises(RuntimeError, match=r"no .ld .* was patched"):
        script._pre_link(None, None, script.env)


def test_amb1_patches_generated_linker(tmp_path: Path) -> None:
    script = load_linker_script("RTL8710A")
    script.env.subst.return_value = str(tmp_path)
    linker = tmp_path / "image2.ld"
    linker.write_text(".ram_image2.text :\n{\n}\n")
    script._pre_link(None, None, script.env)
    patched = linker.read_text()
    assert "KEEP(*(.sram.text*))" in patched
    script._pre_link(None, None, script.env)
    assert linker.read_text() == patched


def test_existing_linker_families() -> None:
    script = load_linker_script("LN882H")
    (patcher,) = script._patchers_for("LN882H")
    patched = patcher("KEEP(*(.vectors))\n")
    assert patched.index("KEEP(*(.vectors))") < patched.index("KEEP(*(.sram.text*))")
    script = load_linker_script("RTL8720C")
    assert not script._patchers_for("RTL8720C")
    script.env.AddPreAction.assert_not_called()


def test_unknown_linker_family_rejected() -> None:
    with pytest.raises(RuntimeError, match="unknown LibreTiny variant"):
        load_linker_script("UNKNOWN")
