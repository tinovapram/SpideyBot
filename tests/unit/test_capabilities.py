"""Capability checks — the two dependencies that decide whether the bot is usable.

`tgcrypto` is asserted against the real environment on purpose: if the source build ever
regresses, that test fails instead of the bot quietly running 291x slower.
"""

from __future__ import annotations

from app import capabilities
from app.cli import main

# ── tgcrypto (plan D1 / R1) ─────────────────────────────────────────────


def test_tgcrypto_is_actually_active_here() -> None:
    """Checks the branch Kurigram took, not merely that the module imports."""
    capability = capabilities.check_tgcrypto()

    assert capability.ok, capability.detail
    assert capability.required


def test_the_fallback_is_recognised(monkeypatch) -> None:
    """Simulate Kurigram's fallback: it defines `ige`/`ctr` and imports `pyaes`."""
    from pyrogram.crypto import aes

    monkeypatch.setattr(aes, "pyaes", object(), raising=False)

    capability = capabilities.check_tgcrypto()

    assert not capability.ok
    assert "291" in capability.detail
    assert "slower" in capability.detail


# ── ffmpeg (plan D27) ───────────────────────────────────────────────────


def test_missing_ffmpeg_explains_how_to_fix_it(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.shutil, "which", lambda _name: None)

    capability = capabilities.check_ffmpeg()

    assert not capability.ok
    assert capability.required
    assert capability.status == "MISSING"
    assert "PATH" in capability.detail


def test_present_ffmpeg_reports_where_it_is(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.shutil, "which", lambda name: f"/usr/bin/{name}")

    capability = capabilities.check_ffmpeg()

    assert capability.ok
    assert capability.detail == "/usr/bin/ffmpeg"


def test_ffprobe_is_reported_separately(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert capabilities.check_ffprobe().detail == "/usr/bin/ffprobe"

    monkeypatch.setattr(capabilities.shutil, "which", lambda _name: None)
    assert not capabilities.check_ffprobe().ok


# ── probe and classification ────────────────────────────────────────────


def test_probe_covers_every_required_capability() -> None:
    """ffprobe ships inside the ffmpeg package but is listed separately: the pipeline
    reads media metadata with it, and a stripped build can omit it."""
    probed = capabilities.probe()

    assert {capability.name for capability in probed if capability.required} == {
        "tgcrypto",
        "ffmpeg",
        "ffprobe",
    }


def test_optional_absence_is_not_a_missing_requirement() -> None:
    """uvloop does not exist on Windows, which must never read as a broken install."""
    optional = capabilities.Capability("uvloop", False, "not installed", required=False)
    required = capabilities.Capability("ffmpeg", False, "not found", required=True)

    assert capabilities.missing_required([optional]) == []
    assert capabilities.missing_required([optional, required]) == [required]
    assert optional.status == "absent"
    assert required.status == "MISSING"
    assert required.status != optional.status


# ── the `check` command ─────────────────────────────────────────────────


def test_check_fails_when_a_required_capability_is_missing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(capabilities.shutil, "which", lambda _name: None)

    exit_code = main(["check"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "MISSING" in output
    assert "missing required capability" in output


def test_check_succeeds_when_everything_is_present(monkeypatch, capsys) -> None:
    monkeypatch.setattr(capabilities.shutil, "which", lambda name: f"/usr/bin/{name}")

    exit_code = main(["check"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "MISSING" not in output
    assert "tgcrypto" in output
