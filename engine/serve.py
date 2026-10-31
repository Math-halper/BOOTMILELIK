#!/usr/bin/env python3
"""Boot Milelik's remade local engine (no Ollama install needed).

This starts the bundled CPU ``llama-server`` on 127.0.0.1:8080 and an
Ollama-compatible front on 127.0.0.1:11434, so Milelik's default backend
works with zero configuration::

    python3 engine/serve.py
    milelik "check my disk space"        # talks to the engine like Ollama

Verify everything end to end (boots on alternate ports, runs one chat,
shuts down)::

    python3 engine/serve.py --check

Use ``--llama-only`` to expose just the OpenAI-style API on :8080
(then run Milelik with ``--backend openai``). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import ollama_compat
except ImportError:  # allow running as `python3 engine/serve.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ollama_compat

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BIN = HERE / "llama-server"
DEFAULT_MODEL = ROOT / "models" / "smollm2-135m-instruct-q4_1.gguf"

LLAMA_HOST = "127.0.0.1"
API_HOST = "127.0.0.1"


class EngineError(ValueError):
    """The bundled engine cannot start on this machine."""


def find_model(explicit: str = "") -> Path:
    """Resolve the GGUF to serve: explicit path or the bundled default."""
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate
        raise EngineError(f"Model file not found: {explicit}")
    if DEFAULT_MODEL.is_file():
        return DEFAULT_MODEL
    raise EngineError(
        f"Bundled model not found: {DEFAULT_MODEL}. Pass --model /path/to/model.gguf.")


def check_binary() -> Path:
    """Return the engine binary or raise with a rebuild pointer."""
    if BIN.is_file():
        return BIN
    raise EngineError(
        f"Engine binary missing: {BIN}. Rebuild it with the steps in engine/README.md.")


def wait_for_health(llama_base: str, timeout: float = 180.0) -> bool:
    """Poll llama-server /health until it answers 200 (model loaded)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(llama_base + "/health", timeout=5) as resp:
                if 200 <= int(resp.status) < 300:
                    return True
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(1.0)
    return False


def build_llama_command(model: Path, port: int, ctx: int) -> list[str]:
    """llama-server command line for a local GGUF file."""
    return [str(check_binary()), "-m", str(model),
            "--host", LLAMA_HOST, "--port", str(port),
            "-c", str(ctx), "--jinja"]


def start_llama_server(model: Path, port: int, ctx: int,
                       verbose: bool = False) -> subprocess.Popen:
    """Spawn llama-server; its logs stream to this terminal."""
    command = build_llama_command(model, port, ctx)
    print(f"[engine] starting: {' '.join(command)}", flush=True)
    try:
        return subprocess.Popen(
            command,
            stdout=None if verbose else subprocess.DEVNULL,
            stderr=None)
    except OSError as error:
        raise EngineError(f"Could not start llama-server: {error}")


def stop_process(process: subprocess.Popen, timeout: float = 10.0) -> None:
    """Terminate a child process, escalating to kill when needed."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def post_chat(api_base: str, model: str, text: str, timeout: int = 300) -> dict:
    """One Ollama-style chat turn against the compat front (for --check)."""
    payload = {"model": model, "stream": False,
               "messages": [{"role": "user", "content": text}]}
    request = urllib.request.Request(
        api_base + "/api/chat", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def run_check(model: Path, llama_port: int, api_port: int, ctx: int) -> int:
    """Boot the engine on alternate ports, run one chat, shut down."""
    print(f"[engine] check: model={model}", flush=True)
    server = start_llama_server(model, llama_port, ctx)
    try:
        llama_base = f"http://{LLAMA_HOST}:{llama_port}"
        print("[engine] check: waiting for model load…", flush=True)
        if not wait_for_health(llama_base):
            print("[engine] check FAILED: llama-server never became healthy.",
                  file=sys.stderr)
            return 1
        compat = ollama_compat.CompatServer(
            API_HOST, api_port, llama_base, model)
        thread = compat.serve_in_background()
        try:
            api_base = f"http://{API_HOST}:{api_port}"
            with urllib.request.urlopen(api_base + "/api/tags", timeout=10) as resp:
                tags = json.loads(resp.read().decode("utf-8", errors="replace"))
            names = [m.get("name", "") for m in tags.get("models", [])]
            print(f"[engine] check: /api/tags -> {names}", flush=True)
            if not names:
                print("[engine] check FAILED: no models advertised.",
                      file=sys.stderr)
                return 1
            reply = post_chat(api_base, names[0], "Reply with exactly: OK")
            content = ((reply.get("message") or {}).get("content") or "").strip()
            print(f"[engine] check: chat reply ({len(content)} chars): "
                  f"{content[:120]!r}", flush=True)
            if not content:
                print("[engine] check FAILED: empty chat reply.", file=sys.stderr)
                return 1
        finally:
            compat.shutdown()
            thread.join(timeout=10)
    finally:
        stop_process(server)
    print("[engine] check PASSED: tags + chat work end to end.", flush=True)
    return 0


def run_forever(model: Path, llama_port: int, api_port: int, ctx: int,
                llama_only: bool, verbose: bool = False) -> int:
    """Boot llama-server and (unless --llama-only) the Ollama front."""
    server = start_llama_server(model, llama_port, ctx, verbose=verbose)
    try:
        llama_base = f"http://{LLAMA_HOST}:{llama_port}"
        print("[engine] waiting for model load…", flush=True)
        if not wait_for_health(llama_base):
            print("[engine] llama-server never became healthy; see its log above.",
                  file=sys.stderr)
            return 1
        print(f"[engine] llama-server ready at {llama_base} "
              f"(model: {model.name})", flush=True)
        if llama_only:
            print("[engine] OpenAI-style API only; run Milelik with "
                  "`--backend openai`. Ctrl+C to stop.", flush=True)
            server.wait()
            return 0 if server.returncode == 0 else 1
        try:
            compat = ollama_compat.CompatServer(API_HOST, api_port, llama_base,
                                                model)
        except OSError as error:
            print(f"[engine] cannot listen on {API_HOST}:{api_port} ({error}). "
                  f"Is `ollama serve` already running? Use --api-port or stop it.",
                  file=sys.stderr)
            return 1
        print(f"[engine] Ollama-compatible API at http://{API_HOST}:{api_port} "
              f"(models: {', '.join(compat.httpd.engine['names'])}). "
              "Ctrl+C to stop.", flush=True)
        try:
            compat.serve_forever()
        except KeyboardInterrupt:
            print("\n[engine] stopping…", flush=True)
        finally:
            compat.shutdown()
        return 0
    finally:
        stop_process(server)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Boot Milelik's bundled CPU engine: llama-server plus an "
                    "Ollama-compatible front (no Ollama install needed).")
    parser.add_argument("--model", default="",
                        help="GGUF file to serve (default: bundled SmolLM2)")
    parser.add_argument("--llama-port", type=int, default=8080,
                        help="llama-server port (default: 8080)")
    parser.add_argument("--api-port", type=int, default=11434,
                        help="Ollama-compatible API port (default: 11434)")
    parser.add_argument("--ctx", type=int, default=8192,
                        help="Context size in tokens (default: 8192)")
    parser.add_argument("--llama-only", action="store_true",
                        help="Expose only the OpenAI-style API on --llama-port")
    parser.add_argument("--check", action="store_true",
                        help="Boot on ports 18080/11435, run one chat, shut down")
    parser.add_argument("--verbose", action="store_true",
                        help="Show llama-server request logs too")
    return parser


def main(argv: list[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    try:
        model = find_model(options.model)
        check_binary()
    except EngineError as error:
        print(f"[engine] {error}", file=sys.stderr)
        return 1
    if options.check:
        llama_port = options.llama_port if options.llama_port != 8080 else 18080
        api_port = options.api_port if options.api_port != 11434 else 11435
        return run_check(model, llama_port, api_port, options.ctx)
    return run_forever(model, options.llama_port, options.api_port,
                       options.ctx, options.llama_only, options.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
