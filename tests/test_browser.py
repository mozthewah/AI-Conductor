"""Smoke test for the real browser tool (skipped when Playwright/Chromium aren't available)."""
import pytest

from conductor.agent import Conductor
from conductor.config import Settings
from conductor.tools import ToolContext

from fakes import FakeLLM

pytest.importorskip("playwright")


async def test_browser_goto_and_read(tmp_path):
    page = tmp_path / "ws" / "hello.html"
    page.parent.mkdir(parents=True)
    page.write_text("<title>Hi</title><h1>Hello conductor</h1><a href='https://example.com'>ex</a>")
    c = Conductor(Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", browser=True), llm=FakeLLM([]))
    if "browser" not in c.tools:
        pytest.skip("browser tool disabled")
    tid = c.store.create_task("t")
    ctx = ToolContext(conductor=c, task_id=tid)
    try:
        out = await c.tools["browser"].handler(ctx, {"action": "goto", "url": f"file://{page}"})
    except Exception as e:  # no Chromium on this machine
        pytest.skip(f"no browser: {e}")
    assert out[1]["type"] == "image" and "Hi" in out[0]["text"]
    text = await c.tools["browser"].handler(ctx, {"action": "read"})
    assert "Hello conductor" in text and "example.com" in text
    assert any(e["type"] == "screenshot" for e in c.store.load_events(tid))
    await c.shutdown()
