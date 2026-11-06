import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parent))


import pytest


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    """Retry backoff is real seconds in production; tests don't need to wait."""
    monkeypatch.setattr("conductor.llm.RETRY_DELAYS", [0, 0, 0, 0])
