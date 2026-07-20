"""Structured (JSON) logging, shared by the Flask app and the RQ worker.

Every log line is one JSON object: timestamp, level, logger name, message,
plus `request_id` (bound per-HTTP-request in webapp/app.py) and
`processing_id` (bound per-background-job in webapp/tasks.py) so a single
request or job's log lines can be grepped out of an aggregated log stream
even with many requests/jobs interleaved across gunicorn/RQ worker processes.

contextvars, not a plain module-level variable — each request/job runs on
its own logical context (gunicorn's sync workers are separate processes
anyway, but this also makes the pattern correct if the app ever moves to an
async/greenlet worker class, where a plain global would leak between them).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import uuid

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
processing_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("processing_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class _ContextFilter(logging.Filter):
    """Stamps every record with the current request_id/processing_id, if any."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.processing_id = processing_id_var.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "processing_id": getattr(record, "processing_id", "-"),
        }
        # Anything passed via logger.info(..., extra={...}) beyond the standard
        # LogRecord attributes — e.g. duration_ms, resume_id, backend — folds
        # straight into the JSON object rather than needing named handling here.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    """Idempotent — safe to call from both webapp/app.py and webapp/worker.py
    (and anything that imports both) without installing duplicate handlers."""
    root = logging.getLogger()
    if getattr(root, "_structured_logging_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_ContextFilter())
    root.handlers = [handler]
    root.setLevel((level or os.environ.get("LOG_LEVEL", "INFO")).upper())
    root._structured_logging_configured = True  # type: ignore[attr-defined]


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_request_id(request_id: str | None = None) -> str:
    rid = request_id or new_id()
    request_id_var.set(rid)
    return rid


def bind_processing_id(processing_id: str) -> None:
    processing_id_var.set(processing_id)
