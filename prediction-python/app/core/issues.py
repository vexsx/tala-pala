"""Bridge Python logging into the shared ``app_issues`` table.

Every WARNING/ERROR record emitted anywhere in the prediction service (job
failures, provider errors, training exceptions, ...) is mirrored into the
database so the dashboard's Issues tab can show one aggregated view across
services. Design constraints:

* **Never break the app**: any database failure inside the handler is
  swallowed (stderr fallback only). Logging must stay total.
* **No recursion**: a thread-local re-entrancy guard stops records emitted
  while writing (e.g. SQLAlchemy warnings) from looping back in.
* **Bounded size**: messages and tracebacks are truncated so a pathological
  error cannot bloat the table.
"""
from __future__ import annotations

import logging
import threading
import traceback
from typing import Optional

from sqlalchemy.engine import Engine

MAX_MESSAGE_CHARS = 2000
MAX_TRACEBACK_CHARS = 6000

_local = threading.local()


class DBIssueHandler(logging.Handler):
    """Writes WARNING+ log records to ``app_issues`` (service='prediction')."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(level=logging.WARNING)
        self._engine = engine

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        if getattr(_local, "writing", False):
            return  # a record emitted while we write -> drop, never recurse
        # uvicorn access logs at WARNING+ are HTTP noise, not app issues
        if record.name.startswith("uvicorn.access"):
            return
        _local.writing = True
        try:
            from ..db import app_issues, utcnow  # late import avoids cycles

            details: dict = {"logger": record.name}
            if record.exc_info and record.exc_info[0] is not None:
                details["exception"] = str(record.exc_info[1])[:MAX_MESSAGE_CHARS]
                details["traceback"] = "".join(
                    traceback.format_exception(*record.exc_info)
                )[-MAX_TRACEBACK_CHARS:]
            with self._engine.begin() as conn:
                conn.execute(
                    app_issues.insert().values(
                        occurred_at=utcnow(),
                        service="prediction",
                        level="error" if record.levelno >= logging.ERROR else "warning",
                        source=record.name[:200],
                        message=record.getMessage()[:MAX_MESSAGE_CHARS],
                        details=details,
                        created_at=utcnow(),
                    )
                )
        except Exception:  # noqa: BLE001 — logging must never raise
            pass
        finally:
            _local.writing = False


def install_issue_capture(engine: Engine) -> Optional[DBIssueHandler]:
    """Attach one DBIssueHandler to the root logger (idempotent)."""
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, DBIssueHandler):
            return handler
    handler = DBIssueHandler(engine)
    root.addHandler(handler)
    return handler
