"""RQ worker entry point.

Run this in its own process alongside `python webapp/app.py`:

    python webapp/worker.py

Requires REDIS_URL (and DATABASE_URL) in the environment — same .env the
Flask app reads, since load_dotenv() is called here too.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from rq import SimpleWorker, Worker  # noqa: E402

from webapp.jobs import QUEUE_NAME, get_queue, get_redis_connection  # noqa: E402

if __name__ == "__main__":
    # The default Worker forks a child process per job (os.fork()), which
    # doesn't exist on Windows. SimpleWorker runs jobs in-process instead —
    # used automatically on win32 for local dev; Linux/production (Docker)
    # gets the real forking Worker for per-job crash isolation.
    worker_cls = SimpleWorker if sys.platform == "win32" else Worker
    worker = worker_cls([get_queue()], connection=get_redis_connection())
    print(f"[worker] listening on queue '{QUEUE_NAME}' (using {worker_cls.__name__}) ...")
    worker.work(with_scheduler=True)
