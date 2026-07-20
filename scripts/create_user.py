"""Create a user account (bootstrap the first admin, or add more later).

Real accounts replaced the old shared ADMIN_PASSWORD — there is no other way
to get the very first login, so this script exists specifically to break
that chicken-and-egg problem. Once at least one admin exists, more users
(any role) can be created from the dashboard's Manage Users page instead.

Usage:
    python scripts/create_user.py --username alice --role admin
    (prompts for a password; add --password to skip the prompt, e.g. in CI)
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from webapp.auth import ValidationError, create_user  # noqa: E402
from webapp.db import init_db, new_session  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", required=True, choices=["admin", "recruiter", "viewer"])
    parser.add_argument("--full-name", default="")
    parser.add_argument("--password", help="Omit to be prompted (recommended over passing on the CLI).")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password (min 8 characters): ")

    init_db()  # dev/CI convenience; production should already have run `alembic upgrade head`
    try:
        with new_session() as db:
            user = create_user(
                db,
                username=args.username,
                password=password,
                full_name=args.full_name,
                role=args.role,
            )
            print(f"Created user '{user.username}' (role={user.role}, id={user.id}).")
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
