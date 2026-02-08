import pytest

from tiny_analytics.app import init_db, settings


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    """Point the app at a throwaway SQLite DB for every test."""
    original = settings.db_path
    settings.db_path = str(tmp_path / "test.db")
    init_db()
    yield
    settings.db_path = original


@pytest.fixture(autouse=True)
def _open_origins():
    """Default: no origin restrictions. Tests that need them override this."""
    original = settings.allowed_origins
    settings.allowed_origins = []
    yield
    settings.allowed_origins = original
