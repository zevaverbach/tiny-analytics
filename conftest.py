import pytest

from main import init_db, settings


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    """Point the app at a throwaway SQLite DB for every test."""
    original = settings.tinytrack_db_path
    settings.tinytrack_db_path = str(tmp_path / "test.db")
    init_db()
    yield
    settings.tinytrack_db_path = original


@pytest.fixture(autouse=True)
def _open_origins():
    """Default: no origin restrictions. Tests that need them override this."""
    original = settings.tinytrack_allowed_origins
    settings.tinytrack_allowed_origins = []
    yield
    settings.tinytrack_allowed_origins = original
