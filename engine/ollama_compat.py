#!/usr/bin/env python3
"""Ollama-compatible HTTP front for Milelik's remade local engine.

Milelik's default backend speaks Ollama's API (``GET /api/tags`` and
``POST /api/chat`` with ``stream: false``). This stdlib-only module
translates those two calls to the bundled CPU ``llama-server``
(OpenAI-style ``/v1/chat/completions``), so Milelik works with zero
configuration and no Ollama install: it just sees a server on
127.0.0.1:11434 that answers like Ollama.

Only the surface Milelik uses is implemented. Anything else returns a
JSON ``{"error": ...}`` with a suitable status code.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SHIM_VERSION = "0.1.0"

# Model names mirror milelik.py so /api/tags detection matches exactly.
BUNDLED_OLLAMA_NAME = "milelik-smollm2:135m"
LOCAL_IMPORTED_MODEL = "milelik-coder:7b"


def ollama_name_for_model_file(path: str | Path) -> str:
    """Stable Ollama-style name for a GGUF file (mirrors milelik.py)."""
    name = Path(path).name.lower()
    if "smollm2-135m" in name:
        return BUNDLED_OLLAMA_NAME
    if "qwen2.5-coder" in name or "qwen2_5-coder" in name:
        return LOCAL_IMPORTED_MODEL
    stem = Path(path).stem
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:56].strip("-")
    return f"{slug or 'model'}:latest"


def advertised_names(model_file: str | Path) -> list[str]:
    """Names /api/tags reports for the loaded model file."""
    primary = ollama_name_for_model_file(model_file)
    names = [primary]
    base = primary.split(":")[0]
    if f"{base}:latest" not in names:
        names.append(f"{base}:latest")
    return names


def tags_response(names: list[str]) -> dict:
    """Ollama /api/tags body. Milelik only reads each entry's ``name``."""
    return {"models": [
        {"name": name, "model": name, "digest": "",
         "size": 0, "details": {"format": "gguf"}} for name in names]}


def translate_messages(messages: list | None) -> list[dict]:
    """Ollama chat history -> OpenAI chat history for llama-server.

    ``tool`` turns become ``user`` turns with their origin preserved in
    text, because GGUF chat templates only guarantee the classic roles.
    Any native ``tool_calls`` are likewise folded into text.
    """
    out: list[dict] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "user")
        content = item.get("content") or ""
        calls = item.get("tool_calls") or []
        if calls:
            rendered = []
            for call in calls:
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                rendered.append(
                    f"{fn.get('name', '?')}({json.dumps(fn.get('arguments', {}))})")
            content = (content + "\n[previous tool calls: "
                       + "; ".join(rendered) + "]").strip()
        if role == "tool":
            tool_name = item.get("tool_name") or item.get("name") or "tool"
            content = f"[tool result from {tool_name}]\n{content}".strip()
            role = "user"
        if role not in ("system", "user", "assistant"):
            role = "user"
        out.append({"role": role, "content": content})
    return out


def translate_chat_request(payload: dict) -> dict:
    """Ollama /api/chat body -> llama-server /v1/chat/completions body."""
    options = payload.get("options") or {}
    body: dict = {
        "model": payload.get("model", "engine"),
        "messages": translate_messages(payload.get("messages")),
        "stream": False,
    }
    for key in ("temperature", "top_p", "top_k", "repeat_penalty", "seed"):
        if options.get(key) is not None:
            body[key] = options[key]
    if options.get("num_predict") is not None:
        body["max_tokens"] = options["num_predict"]
    if payload.get("tools") is not None:
        body["tools"] = payload["tools"]
    return body


def translate_chat_response(data: dict, model: str) -> dict:
    """llama-server chat reply -> Ollama /api/chat reply (``stream: false``)."""
    choices = data.get("choices") or [{}]
    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content") or ""
    finish = first.get("finish_reason") or "stop"
    return {"model": model,
            "message": {"role": "assistant", "content": content},
            "done": True,
            "done_reason": "length" if finish == "length" else "stop"}


def post_json(url: str, payload: dict, timeout: int = 300) -> dict:
    """POST JSON and decode the JSON reply; raises RuntimeError on failure."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", errors="replace")
        except OSError:
            detail = error.reason
        raise RuntimeError(f"engine HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"engine request failed: {error}") from error


class CompatHandler(BaseHTTPRequestHandler):
    """Handles /api/tags and /api/chat; everything else is a JSON error."""

    server_version = f"MilelikEngine/{SHIM_VERSION}"

    def log_message(self, _format: str, *args) -> None:  # quieter stdlib logging
        import sys
        sys.stderr.write(f"[ollama-compat] {self.address_string()} "
                         f"{self.command} {self.path}\n")

    def _send_json(self, obj: dict, status: int = 200) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 50 * 1024 * 1024:
            raise ValueError("missing or unreasonable Content-Length")
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler naming)
        engine = self.server.engine  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/tags":
            self._send_json(tags_response(engine["names"]))
        elif path == "/api/version":
            self._send_json({"version": f"milelik-engine-{SHIM_VERSION}"})
        elif path in ("", "/"):
            self._send_json({"engine": "milelik-engine", "shim": SHIM_VERSION,
                             "models": engine["names"],
                             "llama_server": engine["llama_base"]})
        else:
            self._send_json({"error": f"unsupported path: {self.path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler naming)
        engine = self.server.engine  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        if path != "/api/chat":
            self._send_json({"error": f"unsupported path: {self.path}"}, 404)
            return
        try:
            payload = self._read_json()
        except ValueError as error:
            self._send_json({"error": f"invalid request: {error}"}, 400)
            return
        try:
            reply = post_json(engine["llama_base"] + "/v1/chat/completions",
                              translate_chat_request(payload))
            self._send_json(translate_chat_response(
                reply, payload.get("model", engine["names"][0])))
        except RuntimeError as error:
            self._send_json({"error": str(error)}, 500)


class CompatServer:
    """Threaded Ollama-compatible front pointing at a llama-server."""

    def __init__(self, host: str, port: int, llama_base: str,
                 model_file: str | Path) -> None:
        self.httpd = ThreadingHTTPServer((host, port), CompatHandler)
        self.httpd.daemon_threads = True
        self.httpd.engine = {"llama_base": llama_base.rstrip("/"),
                             "model_file": str(model_file),
                             "names": advertised_names(model_file)}

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def serve_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread
