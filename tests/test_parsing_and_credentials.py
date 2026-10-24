import pytest

from harness.credentials import SecretStore, render_args
from harness.llm.parsing import ParseError, parse_json_object


def test_plain_json():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_fenced_json_with_prose():
    text = 'Sure! Here you go:\n```json\n{"thought": "x", "actions": [{"tool": "wait", "args": {}}]}\n```\nDone.'
    assert parse_json_object(text)["actions"][0]["tool"] == "wait"


def test_trailing_comma_and_braces_in_strings():
    text = 'prefix {"thought": "use {curly} braces", "actions": [1, 2,],} suffix'
    assert parse_json_object(text) == {"thought": "use {curly} braces", "actions": [1, 2]}


def test_no_json_raises():
    with pytest.raises(ParseError):
        parse_json_object("no json here")


def test_log_lines_are_redacted(monkeypatch, caplog):
    """Regression: a handoff log line printed the IRCTC username in plain text (rehearsal 24-Oct 16:33)."""
    import logging

    from harness.logging_setup import _RedactSecrets

    monkeypatch.setenv("SECRET_DEMO_USER", "sourav-demo-42")
    logger = logging.getLogger("redact-test")
    logger.addFilter(_RedactSecrets())
    with caplog.at_level(logging.INFO, logger="redact-test"):
        logger.info("login dialog open with username %s", "sourav-demo-42")
    assert "sourav-demo-42" not in caplog.text and "***" in caplog.text


def test_secret_resolution_and_redaction(monkeypatch):
    monkeypatch.setenv("SECRET_DEMO_PASS", "hunter2-xyz")
    store = SecretStore()
    vault = {"passenger": {"name": "Asha", "age": 21}}
    args = {"text": "{{secret.DEMO_PASS}}", "who": "{{vault.passenger.name}} ({{vault.passenger.age}})",
            "list": ["{{vault.missing.key}}"]}
    out = render_args(args, vault, store)
    assert out["text"] == "hunter2-xyz"
    assert out["who"] == "Asha (21)"
    assert out["list"] == ["{{vault.missing.key}}"]  # unresolved stays visible for the tool to report
    assert store.redact("typed hunter2-xyz into box") == "typed *** into box"
    assert "DEMO_PASS" in store.available_names()
