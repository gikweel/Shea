import json
import logging
from datetime import UTC, datetime
from typing import Any

from .context import get_correlation_id, get_profile_id, get_task_id


class StructuredTracer(logging.Filter):
    """Injects correlation_id, task_id, and profile_id into standard log records."""
    
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "unknown"
        record.task_id = get_task_id() or "unknown"
        record.profile_id = get_profile_id() or "system"
        return True

class JsonFormatter(logging.Formatter):
    """Formats log records as JSON including observability context."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "unknown"),
            "task_id": getattr(record, "task_id", "unknown"),
            "profile_id": getattr(record, "profile_id", "system"),
        }
        
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
            
        return json.dumps(log_obj)

_TRACING_HANDLER_MARK = "_shea_structured_tracing"


def configure_tracing(level: int = logging.INFO) -> None:
    """Initialize the global structured tracer for the application.

    Idempotent, and deliberately polite to its host: it manages only the
    handler it installed itself. Repeated calls replace (and *close*, so no
    file descriptor leaks) the previous tracing handler rather than stacking
    duplicates, and handlers belonging to the embedding application — or to a
    test harness — are left exactly as they were.
    """
    root_logger = logging.getLogger()

    for existing in root_logger.handlers[:]:
        if getattr(existing, _TRACING_HANDLER_MARK, False):
            root_logger.removeHandler(existing)
            existing.close()

    handler = logging.FileHandler(".shea_logs.json")
    setattr(handler, _TRACING_HANDLER_MARK, True)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(StructuredTracer())

    root_logger.addHandler(handler)
    root_logger.setLevel(level)