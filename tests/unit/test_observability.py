import json
import logging
from pathlib import Path

import pytest

from shea.observability.context import active_context, get_correlation_id
from shea.observability.tracer import configure_tracing


def test_contextvars_flow():
    with active_context(correlation_id="req-123", task_id="task-456", profile_id="prof-789"):
        assert get_correlation_id() == "req-123"
        
        # Test nested
        with active_context(correlation_id="req-nested"):
            assert get_correlation_id() == "req-nested"
            
        assert get_correlation_id() == "req-123"
        
    assert get_correlation_id() is None

def test_tracer_json(caplog):
    configure_tracing(logging.INFO)
    logger = logging.getLogger("test_logger")
    
    with active_context(correlation_id="trace-123", task_id="tsk-99", profile_id="prof-1"):
        logger.info("Hello world")
        
    # Since caplog captures before formatters in some cases, we just test the ContextVar binding directly.
    # To truly test the formatter we can construct a LogRecord.
    from shea.observability.tracer import JsonFormatter, StructuredTracer
    
    record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
    tracer = StructuredTracer()
    
    with active_context(correlation_id="trace-abc"):
        tracer.filter(record)
        
    assert record.correlation_id == "trace-abc"
    
    formatter = JsonFormatter()
    out = formatter.format(record)
    data = json.loads(out)
    assert data["correlation_id"] == "trace-abc"
    assert data["message"] == "msg"


def test_configure_tracing_leaves_the_hosts_own_handlers_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # the log file is cwd-relative; keep it out of the repo
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        configure_tracing(logging.INFO)
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)
        _remove_tracing_handlers(root)


def test_configure_tracing_is_idempotent_and_closes_the_handler_it_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    try:
        configure_tracing(logging.INFO)
        first = [h for h in root.handlers if getattr(h, "_shea_structured_tracing", False)]
        assert len(first) == 1

        configure_tracing(logging.INFO)
        second = [h for h in root.handlers if getattr(h, "_shea_structured_tracing", False)]

        assert len(second) == 1  # replaced, not stacked
        assert second[0] is not first[0]
        assert first[0].stream is None  # type: ignore[attr-defined]  # closed: no leaked fd
    finally:
        _remove_tracing_handlers(root)


def _remove_tracing_handlers(root: logging.Logger) -> None:
    for handler in root.handlers[:]:
        if getattr(handler, "_shea_structured_tracing", False):
            root.removeHandler(handler)
            handler.close()