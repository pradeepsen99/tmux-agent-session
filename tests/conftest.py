import pytest


@pytest.fixture(autouse=True)
def isolated_metadata_cache(tmp_path, monkeypatch):
    """Keep loader tests from reading or writing the real metadata cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
