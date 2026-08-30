"""Client for the local Qwen llama-server (OpenAI-compatible API)."""
from __future__ import annotations

import json
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:8080"
MODEL = "Qwen3.8-27B-Q4_K_M"


def chat(messages: list[dict[str, str]], *, max_tokens: int = 4000, temperature: float = 0.2,
         timeout: float = 300.0) -> str:
    """Chat completion; returns content (reasoning is separate, discarded)."""
    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    msg = data["choices"][0]["message"]
    return (msg.get("content") or "").strip()


def chat_json(messages: list[dict[str, str]], *, max_tokens: int = 4000, retries: int = 2) -> Any:
    """Chat completion expected to return JSON; extracts first {...} or [...] block."""
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            text = chat(messages, max_tokens=max_tokens)
            start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
            if start < 0:
                raise ValueError(f"no JSON in response: {text[:200]}")
            # find matching close by scanning
            depth = 0
            open_ch = text[start]
            close_ch = "}" if open_ch == "{" else "]"
            for i in range(start, len(text)):
                if text[i] == open_ch:
                    depth += 1
                elif text[i] == close_ch:
                    depth -= 1
                    if depth == 0:
                        return json.loads(text[start:i + 1])
            raise ValueError(f"unbalanced JSON: {text[:200]}")
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"chat_json failed: {last}")


def health() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=5) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False
