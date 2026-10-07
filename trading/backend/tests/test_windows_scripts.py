"""The Windows launcher scripts must be pure ASCII.

This file exists because of a real bug that reached the operator.

`Start-ATLAS.ps1` had an ASCII-art banner drawn with Unicode box characters.
The file was UTF-8 **without a BOM**, and **Windows PowerShell 5.1 reads a
BOM-less file using the system ANSI codepage**, not UTF-8. On a Western
European Windows that is Windows-1252, so the three bytes of `║` (U+2551 =
`E2 95 91`) were decoded as three separate characters — and the last of them,
byte `0x91`, is U+2018 LEFT SINGLE QUOTATION MARK.

PowerShell accepts smart quotes as string delimiters. That stray `'` opened a
string that never closed, and the parser reported:

    The string is missing the terminator: '.

...blaming a line 30 lines further down that merely happened to contain a
quote. The launcher would not start at all, and the error pointed at innocent
code.

Keeping every script pure ASCII sidesteps the whole class of problem: ASCII
decodes identically under UTF-8, Windows-1252, and every other codepage a
Windows machine is likely to use, with or without a BOM.

If you want a box-drawing banner back, add a UTF-8 BOM to the file *and* test
it on Windows PowerShell 5.1 — not just PowerShell 7, which assumes UTF-8 and
will not reproduce the failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import PROJECT_ROOT

#: Everything Windows executes directly.
SCRIPT_PATTERNS = ("*.ps1", "*.bat", "*.cmd")


def _windows_scripts() -> list[Path]:
    found: list[Path] = []
    for pattern in SCRIPT_PATTERNS:
        found.extend(PROJECT_ROOT.rglob(pattern))
    return sorted(
        path
        for path in found
        if "node_modules" not in path.parts and ".venv" not in path.parts
    )


def test_scripts_exist() -> None:
    """Guards the guard: a glob that silently matches nothing proves nothing."""
    scripts = _windows_scripts()
    names = {p.name for p in scripts}

    assert scripts, "no Windows scripts found — has the project layout changed?"
    for required in ("Start-ATLAS.ps1", "setup.ps1", "Check-Setup.ps1"):
        assert required in names, f"{required} is missing"


@pytest.mark.parametrize("script", _windows_scripts(), ids=lambda p: p.name)
def test_script_is_pure_ascii(script: Path) -> None:
    """A non-ASCII byte in a BOM-less script breaks Windows PowerShell 5.1."""
    raw = script.read_bytes()
    offenders = [(i, b) for i, b in enumerate(raw) if b > 0x7F]

    if offenders:
        index, byte = offenders[0]
        line = raw[:index].count(b"\n") + 1
        snippet = raw[max(0, index - 40) : index + 40].decode("ascii", "replace")
        pytest.fail(
            f"{script.name} contains {len(offenders)} non-ASCII byte(s). "
            f"First is 0x{byte:02X} on line {line}.\n"
            f"  ...{snippet}...\n"
            f"Windows PowerShell 5.1 decodes a BOM-less file as Windows-1252, "
            f"which can turn these bytes into smart quotes and break parsing. "
            f"Use plain ASCII: '-' not '—', and ASCII art not box-drawing."
        )


@pytest.mark.parametrize("script", _windows_scripts(), ids=lambda p: p.name)
def test_script_survives_cp1252_decoding(script: Path) -> None:
    """The file must mean the same thing however Windows decodes it.

    This is the property that actually matters, stated directly: reading the
    bytes as Windows-1252 must produce exactly the same text as reading them
    as UTF-8. For pure ASCII that is guaranteed, and this asserts it rather
    than assuming it.
    """
    raw = script.read_bytes()

    as_utf8 = raw.decode("utf-8", errors="replace")
    as_cp1252 = raw.decode("cp1252", errors="replace")

    assert as_utf8 == as_cp1252, (
        f"{script.name} is interpreted differently as UTF-8 and as Windows-1252. "
        f"Windows PowerShell 5.1 would see the Windows-1252 version."
    )


@pytest.mark.parametrize("script", _windows_scripts(), ids=lambda p: p.name)
def test_script_has_no_smart_quotes(script: Path) -> None:
    """Smart quotes are string delimiters in PowerShell, not decoration."""
    text = script.read_text(encoding="utf-8", errors="replace")

    for char, name in (
        ("‘", "left single"),
        ("’", "right single"),
        ("“", "left double"),
        ("”", "right double"),
    ):
        assert char not in text, (
            f"{script.name} contains a {name} smart quote. PowerShell treats "
            f"these as string delimiters, so they break parsing."
        )
