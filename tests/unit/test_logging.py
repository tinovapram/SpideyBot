"""Redaction: the only thing standing between a plaintext credential and a log file."""

from __future__ import annotations

import logging

import pytest
import structlog

from app.logging import (
    REDACTED,
    RedactingFilter,
    Redactor,
    mask_secret,
    setup_logging,
)

SECRET = "supersecretvalue"


# ── Redactor ────────────────────────────────────────────────────────────


def test_short_values_are_not_registered() -> None:
    """A 3-character value would redact unrelated text everywhere it appeared."""
    redactor = Redactor()
    redactor.register("abc")
    redactor.register("")
    redactor.register(None)

    assert redactor.count == 0
    assert redactor.scrub("abcdef") == "abcdef"


def test_minimum_length_boundary_is_inclusive() -> None:
    redactor = Redactor(["abcdef"])

    assert redactor.count == 1


def test_duplicates_are_registered_once() -> None:
    redactor = Redactor([SECRET, SECRET])

    assert redactor.count == 1


def test_longest_value_wins_so_nothing_is_left_behind() -> None:
    """Otherwise the shorter value redacts first and leaves a visible tail."""
    redactor = Redactor(["abcdef", "abcdefgh"])

    assert redactor.scrub("abcdefgh") == REDACTED
    assert redactor.scrub("abcdef") == REDACTED


def test_scrub_replaces_every_occurrence() -> None:
    redactor = Redactor([SECRET])

    assert redactor.scrub(f"a {SECRET} b {SECRET}") == f"a {REDACTED} b {REDACTED}"


def test_scrub_value_walks_nested_containers() -> None:
    redactor = Redactor([SECRET])

    assert redactor.scrub_value({"k": [SECRET, {"n": SECRET}]}) == {
        "k": [REDACTED, {"n": REDACTED}]
    }
    assert redactor.scrub_value((SECRET,)) == (REDACTED,)
    assert redactor.scrub_value({SECRET}) == {REDACTED}


def test_scrub_value_leaves_non_strings_alone() -> None:
    redactor = Redactor([SECRET])

    assert redactor.scrub_value(123) == 123
    assert redactor.scrub_value(None) is None


# ── RedactingFilter ─────────────────────────────────────────────────────


def _record(msg: object, args: object = None) -> logging.LogRecord:
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, None)


def test_filter_scrubs_message_and_positional_args() -> None:
    record = _record("token=%s", (SECRET,))
    RedactingFilter(Redactor([SECRET])).filter(record)

    assert record.args == (REDACTED,)
    assert record.getMessage() == f"token={REDACTED}"


def test_filter_scrubs_keyword_args() -> None:
    """The `%(name)s` form: the mapping is what `getMessage` formats through."""
    record = _record("token=%(token)s", ({"token": SECRET},))
    RedactingFilter(Redactor([SECRET])).filter(record)

    assert record.getMessage() == f"token={REDACTED}"


def test_no_copy_of_the_secret_survives_anywhere_on_the_record() -> None:
    """Formatting and attributes are both printable, so neither may retain the value."""
    record = _record("token=%(token)s", ({"token": SECRET},))
    RedactingFilter(Redactor([SECRET])).filter(record)

    assert SECRET not in record.getMessage()
    assert SECRET not in str(record.__dict__)


def test_filter_scrubs_a_mapping_nested_beside_other_args() -> None:
    record = _record("%s %(token)s", ("label", {"token": SECRET}))
    RedactingFilter(Redactor([SECRET])).filter(record)

    assert SECRET not in str(record.args)


def test_filter_leaves_mapping_args_that_are_not_secrets() -> None:
    record = _record("n=%(n)s", ({"n": 41},))
    RedactingFilter(Redactor([SECRET])).filter(record)

    assert record.getMessage() == "n=41"


def test_filter_scrubs_preformatted_traceback() -> None:
    """Stdlib formats tracebacks after the processors, so this path is the safety net."""
    record = _record("boom")
    record.exc_text = f"ValueError: {SECRET}"
    RedactingFilter(Redactor([SECRET])).filter(record)

    assert record.exc_text == f"ValueError: {REDACTED}"


def test_filter_never_drops_the_record() -> None:
    record = _record("harmless")

    assert RedactingFilter(Redactor([SECRET])).filter(record) is True


# ── mask_secret ─────────────────────────────────────────────────────────


def test_mask_keeps_the_documented_windows() -> None:
    # 13 chars: 4 shown, 7 hidden, 2 shown.
    assert mask_secret("ndus=abcdefgh") == "ndus*******gh"


def test_mask_hides_short_values_entirely() -> None:
    """With nothing to hide behind the windows, revealing 4+2 chars would leak most of it."""
    assert mask_secret("abcd") == "****"
    assert mask_secret("abcdef") == "******"


def test_mask_of_empty_value_is_empty() -> None:
    assert mask_secret("") == ""


# ── setup_logging, end to end ───────────────────────────────────────────


@pytest.fixture
def restore_logging():
    """setup_logging mutates global state, so put it back."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    structlog.reset_defaults()


def test_setup_logging_redacts_a_real_stdlib_call(restore_logging, capsys) -> None:
    redactor = setup_logging(level=logging.INFO, secrets=[SECRET])

    logging.getLogger("test").error("token=%s", SECRET)
    captured = capsys.readouterr().out

    assert redactor.count == 1
    assert SECRET not in captured
    assert REDACTED in captured


def test_setup_logging_redacts_the_structured_event_dict(restore_logging, capsys) -> None:
    setup_logging(level=logging.INFO, secrets=[SECRET])

    structlog.get_logger("test").error("failed", token=SECRET)
    captured = capsys.readouterr().out

    assert SECRET not in captured
    assert REDACTED in captured
