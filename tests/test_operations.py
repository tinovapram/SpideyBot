from __future__ import annotations

from core.logging import redact_sensitive_fields
from core.metrics import Metrics


def test_sensitive_log_fields_are_redacted() -> None:
    result = redact_sensitive_fields(
        None,
        "info",
        {
            "cookie": "ndus=secret-value",
            "url": "https://example.test/file?token=secret&part=1",
            "session_token": "secret-session-token",
            "session_encrypt_key": "secret-encryption-key",
            "session_max_live": 50,
            "session_idle_seconds": 600,
            "user_id": 42,
        },
    )

    assert result["cookie"] == "[REDACTED]"
    assert result["url"] == "https://example.test/file?token=[REDACTED]&part=1"
    assert result["session_token"] == "[REDACTED]"
    assert result["session_encrypt_key"] == "[REDACTED]"
    assert result["session_max_live"] == 50
    assert result["session_idle_seconds"] == 600
    assert result["user_id"] == 42


def test_metrics_render_prometheus_text() -> None:
    metrics = Metrics(downloads_completed=3, queue_depth=2, active_sessions=1)

    output = metrics.prometheus()

    assert "spideybot_downloads_total 3" in output
    assert "spideybot_queue_depth 2" in output
    assert "spideybot_active_sessions 1" in output
