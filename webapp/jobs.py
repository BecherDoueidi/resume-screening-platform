"""Redis connection + RQ queue setup for background resume processing.

Reads REDIS_URL from the environment. Real Redis is required for both local
development and production (just different hosts) — same policy as
DATABASE_URL in webapp/db.py. No default is provided in production-shaped
code; missing config fails loudly instead of silently degrading.
"""

from __future__ import annotations

import os

import redis
from rq import Queue, Retry

QUEUE_NAME = "resume-processing"

# Applied to every enqueued job: 3 attempts total, with backoff between retries.
DEFAULT_RETRY = Retry(max=3, interval=[10, 30, 60])

_redis_conn = None
_queue: Queue | None = None


def _redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError(
            "REDIS_URL is not set. Point it at a Redis instance, e.g. "
            "redis://localhost:6379/0 (docker run -p 6379:6379 redis:7-alpine)."
        )
    return url


def get_redis_connection():
    global _redis_conn
    if _redis_conn is None:
        _redis_conn = redis.from_url(_redis_url())
    return _redis_conn


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue(QUEUE_NAME, connection=get_redis_connection())
    return _queue
