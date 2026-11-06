"""A real browser the conductor can drive (Playwright + Chromium).

Each task gets its own tab. After every action the model receives a screenshot,
and the UI's "Computer" panel shows the same view live.
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

from ..safety import assess_browser
from . import Tool, ToolContext, ToolError, clip

ACTIONS = ["goto", "click", "type", "press", "scroll", "back", "read", "screenshot", "close"]
FALLBACK_CHROMIUM = ["/opt/pw-browsers/chromium"]


def available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class BrowserPool:
    def __init__(self, shots_dir: Path):
        self.shots_dir = shots_dir
        self._pw = None
        self._browser = None
        self._pages: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self._counter = 0

    async def _ensure(self):
        async with self._lock:
            if self._browser:
                return
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            exe = os.environ.get("CONDUCTOR_CHROMIUM")
            try:
                self._browser = await self._pw.chromium.launch(executable_path=exe) if exe \
                    else await self._pw.chromium.launch()
            except Exception:
                for path in FALLBACK_CHROMIUM:
                    if Path(path).exists():
                        self._browser = await self._pw.chromium.launch(executable_path=path)
                        break
                else:
                    raise

    async def page(self, task_id: str):
        await self._ensure()
        page = self._pages.get(task_id)
        if page is None or page.is_closed():
            ctx = await self._browser.new_context(viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()
            self._pages[task_id] = page
        return page

    async def close_page(self, task_id: str) -> None:
        page = self._pages.pop(task_id, None)
        if page and not page.is_closed():
            await page.context.close()

    async def shutdown(self) -> None:
        for tid in list(self._pages):
            await self.close_page(tid)
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    async def snap(self, task_id: str, page) -> tuple[str, bytes]:
        self._counter += 1
        d = self.shots_dir / task_id
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self._counter:05d}.jpg"
        data = await page.screenshot(type="jpeg", quality=60)
        path.write_bytes(data)
        return str(path), data


def _pool(ctx: ToolContext) -> BrowserPool:
    c = ctx.conductor
    if getattr(c, "browser_pool", None) is None:
        c.browser_pool = BrowserPool(ctx.settings.workspace / ".screens")
    return c.browser_pool


async def browser(ctx: ToolContext, args: dict) -> list[dict] | str:
    action = args["action"]
    pool = _pool(ctx)
    if action == "close":
        await pool.close_page(ctx.task_id)
        return "Browser tab closed."
    page = await pool.page(ctx.task_id)
    assessment = assess_browser(args, page.url)
    if assessment.level != "safe" and not await ctx.approve("browser", _summ(args), args, assessment):
        raise ToolError("That browser action wasn't approved. Ask the user, or find another way.")
    try:
        if action == "goto":
            url = args.get("url") or ""
            if not url:
                raise ToolError("goto needs a url")
            if "://" not in url:
                url = "https://" + url
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        elif action == "click":
            if args.get("selector"):
                await page.click(args["selector"], timeout=10_000)
            elif args.get("text"):
                await page.get_by_text(args["text"], exact=False).first.click(timeout=10_000)
            elif "x" in args and "y" in args:
                await page.mouse.click(args["x"], args["y"])
            else:
                raise ToolError("click needs a selector, text, or x/y")
        elif action == "type":
            if args.get("selector"):
                await page.fill(args["selector"], args.get("text", ""), timeout=10_000)
            else:
                await page.keyboard.type(args.get("text", ""))
        elif action == "press":
            await page.keyboard.press(args.get("text") or "Enter")
        elif action == "scroll":
            await page.mouse.wheel(0, int(args.get("y", 700)))
        elif action == "back":
            await page.go_back(timeout=15_000)
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        await asyncio.sleep(0.6)
    except ToolError:
        raise
    except Exception as e:  # Playwright timeouts, bad selectors...
        raise ToolError(f"Browser {action} failed: {str(e).splitlines()[0][:300]}")

    title, url = await page.title(), page.url
    if action == "read":
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        links = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).slice(0, 60)"
            ".map(a => `${a.innerText.trim().slice(0, 80)} -> ${a.href}`).filter(s => !s.startsWith(' ->'))")
        return clip(f"{title}\n{url}\n\n{text}\n\nLinks:\n" + "\n".join(links), 25_000)

    shot_path, data = await pool.snap(ctx.task_id, page)
    await ctx.emit("screenshot", {
        "path": str(Path(shot_path).relative_to(ctx.settings.workspace)), "url": url, "title": title})
    return [
        {"type": "text", "text": f"Page: {title}\nURL: {url}\n(Use action 'read' for the page text.)"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(data).decode()}},
    ]


def _summ(a: dict) -> str:
    act = a.get("action", "")
    if act == "goto":
        return f"Opened {a.get('url', '')}"
    if act == "click":
        return f"Clicked {a.get('text') or a.get('selector') or 'page'}"
    if act == "type":
        return f"Typed “{(a.get('text') or '')[:40]}”"
    return f"Browser: {act}"


TOOLS = [
    Tool(
        name="browser",
        description=(
            "Drive a real Chromium browser tab (yours for this task). Actions: goto(url), click(selector | "
            "text | x,y), type(text, optional selector to fill), press(text=key, default Enter), scroll(y), "
            "back, read (page text + links), screenshot, close. Visual actions return a screenshot. Prefer "
            "web_search/web_fetch for plain lookups; use the browser for interactive sites, logins the "
            "user set up, forms, and pages that need JavaScript. Before submitting anything that spends "
            "money, sends messages, or changes an account, confirm with ask_user."
        ),
        properties={
            "action": {"type": "string", "enum": ACTIONS},
            "url": {"type": "string"},
            "selector": {"type": "string", "description": "CSS selector"},
            "text": {"type": "string", "description": "Text to type / key to press / visible text to click"},
            "x": {"type": "integer"}, "y": {"type": "integer"},
        },
        required=["action"],
        handler=browser,
        summarize=_summ,
    )
]
