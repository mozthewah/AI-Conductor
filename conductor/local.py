"""Your own model servers as providers: Ollama, anything OpenAI-compatible, or anything Anthropic-compatible.

CONDUCTOR_LOCAL_URL takes one or more servers, comma-separated, each optionally prefixed with its API:

    CONDUCTOR_LOCAL_URL=http://127.0.0.1:11434                         # auto-detected (Ollama here)
    CONDUCTOR_LOCAL_URL=ollama=http://127.0.0.1:11434, openai=http://comm-laptop:1234/v1
    CONDUCTOR_LOCAL_URL=anthropic=http://127.0.0.1:18000               # speaks /v1/messages

APIs:
  ollama     Ollama's native /api/chat. Used for Ollama (rather than its /v1 shim) because only the native
             API lets Conductor set the context window (num_ctx); Ollama's default is small enough to
             silently cut off Conductor's instructions and tool list.
  openai     /v1/chat/completions: LM Studio, llama.cpp's llama-server, vLLM, LocalAI, Jan, KoboldCpp, ...
  anthropic  /v1/messages (e.g. an OpenVINO or LiteLLM server that speaks the Anthropic API).
  auto       (default) GET /api/tags answers -> ollama; otherwise GET /v1/models decides openai vs anthropic.

Models are discovered from the server and show up as local/<name> (local/<name>@<host> if two servers
share a name). They're free, so Conductor never checks credits for them. With no cloud keys they are the
whole roster; with cloud keys, add the ones you want on the Models page (or pin one with CONDUCTOR_MODEL).
Requests to them carry only core fields (no prompt caching, hosted web tools or provider routing) and
never your Anthropic/OpenRouter credentials: only CONDUCTOR_LOCAL_KEY, if you set one.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .models import ModelInfo

log = logging.getLogger("conductor.local")

APIS = ("auto", "ollama", "openai", "anthropic")
DEFAULT_CONTEXT = 32_768     # what Conductor asks Ollama for when you don't say (fits Conductor's prompt + tools)
PROBE_TIMEOUT = 4
MAX_MODELS = 60
# Ports the common local servers use, for "find my server" on the Setup page.
COMMON = [("ollama", "http://127.0.0.1:11434"), ("openai", "http://127.0.0.1:1234"),  # Ollama, LM Studio
          ("openai", "http://127.0.0.1:8080"), ("openai", "http://127.0.0.1:8000")]  # llama.cpp, vLLM
NOT_CHAT = re.compile(r"embed|rerank|whisper|tts|bge-|nomic-embed|clip", re.I)


@dataclass
class Endpoint:
    api: str      # auto | ollama | openai | anthropic (auto is resolved by discover())
    root: str     # scheme://host:port[/prefix], without /v1
    key: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.root).netloc or self.root

    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "User-Agent": "ai-conductor"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
            h["x-api-key"] = self.key
        return h


def normalize(url: str) -> str:
    """'http://host:1234/v1/chat/completions' -> 'http://host:1234' (the API paths are added per request)."""
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    for suffix in ("/chat/completions", "/completions", "/messages", "/api/chat", "/api/tags", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    elif url.endswith("/api"):
        url = url[:-4]
    return url.rstrip("/")


def parse_endpoints(setting: str, default_api: str = "auto", key: str = "") -> list[Endpoint]:
    out, seen = [], set()
    for part in re.split(r"[,\s]+", setting or ""):
        if not part:
            continue
        api = default_api if default_api in APIS else "auto"
        m = re.match(r"^(auto|ollama|openai|anthropic)=(.+)$", part, re.I)
        if m:
            api, part = m.group(1).lower(), m.group(2)
        root = normalize(part)
        if root and root not in seen:
            seen.add(root)
            out.append(Endpoint(api, root, key))
    return out


# ------------------------------------------------------------------ discovery
def _get(url: str, ep: Endpoint, body: dict | None = None, timeout: float = PROBE_TIMEOUT) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=ep.headers(), method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def _ollama_models(ep: Endpoint, context: int) -> list[dict]:
    tags = _get(f"{ep.root}/api/tags", ep)
    out = []
    for t in (tags or {}).get("models") or []:
        name = t.get("model") or t.get("name")
        if not name or NOT_CHAT.search(name):
            continue
        caps, ctx, size = set(), 0, (t.get("details") or {}).get("parameter_size", "")
        try:
            show = _get(f"{ep.root}/api/show", ep, {"model": name})
            caps = set(show.get("capabilities") or [])
            ctx = next((int(v) for k, v in (show.get("model_info") or {}).items()
                        if k.endswith(".context_length") and isinstance(v, (int, float))), 0)
        except Exception as e:  # old Ollama or a slow disk: assume a tool-capable text model
            log.info("couldn't read details for %s (%s)", name, e)
        if caps and "completion" not in caps:
            continue  # embedding-only
        window = context or min(ctx or DEFAULT_CONTEXT, DEFAULT_CONTEXT)
        out.append({"name": name, "context": window, "tools": not caps or "tools" in caps,
                    "vision": "vision" in caps, "reasoning": "thinking" in caps,
                    "notes": f"Ollama{', ' + size if size else ''}" + ("" if not caps or "tools" in caps else
                                                                      "; this model can't call tools")})
    return out


def _v1_models(ep: Endpoint, context: int) -> tuple[str, list[dict]]:
    """GET /v1/models. Anthropic-style lists say type: model / display_name; OpenAI-style say object: model."""
    data = _get(f"{ep.root}/v1/models", ep)
    entries = (data or {}).get("data") if isinstance(data, dict) else data
    entries = [e for e in entries or [] if isinstance(e, dict) and e.get("id")]
    api = ep.api
    if api == "auto":
        anthropicish = entries and all(e.get("type") == "model" and "object" not in e for e in entries)
        api = "anthropic" if anthropicish else "openai"
    out = []
    for e in entries:
        if NOT_CHAT.search(e["id"]) or e.get("type") in ("embeddings", "embedding"):
            continue
        meta = e.get("meta") or {}
        ctx = (e.get("context_length") or e.get("max_model_len") or e.get("max_context_length")
               or meta.get("n_ctx") or meta.get("n_ctx_train") or 0)
        out.append({"name": e["id"], "context": context or int(ctx or DEFAULT_CONTEXT), "tools": True,
                    "vision": bool(e.get("vision") or "vision" in (e.get("capabilities") or [])),
                    "reasoning": False, "notes": api})
    return api, out


def discover(ep: Endpoint, context: int = 0, names: list[str] | None = None) -> tuple[str, list[dict], str]:
    """(resolved api, models, error). Falls back to `names` when the server can't list its models."""
    api, found, err = ep.api, [], ""
    if ep.api in ("auto", "ollama"):
        try:
            found, api = _ollama_models(ep, context), "ollama"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    if api not in ("ollama",):  # auto (not Ollama), openai or anthropic
        try:
            api, found = _v1_models(ep, context)
            err = ""
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            api = "openai" if api == "auto" else api
    wanted = [n.strip() for n in names or [] if n.strip()]
    if wanted:
        found = [m for m in found if m["name"] in wanted] or [
            {"name": n, "context": context or DEFAULT_CONTEXT, "tools": True, "vision": False, "reasoning": False,
             "notes": f"{api} (from CONDUCTOR_LOCAL_MODELS)"} for n in wanted]
    return api, found[:MAX_MODELS], err


def to_models(eps: list[Endpoint], context: int = 0, names: list[str] | None = None) -> tuple[dict[str, ModelInfo], list[dict]]:
    """All models from all servers, plus a status line per server (for doctor and the Models page)."""
    models: dict[str, ModelInfo] = {}
    status = []
    for ep in eps:
        api, found, err = discover(ep, context, names)
        ep.api = api
        status.append({"url": ep.root, "api": api, "models": [m["name"] for m in found], "error": err})
        for m in found:
            mid = f"local/{m['name']}"
            if mid in models:
                mid = f"{mid}@{ep.host}"
            ctx = int(m["context"])
            models[mid] = ModelInfo(
                mid, "local", f"{m['name']} (local)", 0.0, 0.0, ctx, max(1024, min(16_384, ctx // 2)),
                tools=m["tools"], vision=m["vision"], reasoning=m["reasoning"], structured=False, tier="fast",
                notes=m["notes"], base_url=ep.root, api=api, remote_id=m["name"], api_key=ep.key)
    return models, status


def probe(url: str, api: str = "auto", key: str = "") -> dict:
    """Setup page: does this URL answer, what does it speak, which models does it have."""
    eps = parse_endpoints(url, api, key)
    if not eps:
        return {"ok": False, "error": "No URL given."}
    api, found, err = discover(eps[0])
    return {"ok": bool(found), "url": eps[0].root, "api": api, "models": [m["name"] for m in found],
            "error": err if not found else ""}


def find_running() -> list[dict]:
    """Local servers answering on their usual ports (nothing is changed; read-only requests)."""
    out = []
    for api, url in COMMON:
        r = probe(url, api)
        if r["ok"]:
            out.append(r)
    return out


# ------------------------------------------------------------------ message conversion
def _image_url(block: dict) -> str | None:
    src = block.get("source") or {}
    if src.get("type") == "base64":
        return f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
    if src.get("type") == "url":
        return src.get("url")
    return None


def _result_text(content) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") == "image" and (u := _image_url(b)):
            images.append(u)
    return "\n".join(texts), images


def to_openai(system: str, messages: list[dict], vision: bool) -> list[dict]:
    """Conductor's (Anthropic-shaped) transcript -> OpenAI chat messages."""
    from .llm import _foreign_turn

    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            blocks = _foreign_turn(content)  # drop reasoning; hosted-tool blocks become text
            text = "\n\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            calls = [{"id": b.get("id") or f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                      "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": text or ("" if calls else "(continuing)")}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        parts: list[dict] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "tool_result":
                text, images = _result_text(b.get("content"))
                if b.get("is_error"):
                    text = f"Error: {text}"
                out.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""), "content": text or "(no output)"})
                parts += [{"type": "image_url", "image_url": {"url": u}} for u in images] if vision else []
            elif t == "text" and b.get("text"):
                parts.append({"type": "text", "text": b["text"]})
            elif t == "image":
                u = _image_url(b)
                parts.append({"type": "image_url", "image_url": {"url": u}} if vision and u
                             else {"type": "text", "text": "[image omitted: this model can't see images]"})
            elif t == "document":
                parts.append({"type": "text", "text": f"[document: {b.get('title') or 'attached'}]"})
        if parts:
            if all(p["type"] == "text" for p in parts):
                out.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
            else:
                out.append({"role": "user", "content": parts})
    return out


def to_ollama(openai_msgs: list[dict]) -> list[dict]:
    """OpenAI chat messages -> Ollama /api/chat (images as base64 lists, tool args as objects, tool_name)."""
    names: dict[str, str] = {}
    out = []
    for m in openai_msgs:
        msg: dict[str, Any] = {"role": m["role"]}
        content = m.get("content")
        if isinstance(content, list):
            msg["content"] = "\n\n".join(p["text"] for p in content if p.get("type") == "text")
            imgs = [p["image_url"]["url"].split(",", 1)[-1] for p in content if p.get("type") == "image_url"
                    and p["image_url"]["url"].startswith("data:")]
            if imgs:
                msg["images"] = imgs
        else:
            msg["content"] = content or ""
        if m.get("tool_calls"):
            msg["tool_calls"] = []
            for c in m["tool_calls"]:
                names[c["id"]] = c["function"]["name"]
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                msg["tool_calls"].append({"function": {"name": c["function"]["name"], "arguments": args}})
        if m["role"] == "tool":
            msg["tool_name"] = names.get(m.get("tool_call_id", ""), "")
        out.append(msg)
    return out


def openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in tools if t.get("name") and "input_schema" in t]


def parse_args(raw) -> dict:
    """Tool arguments: an object, a JSON string, or (from small models) almost-JSON."""
    if isinstance(raw, dict):
        return raw
    if not raw or not str(raw).strip():
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except ValueError:
        from .llm import parse_json_loose
        val = parse_json_loose(str(raw))
        if val:
            return val
        raise


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
