import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parent))


import pytest


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    """Retry backoff is real seconds in production; tests don't need to wait."""
    monkeypatch.setattr("conductor.llm.RETRY_DELAYS", [0, 0, 0, 0])


@pytest.fixture(autouse=True)
def _no_real_claude_cli(monkeypatch):
    """Never launch the real `claude` CLI from tests (it may be installed on the machine)."""
    monkeypatch.setattr("conductor.claude_cli.find", lambda path="": None)


@pytest.fixture(autouse=True)
def _no_headroom_proxy_probe(monkeypatch):
    """Don't look for a real Headroom proxy on this machine during tests."""
    monkeypatch.setattr("conductor.agent.proxy_url", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_real_headroom_proxy(monkeypatch):
    """Never launch a real Headroom proxy from tests."""
    monkeypatch.setattr("conductor.compression.HeadroomProxy.start", lambda self: False)


@pytest.fixture(autouse=True)
def _no_real_copilot_cli(monkeypatch):
    """Never launch a real GitHub Copilot CLI, or touch ~/.copilot, from tests."""
    monkeypatch.setattr("conductor.copilot_cli.find", lambda path="": None)
    monkeypatch.setattr("conductor.agent.install_copilot_hook", lambda: None)
