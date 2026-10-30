import io
import json
import logging

import pytest

from src.logging_config import JsonFormatter, configure_logging, redact, request_id_var

JWT = (
    "eyJhbGciOiJSUzI1NiJ9."
    "eyJzdWIiOiJ1c2VyIn0."
    "c2lnbmF0dXJlLXBhcnQ"
)


@pytest.mark.parametrize(
    "text,secret",
    [
        (f"Authorization: Bearer {JWT}", JWT),
        (f"got token {JWT} here", JWT),
        ("header Bearer abc.def-ghi_jkl", "abc.def-ghi_jkl"),
        ("key AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP"),
        ("secret_access_key=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"),
        ("password: hunter2", "hunter2"),
        ("session_token=FwoGZXIvYXdz", "FwoGZXIvYXdz"),
    ],
)
def test_secrets_are_redacted(text, secret):
    assert secret not in redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "Authentication rejected: invalid_token:InvalidSignatureError",
        "Authentication rejected: not_access_token",
        "request method=GET route=/api/runs status=200 duration_ms=3 sub=user-123",
        "Marked 2 interrupted run(s) from a previous process",
    ],
)
def test_diagnostic_messages_are_left_intact(text):
    assert redact(text) == text


def _format(record_kwargs):
    record = logging.LogRecord("src.test", logging.ERROR, __file__, 1, **record_kwargs)
    return json.loads(JsonFormatter().format(record))


def test_json_formatter_includes_context_and_redacts_message_and_exception():
    token = request_id_var.set("req-123")
    try:
        try:
            raise RuntimeError(f"bad Bearer {JWT} secret=abc123")
        except RuntimeError:
            import sys

            entry = _format({"msg": "failed with %s", "args": (f"token={JWT}",), "exc_info": sys.exc_info()})
    finally:
        request_id_var.reset(token)

    assert entry["request_id"] == "req-123"
    assert entry["level"] == "ERROR"
    assert JWT not in json.dumps(entry)
    assert "abc123" not in json.dumps(entry)
    assert "RuntimeError" in entry["exception"]


def test_configure_logging_is_idempotent_and_installs_redacting_handler():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        root.handlers = []
        configure_logging()
        configure_logging()

        installed = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
        assert len(installed) == 1
    finally:
        root.handlers, root.level = saved_handlers, saved_level


def test_log_line_never_contains_the_bearer_token():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.redaction")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("incoming Authorization: Bearer %s", JWT)
    finally:
        logger.removeHandler(handler)

    assert JWT not in stream.getvalue()
    assert JWT.split(".")[1] not in stream.getvalue()
