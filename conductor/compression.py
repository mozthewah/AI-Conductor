"""Token savings with Headroom (pip install headroom-ai): shrink bulky tool output before models see it.

How it's used, and why it's cache-safe:
  Conductor's transcript is append-only. Each new batch of tool results is compressed ONCE, when it
  arrives, and the compressed version is what's stored and sent from then on, so earlier turns never
  change and the provider's prompt cache keeps hitting. (Re-compressing history each call is what
  breaks caches; this never does that.)

What gets compressed: large outputs of tools whose exact text the model doesn't need to edit -
shell output, process logs, page text from the browser, file listings. File contents read for editing
are left alone. The full original is saved under .conductor/full/ in the workspace and the model is
told where, so nothing is lost: it can read the file if it needs everything.

Headroom's anonymous usage beacon is turned off unless you set HEADROOM_BEACON yourself.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("conductor.compression")

COMPRESSIBLE = {"run_shell", "processes", "browser", "list_files"}
MIN_CHARS = 2000          # smaller outputs aren't worth it
MIN_SAVED_TOKENS = 200    # only swap in the compressed text when it saves at least this much


class Compressor:
    def __init__(self, mode: str = "auto", workspace: Path | None = None):
        self.mode = mode if mode in ("auto", "on", "off") else "auto"
        self.workspace = Path(workspace) if workspace else None
        self._compress = None
        self._config = None
        self.error = ""
        self.version = ""
        self.saved_total = 0
        if self.mode != "off":
            os.environ.setdefault("HEADROOM_BEACON", "off")  # no anonymous upload unless you opt in
            try:
                import headroom
                from headroom import CompressConfig, compress
                self._compress, self._config = compress, CompressConfig
                self.version = getattr(headroom, "__version__", "")
            except Exception as e:  # not installed, or a broken install: run without it
                self.error = f"{type(e).__name__}: {e}"

    @property
    def available(self) -> bool:
        return self._compress is not None

    def compress_results(self, task_id: str, brief: str, assistant_blocks: list[dict],
                         results: list[dict], model_id: str) -> tuple[list[dict], list[dict]]:
        """Compress the big tool_result blocks in this batch. Returns (results, stats per compressed block)."""
        if not self.available:
            return results, []
        names = {b.get("id"): b.get("name") for b in assistant_blocks if b.get("type") == "tool_use"}
        targets = [i for i, r in enumerate(results)
                   if r.get("type") == "tool_result" and not r.get("is_error")
                   and names.get(r.get("tool_use_id")) in COMPRESSIBLE
                   and isinstance(r.get("content"), str) and len(r["content"]) >= MIN_CHARS]
        if not targets:
            return results, []
        batch = [dict(results[i]) for i in targets]
        messages = [{"role": "user", "content": brief[:4000] or "Continue the task."},
                    {"role": "assistant", "content": [b for b in assistant_blocks if b.get("type") == "tool_use"]},
                    {"role": "user", "content": batch}]
        try:
            res = self._compress(messages, model=_model_name(model_id),
                                 config=self._config(protect_recent=0, compress_user_messages=False))
            new_blocks = res.messages[-1]["content"]
        except Exception as e:
            log.warning("headroom compression failed: %s", e)
            return results, []
        out, stats = list(results), []
        for i, nb in zip(targets, new_blocks):
            before, after = results[i]["content"], nb.get("content")
            if not isinstance(after, str) or len(after) >= len(before):
                continue
            saved = (len(before) - len(after)) // 4  # rough tokens
            if saved < MIN_SAVED_TOKENS:
                continue
            where = self._save_full(task_id, results[i].get("tool_use_id", "x"), before)
            note = (f"\n\n[Shortened by Headroom from about {len(before) // 4:,} to {len(after) // 4:,} tokens. "
                    + (f"Full output: {where} (use read_file if you need all of it).]" if where else "]"))
            out[i] = {**results[i], "content": after + note}
            stats.append({"tool": names.get(results[i].get("tool_use_id")), "chars_before": len(before),
                          "chars_after": len(after), "tokens_saved": saved, "full": where})
            self.saved_total += saved
        return out, stats

    def _save_full(self, task_id: str, tool_use_id: str, text: str) -> str | None:
        if not self.workspace:
            return None
        try:
            d = self.workspace / ".conductor" / "full" / task_id
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{tool_use_id}.txt"
            p.write_text(text)
            return str(p.relative_to(self.workspace))
        except OSError:
            return None


def _model_name(model_id: str) -> str:
    """Headroom picks a tokenizer from the model name; give it the plain one."""
    name = (model_id or "").split("/")[-1].split(":")[0]
    return name or "claude-sonnet-4-5"


def proxy_url(setting: str, timeout: float = 1.0) -> str | None:
    """Headroom's local proxy, if it's running (for routing Claude Code through it)."""
    if setting == "off":
        return None
    url = setting if setting.startswith("http") else "http://127.0.0.1:8787"
    try:
        import urllib.request
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as r:
            return url.rstrip("/") if r.status == 200 else None
    except Exception:
        return None
