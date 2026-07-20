"""Authentication & authorization.

Replaces the old single-shared-password demo gate (ADMIN_PASSWORD +
session["is_admin"]) with real per-user accounts: password hashing
(werkzeug's scrypt-based hasher — no plaintext ever stored), Flask-Login for
session management (login_user/logout_user/current_user, signed session
cookie via app.secret_key), and role-based permissions.

Roles: admin | recruiter | viewer. See ROLE_PERMISSIONS below for exactly
what each can do; templates and routes both consult the same map so the UI
never offers an action a role can't perform.
"""

from __future__ import annotations

import re
from functools import wraps

from flask import abort, jsonify
from flask_login import LoginManager, current_user, login_required
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from webapp.db import new_session
from webapp.models_db import User

__all__ = [
    "login_manager",
    "login_required",
    "require_permission",
    "require_api_permission",
    "current_user",
    "hash_password",
    "verify_password",
    "create_user",
    "authenticate",
    "ROLES",
    "has_permission",
    "ValidationError",
]

ROLES = ("admin", "recruiter", "viewer")

# Permission constants — routes and templates both check against these, never
# against a role name directly, so adding a role only means updating this map.
VIEW = "view"
MANAGE_CANDIDATES = "manage_candidates"
MANAGE_JOBS = "manage_jobs"
DELETE_ALL = "delete_all"
MANAGE_USERS = "manage_users"

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {VIEW, MANAGE_CANDIDATES, MANAGE_JOBS, DELETE_ALL, MANAGE_USERS},
    "recruiter": {VIEW, MANAGE_CANDIDATES, MANAGE_JOBS},
    "viewer": {VIEW},
}


class ValidationError(ValueError):
    """Raised on bad input (username/password/role) — caught by routes to show a form error."""


def has_permission(user, permission: str) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return permission in ROLE_PERMISSIONS.get(user.role, set())


def require_permission(permission: str):
    """Like @login_required, but also 403s if the logged-in user's role lacks `permission`.

    HTML-oriented: an unauthenticated request gets redirected to the login
    page (flask_login's default @login_required behavior). For the JSON API
    (webapp/api.py), use require_api_permission instead — a browser redirect
    is not a sane response for a fetch()/curl/API-client caller.
    """

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not has_permission(current_user, permission):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def require_api_permission(permission: str):
    """Like require_permission, but for JSON API routes: 401 (not authenticated)
    or 403 (authenticated, wrong role) as a JSON body, never an HTML redirect."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return jsonify(error={"code": "unauthorized", "message": "Authentication required."}), 401
            if not has_permission(current_user, permission):
                return jsonify(error={"code": "forbidden", "message": "Your role does not permit this action."}), 403
            return view(*args, **kwargs)

        return wrapped

    return decorator


# --- Password hashing (werkzeug: scrypt by default, salted, never reversible) --


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


# --- Input validation ------------------------------------------------------

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,64}$")


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValidationError("Username must be 3-64 characters: letters, numbers, underscore, dot, or hyphen only.")
    return username


def validate_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValidationError("Password must be at least 8 characters.")
    return password


def validate_role(role: str) -> str:
    if role not in ROLES:
        raise ValidationError(f"Role must be one of: {', '.join(ROLES)}.")
    return role


# --- User CRUD ---------------------------------------------------------


def create_user(session, *, username: str, password: str, full_name: str, role: str) -> User:
    username = validate_username(username)
    validate_password(password)
    role = validate_role(role)
    full_name = (full_name or "").strip()

    if session.scalar(select(User).where(User.username == username)):
        raise ValidationError(f"Username '{username}' is already taken.")

    user = User(
        username=username,
        full_name=full_name,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    session.commit()
    return user


def authenticate(session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username.strip()))
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


# --- Flask-Login wiring --------------------------------------------------

login_manager = LoginManager()
login_manager.login_view = "admin_login"


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    with new_session() as db:
        return db.get(User, int(user_id))
