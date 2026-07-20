"""Fixtures shared by the integration tests: a real Flask app + test client
wired to the isolated test database (see tests/conftest.py), with CSRF
disabled by default (one dedicated test re-enables it to prove it's wired up)."""

from __future__ import annotations

import pytest

from webapp.app import app as flask_app
from webapp.auth import create_user


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _login_as(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


@pytest.fixture
def admin_user(db_session):
    return create_user(db_session, username="admin1", password="adminpass123", full_name="Admin One", role="admin")


@pytest.fixture
def recruiter_user(db_session):
    return create_user(
        db_session, username="recruiter1", password="recruitpass123", full_name="Recruiter One", role="recruiter"
    )


@pytest.fixture
def viewer_user(db_session):
    return create_user(db_session, username="viewer1", password="viewerpass123", full_name="Viewer One", role="viewer")


@pytest.fixture
def admin_client(client, admin_user):
    _login_as(client, admin_user)
    return client


@pytest.fixture
def recruiter_client(client, recruiter_user):
    _login_as(client, recruiter_user)
    return client


@pytest.fixture
def viewer_client(client, viewer_user):
    _login_as(client, viewer_user)
    return client
