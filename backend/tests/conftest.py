"""Establish one clean application database before test-module imports.

Several API test modules configure ``DATABASE_URL`` at import time. Once
``app.config`` has been imported, however, SQLAlchemy correctly keeps the first
engine for the process. A session-scoped path here makes collection order
irrelevant and prevents a developer's persistent ``patra.db`` from leaking
state into the combined suite.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


_TEST_ROOT = Path(tempfile.mkdtemp(prefix="cloudbali-pytest-session-"))
os.environ["DATABASE_URL"] = "sqlite:///" + (_TEST_ROOT / "app.db").as_posix()
os.environ["JWT_SECRET"] = "test-secret"
os.environ["SEED_PASSWORD_JOOHAN"] = "admin-test-password"
os.environ["SEED_PASSWORD_GUKSEO"] = "admin-test-password"


@pytest.fixture(autouse=True)
def _stub_live_candidate_approval_revalidation(monkeypatch: pytest.MonkeyPatch):
    """Unit/API tests never depend on live OSM or Wikidata availability."""

    monkeypatch.setattr(
        "app.discovery.revalidate_candidate_lifecycle",
        lambda _candidate: ("", {"checked_at": "2026-08-23T00:00:00+00:00", "checks": [{"source": "test", "active": True}]}),
    )
