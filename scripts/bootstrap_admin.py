"""Idempotently ensures at least one admin account exists.

Run automatically on container startup (docker-compose.yml's `migrate`
service) — unlike scripts/create_user.py, this never errors if an admin
already exists, so it's safe to run on every `docker compose up`, not just
the first one.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from sqlalchemy import select  # noqa: E402

from webapp.auth import ValidationError, create_user  # noqa: E402
from webapp.db import init_db, new_session  # noqa: E402
from webapp.logging_config import configure_logging  # noqa: E402
from webapp.models_db import User  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)


def main() -> int:
    init_db()  # idempotent — no-op if Alembic already created the schema

    with new_session() as db:
        existing_admin = db.scalar(select(User).where(User.role == "admin"))
        if existing_admin:
            logger.info("admin_bootstrap_skipped", extra={"username": existing_admin.username})
            return 0

        username = os.environ.get("ADMIN_USERNAME", "admin")
        full_name = os.environ.get("ADMIN_FULL_NAME", "Administrator")
        password = os.environ.get("ADMIN_PASSWORD")
        if not password:
            password = "admin123"
            logger.warning("admin_bootstrap_using_default_password", extra={"username": username})
        try:
            user = create_user(db, username=username, password=password, full_name=full_name, role="admin")
        except ValidationError as exc:
            logger.error("admin_bootstrap_failed", extra={"error": str(exc)})
            return 1
        logger.info("admin_bootstrap_created", extra={"username": user.username})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
