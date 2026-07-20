"""Shared pytest fixtures.

Env vars required by webapp modules (DATABASE_URL, REDIS_URL, FLASK_SECRET_KEY)
are set here, at conftest module-import time — pytest imports conftest.py
before it collects/imports any test module, so this runs before anything
that does `from webapp import ...` at module scope.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# A unique file per test run — never reused, so there's nothing to delete (and
# therefore nothing that can fail to delete) at session start. Best-effort
# cleanup at session end; a leftover file here is harmless, just ignored on
# the next run since a fresh unique name is always chosen.
_TEST_DB_PATH = ROOT / "webapp" / f"test_screener_{uuid.uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH.as_posix()}"
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")  # never actually dialed in tests — always mocked
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-not-for-production-use")

import pytest  # noqa: E402
from reportlab.lib.pagesizes import LETTER  # noqa: E402
from reportlab.platypus import Paragraph, SimpleDocTemplate  # noqa: E402

from webapp import models_db  # noqa: E402,F401  (registers all tables on Base.metadata)
from webapp.db import Base, get_engine, new_session  # noqa: E402

from screener.models import JobDescription  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Creates the schema once for the whole test session; tears it down after."""
    Base.metadata.create_all(get_engine())
    yield
    get_engine().dispose()
    try:
        _TEST_DB_PATH.unlink(missing_ok=True)
    except PermissionError:
        pass  # best-effort — a stray open handle here doesn't affect test correctness


@pytest.fixture(autouse=True)
def _clean_tables(_test_database):
    """Every test starts from an empty database — deletes all rows first."""
    with new_session() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    yield


@pytest.fixture
def db_session():
    with new_session() as session:
        yield session


@pytest.fixture
def sample_jd() -> JobDescription:
    return JobDescription(
        title="Senior Backend Engineer",
        summary="Design and scale backend services.",
        required_skills=["Python", "PostgreSQL", "REST APIs"],
        nice_to_have=["Kubernetes"],
        min_years_experience=5,
        responsibilities=["Own service reliability", "Design APIs"],
    )


@pytest.fixture
def make_pdf(tmp_path):
    """Builds a minimal real PDF containing the given text lines, returning its path.
    Used to unit-test the actual PDF parser rather than mocking pdfplumber."""

    def _make(lines: list[str], filename: str = "resume.pdf") -> Path:
        path = tmp_path / filename
        doc = SimpleDocTemplate(str(path), pagesize=LETTER)
        doc.build([Paragraph(line) for line in lines])
        return path

    return _make


@pytest.fixture
def sample_resume_text() -> str:
    return (
        "John Smith\n"
        "john.smith@example.com | (555) 123-4567 | linkedin.com/in/johnsmith\n"
        "Summary\n"
        "Backend engineer with 8 years of experience in Python and PostgreSQL.\n"
        "Experience\n"
        "Senior Engineer, Acme Corp (2019-present)\n"
        "Built scalable APIs serving 1M requests per day.\n"
        "Education\n"
        "B.Sc. Computer Science, Springfield University, Class of 2015\n"
        "He is a dedicated engineer who exceeded his targets.\n"
    )
