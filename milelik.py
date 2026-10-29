#!/usr/bin/env python3
"""
Milelik - a fast, local AI assistant for linux Mint.

It talks to a *local* model served by Ollama and gives that model a small,
useful set of tools:

  * run shell commands (with your confirmation by default)
  * read / write / list files
  * fetch web pages and search the web (DuckDuckGo, no API key needed)
  * DNS lookups / TCP port checks

No cloud API, no API keys, no pip dependencies - Python 3.10+ standard
library only, which is what linux Mint ships with.

Quick start:
    ./install.sh                 # installs Ollama + a model + `milelik`
    milelik "find the 10 biggest folders in my home directory"
    milelik --interactive
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

__version__ = "1.0.0"

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_CTX = 8192
DEFAULT_MAX_STEPS = 12
DEFAULT_CMD_TIMEOUT = 60
MAX_TOOL_OUTPUT = 8000          # chars of tool output fed back to the model
MAX_DOWNLOAD_BYTES = 2_000_000  # cap web downloads at ~2 MB
USER_AGENT = "Milelik/1.0 (+https://github.com/Math-halper/BOOTMILELIK)"

# ---------------------------------------------------------------------------
# Terminal colours (disabled automatically when output is not a TTY)
# ---------------------------------------------------------------------------

class C:
    enabled = False
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"

    @classmethod
    def wrap(cls, code: str, text: str) -> str:
        return f"{code}{text}{cls.RESET}" if cls.enabled else text

def c_dim(t):    return C.wrap(C.DIM, t)
def c_bold(t):   return C.wrap(C.BOLD, t)
def c_green(t):  return C.wrap(C.GREEN, t)
def c_yellow(t): return C.wrap(C.YELLOW, t)
def c_red(t):    return C.wrap(C.RED, t)
def c_cyan(t):   return C.wrap(C.CYAN, t)

def info(msg=""):
    print(msg)

def warn(msg):
    print(c_yellow(f"⚠ {msg}"), file=sys.stderr)

def err(msg):
    print(c_red(f"✖ {msg}"), file=sys.stderr)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    """Keep the head and tail of long text, drop the middle."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n\n... [{len(text) - limit} characters truncated] ...\n\n{text[-tail:]}"

def one_line(text: str, limit: int = 140) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"

def distro_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()

def normalise_host(host: str) -> str:
    host = (host or DEFAULT_HOST).strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host

# ---------------------------------------------------------------------------
# Safety: dangerous-command detection
# ---------------------------------------------------------------------------
# These are never auto-approved, even with --yolo. The user must type YES.

DANGEROUS_PATTERNS = [
    (r"\brm\b[^|;&]*-[a-zA-Z]*r[a-zA-Z]*[^|;&]*\s+/(?:\s|$|\*)", "recursive delete of the root filesystem"),
    (r"\brm\b[^|;&]*-[a-zA-Z]*r[a-zA-Z]*[^|;&]*\s+(~|\$HOME)(?:\s|$|/\*)", "recursive delete of your home directory"),
    (r"\bmkfs(\.[a-z0-9]+)?\b", "formatting a filesystem"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "raw write to a disk device"),
    (r">\s*/dev/(sd[a-z]|nvme\d|hd[a-z]|vd[a-z])", "overwriting a disk device"),
    (r"\b(shutdown|poweroff|halt)\b", "shutting down the machine"),
    (r"\breboot\b", "rebooting the machine"),
    (r":\(\)\s*\{", "a fork bomb"),
    (r"\bchmod\b[^|;&]*\b-R\b[^|;&]*\b777\b[^|;&]*\s+/", "recursively making the whole system world-writable"),
    (r"\bchown\b[^|;&]*\b-R\b[^|;&]*\s+/\s*$", "recursively changing ownership of the whole system"),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", "piping a downloaded script straight into a shell"),
    (r"\bapt(-get)?\b[^|;&]*\b(purge|autoremove)\b[^|;&]*--purge\b", "purging system packages"),
]

_COMPILED_DANGER = [(re.compile(p, re.I), why) for p, why in DANGEROUS_PATTERNS]

def check_dangerous(command: str):
    """Return a human-readable reason if the command looks dangerous."""
    for rx, why in _COMPILED_DANGER:
        if rx.search(command or ""):
            return why
    return None

def uses_sudo(command: str) -> bool:
    return bool(re.search(r"(^|[|;&]\s*)sudo\b", command or ""))

# ---------------------------------------------------------------------------
# HTML -> plain text, and DuckDuckGo result parsing (stdlib only)
# ---------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        out = []
        for line in "".join(self.parts).splitlines():
            line = " ".join(line.split())
            if line:
                out.append(line)
        return "\n".join(out)

def html_to_text(markup: str) -> str:
    p = TextExtractor()
    try:
        p.feed(markup)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup)
    return p.text()

class DDGParser(HTMLParser):
    """Parse DuckDuckGo's html endpoint (result__a / result__snippet)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._mode = None        # "title" | "snippet"
        self._href = ""
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        a = dict(attrs)
        cls = a.get("class", "")
        if "result__a" in cls:
            self._mode, self._href, self._buf = "title", a.get("href", ""), []
        elif "result__snippet" in cls:
            self._mode, self._buf = "snippet", []

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self._mode:
            return
        text = " ".join("".join(self._buf).split())
        if self._mode == "title" and text:
            self.results.append({"title": text, "url": clean_ddg_url(self._href), "snippet": ""})
        elif self._mode == "snippet" and self.results and text:
            self.results[-1]["snippet"] = text
        self._mode = None

def clean_ddg_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if "uddg" in q:
            return urllib.parse.unquote(q["uddg"][0])
    except Exception:
        pass
    return href

def parse_search_results(markup: str, max_results: int = 5):
    p = DDGParser()
    try:
        p.feed(markup)
        p.close()
    except Exception:
        return []
    return [r for r in p.results if r["url"]][:max_results]

# ---------------------------------------------------------------------------
# Tools available to the model
# ---------------------------------------------------------------------------

class Toolbox:
    def __init__(self, auto: bool = False, verbose: bool = False,
                 cwd: str | None = None, cmd_timeout: int = DEFAULT_CMD_TIMEOUT):
        self.auto = auto                # --yolo: skip normal confirmations
        self.verbose = verbose
        self.cwd = cwd or os.getcwd()
        self.cmd_timeout = cmd_timeout

    # -- confirmation ------------------------------------------------------
    def confirm(self, action: str, detail: str = "",
                dangerous_reason: str | None = None, sudo: bool = False,
                from_user: bool = False) -> bool:
        """
        Dangerous actions and sudo are *never* auto-approved.
        Direct user input (!command) skips the ordinary prompt.
        Anything confirmed needs an interactive terminal.
        """
        if dangerous_reason:
            print(c_red(f"\n⛔ DANGEROUS — this looks like {dangerous_reason}:"))
            if detail:
                print(f"   {detail}")
            if not sys.stdin.isatty():
                warn("Blocked: dangerous action in non-interactive mode.")
                return False
            try:
                ans = input(c_red("Type YES to allow it: ")).strip()
            except (EOFError, KeyboardInterrupt):
                return False
            return ans == "YES"

        if from_user:
            return True
        if self.auto and not sudo:
            return True
        if not sys.stdin.isatty():
            warn(f"Skipped (needs confirmation but no terminal): {action}")
            return False
        label = f"{c_yellow('[sudo]')} " if sudo else ""
        suffix = f"\n   {detail}" if detail else ""
        try:
            ans = input(f"{label}Allow {action}?{suffix}\n   [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")

    def _resolve(self, path: str) -> Path:
        p = Path(os.path.expanduser(path or "."))
        return p if p.is_absolute() else (Path(self.cwd) / p)

    # -- shell -------------------------------------------------------------
    def run_command(self, command: str, timeout: int | None = None,
                    from_user: bool = False) -> str:
        command = (command or "").strip()
        if not command:
            return "Error: empty command."
        try:
            timeout = max(1, min(int(timeout or self.cmd_timeout), 600))
        except (TypeError, ValueError):
            timeout = self.cmd_timeout

        danger = check_dangerous(command)
        sudo = uses_sudo(command)
        if not self.confirm("run command", f"$ {command}", danger, sudo, from_user):
            return "User declined to run this command. Do not retry it unchanged; suggest an alternative or move on."

        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=self.cwd, capture_output=True, text=True,
                timeout=timeout, errors="replace",
            )
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else ""
            return truncate(f"Command timed out after {timeout}s.\nPartial output:\n{out}")
        except OSError as e:
            return f"Error running command: {e}"

        parts = [f"exit_code: {proc.returncode}"]
        if proc.stdout.strip():
            parts.append(f"stdout:\n{proc.stdout.strip()}")
        if proc.stderr.strip():
            parts.append(f"stderr:\n{proc.stderr.strip()}")
        if not proc.stdout.strip() and not proc.stderr.strip():
            parts.append("(no output)")
        return truncate("\n".join(parts))

    # -- web ---------------------------------------------------------------
    def _http_get(self, url: str, timeout: int = 20):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,text/plain,*/*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_DOWNLOAD_BYTES)
            return resp.status, resp.geturl(), resp.headers.get_content_type(), \
                resp.headers.get_content_charset() or "utf-8", body

    def fetch_url(self, url: str, max_chars: int = 8000) -> str:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            status, final_url, ctype, charset, body = self._http_get(url)
        except urllib.error.HTTPError as e:
            return f"HTTP {e.code} {e.reason} fetching {url}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return f"Error fetching {url}: {getattr(e, 'reason', e)}"

        header = f"HTTP {status} {final_url} ({ctype})"
        if ctype and not (ctype.startswith("text/") or "json" in ctype or "xml" in ctype):
            return f"{header}\nBinary/non-text content ({len(body)} bytes) - content not shown."
        text = body.decode(charset, errors="replace")
        if ctype == "text/html" or "<html" in text[:500].lower():
            text = html_to_text(text)
        try:
            max_chars = max(500, min(int(max_chars or 8000), 20000))
        except (TypeError, ValueError):
            max_chars = 8000
        return truncate(f"{header}\n\n{text.strip()}", max_chars + 200)

    def web_search(self, query: str, max_results: int = 5) -> str:
        query = (query or "").strip()
        if not query:
            return "Error: empty search query."
        try:
            max_results = max(1, min(int(max_results or 5), 10))
        except (TypeError, ValueError):
            max_results = 5
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        try:
            _, _, _, charset, body = self._http_get(url)
        except Exception as e:
            return f"Search failed ({getattr(e, 'reason', e)}). Try fetch_url on a known site instead."
        results = parse_search_results(body.decode(charset, errors="replace"), max_results)
        if not results:
            return f"No results found for: {query!r}. DuckDuckGo may be rate-limiting; try again or fetch a specific URL."
        lines = [f"Search results for: {query}"]
        for i, r in enumerate(results, 1):
            lines.append(f"\n{i}. {r['title']}\n   {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
        return truncate("\n".join(lines))

    # -- files -------------------------------------------------------------
    def read_file(self, path: str, max_chars: int = 8000) -> str:
        p = self._resolve(path)
        try:
            if not p.exists():
                return f"Error: no such file: {p}"
            if p.is_dir():
                return f"Error: {p} is a directory - use list_dir."
            raw = p.read_bytes()[:1_000_000]
        except OSError as e:
            return f"Error reading {p}: {e}"
        if b"\x00" in raw[:4096]:
            return f"{p} appears to be a binary file ({p.stat().st_size} bytes) - content not shown."
        return truncate(f"{p}:\n\n{raw.decode('utf-8', errors='replace')}",
                        max(500, int(max_chars or 8000)))

    def write_file(self, path: str, content: str, overwrite: bool = False) -> str:
        p = self._resolve(path)
        exists = p.exists()
        if exists and not overwrite:
            return f"Error: {p} already exists. Call again with overwrite=true to replace it."
        action = "overwrite file" if exists else "write file"
        detail = f"{p} ({len(content or '')} characters)"
        if not self.confirm(action, detail):
            return "User declined to write this file."
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content or "", encoding="utf-8")
        except OSError as e:
            return f"Error writing {p}: {e}"
        return f"Wrote {len(content or '')} characters to {p}."

    def list_dir(self, path: str = ".") -> str:
        p = self._resolve(path)
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError as e:
            return f"Error listing {p}: {e}"
        if not entries:
            return f"{p} is empty."
        lines = [f"{p}:"]
        for e in entries[:200]:
            try:
                kind = "dir " if e.is_dir() else "file"
                size = "" if e.is_dir() else f"  {e.stat().st_size} bytes"
            except OSError:
                kind, size = "?", ""
            lines.append(f"  [{kind}] {e.name}{size}")
        if len(entries) > 200:
            lines.append(f"  ... and {len(entries) - 200} more")
        return truncate("\n".join(lines))

    # -- network diagnostics -------------------------------------------------
    def dns_lookup(self, hostname: str) -> str:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as e:
            return f"DNS lookup failed for {hostname}: {e}"
        addrs = sorted({i[4][0] for i in infos})
        return f"{hostname} -> " + ", ".join(addrs)

    def check_port(self, host: str, port: int, timeout: float = 3) -> str:
        try:
            port = int(port)
        except (TypeError, ValueError):
            return f"Error: invalid port: {port!r}"
        start = time.time()
        try:
            with socket.create_connection((host, port), timeout=float(timeout or 3)):
                ms = int((time.time() - start) * 1000)
                return f"Port {port} on {host} is OPEN (connected in {ms} ms)."
        except OSError as e:
            return f"Port {port} on {host} is closed/unreachable: {e}"

    def system_info(self) -> str:
        info_lines = [
            f"OS: {distro_name()}",
            f"Kernel: {platform.release()}",
            f"Host: {socket.gethostname()}  User: {getpass.getuser()}",
            f"Working directory: {self.cwd}",
            f"Date/time: {datetime.datetime.now().isoformat(timespec='seconds')}",
        ]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                info_lines.append(f"Local IP: {s.getsockname()[0]}")
        except OSError:
            info_lines.append("Local IP: (no network route found)")
        try:
            du = shutil.disk_usage(self.cwd)
            info_lines.append(f"Disk ({self.cwd}): {du.used // 2**30} GB used / "
                              f"{du.total // 2**30} GB total ({du.free // 2**30} GB free)")
        except OSError:
            pass
        return "\n".join(info_lines)

    # -- dispatch ------------------------------------------------------------
    def dispatch(self, name: str, args: dict) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            if name == "run_command":
                return self.run_command(args.get("command", ""), args.get("timeout"))
            if name == "fetch_url":
                return self.fetch_url(args.get("url", ""), args.get("max_chars", 8000))
            if name == "web_search":
                return self.web_search(args.get("query", ""), args.get("max_results", 5))
            if name == "read_file":
                return self.read_file(args.get("path", ""), args.get("max_chars", 8000))
            if name == "write_file":
                ov = args.get("overwrite", False)
                if isinstance(ov, str):
                    ov = ov.lower() in ("true", "yes", "1")
                return self.write_file(args.get("path", ""), args.get("content", ""), bool(ov))
            if name == "list_dir":
                return self.list_dir(args.get("path", "."))
            if name == "dns_lookup":
                return self.dns_lookup(args.get("hostname", ""))
            if name == "check_port":
                return self.check_port(args.get("host", ""), args.get("port", 0), args.get("timeout", 3))
            if name == "system_info":
                return self.system_info()
        except Exception as e:  # never let a tool crash the agent loop
            return f"Tool {name} raised an error: {e}"
        return f"Error: unknown tool {name!r}. Available: {', '.join(TOOL_NAMES)}."

# Tool schemas sent to Ollama (native function-calling format)
TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a non-interactive bash command on this linux Mint machine and return its output. Requires user confirmation.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The bash command to run, e.g. 'df -h' or 'ss -tulpn'"},
            "timeout": {"type": "integer", "description": "Max seconds to wait (default 60, max 600)"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Fetch an http(s) URL (GET) and return its readable text content.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "Max characters to return (default 8000)"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web with DuckDuckGo. Returns titles, URLs and snippets. Use fetch_url afterwards to read a result in full.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a local text file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write text to a local file. Requires user confirmation. Set overwrite=true to replace an existing file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List the contents of a local directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path, default '.'"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "dns_lookup",
        "description": "Resolve a hostname to IP addresses.",
        "parameters": {"type": "object", "properties": {
            "hostname": {"type": "string"}},
            "required": ["hostname"]}}},
    {"type": "function", "function": {
        "name": "check_port",
        "description": "Check whether a TCP port on a host is open (e.g. a router, server, or this machine).",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string"},
            "port": {"type": "integer"}},
            "required": ["host", "port"]}}},
    {"type": "function", "function": {
        "name": "system_info",
        "description": "Get basic info about this machine: OS, kernel, user, local IP, disk usage, date/time.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]
TOOL_NAMES = [t["function"]["name"] for t in TOOLS]

# ---------------------------------------------------------------------------
# Fallback tool-calling for models without native function calling.
# The model emits:  {"tool": "run_command", "arguments": {"command": "df -h"}}
# ---------------------------------------------------------------------------

def parse_fallback_tool_calls(content: str):
    calls = []
    if not content:
        return calls
    decoder = json.JSONDecoder()
    seen_spans = []
    for i, ch in enumerate(content):
        if ch != "{" or any(s <= i < e for s, e in seen_spans):
            continue
        try:
            obj, end = decoder.raw_decode(content[i:])
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool") or obj.get("name") or obj.get("function")
        if name not in TOOL_NAMES:
            continue
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not args:  # flat style: {"tool": "run_command", "command": "ls"}
            args = {k: v for k, v in obj.items()
                    if k not in ("tool", "name", "function", "arguments", "args", "parameters")}
        calls.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
        seen_spans.append((i, i + end))
    return calls

def normalise_tool_calls(msg: dict):
    """Return [{'name':..., 'arguments': {...}}] from an Ollama message."""
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name") or tc.get("name")
        args = fn.get("arguments", tc.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if name:
            out.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
    if out:
        return out
    return parse_fallback_tool_calls(msg.get("content") or "")

# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

class OllamaError(Exception):
    pass

class ToolUnsupported(OllamaError):
    pass

class OllamaClient:
    def __init__(self, host: str, model: str, ctx: int = DEFAULT_CTX,
                 temperature: float = 0.2, keep_alive: str = "10m"):
        self.host = normalise_host(host)
        self.model = model
        self.ctx = ctx
        self.temperature = temperature
        self.keep_alive = keep_alive

    def _request(self, path: str, payload: dict | None = None, timeout: int = 300):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.host + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if "does not support tools" in body or "does not support tool" in body:
                raise ToolUnsupported(body or str(e))
            raise OllamaError(f"Ollama HTTP {e.code}: {body or e.reason}")
        except urllib.error.URLError as e:
            raise OllamaError(
                f"Cannot reach Ollama at {self.host} ({getattr(e, 'reason', e)}).\n"
                f"Is it running? Try:  ollama serve    (or ./install.sh)")

    def tags(self):
        data = self._request("/api/tags", timeout=10)
        return [m.get("name", "") for m in data.get("models", [])]

    def is_up(self) -> bool:
        try:
            self.tags()
            return True
        except OllamaError:
            return False

    def ensure_running(self, auto_start: bool = True) -> bool:
        if self.is_up():
            return True
        if auto_start and shutil.which("ollama"):
            info(c_dim("Ollama is not running - starting `ollama serve` in the background…"))
            try:
                subprocess.Popen(["ollama", "serve"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            except OSError:
                return False
            for _ in range(50):
                time.sleep(0.2)
                if self.is_up():
                    return True
        return False

    def model_available(self, model: str | None = None) -> bool:
        model = model or self.model
        try:
            installed = self.tags()
        except OllamaError:
            return False
        if model in installed:
            return True
        base = model.split(":")[0]
        return any(m == f"{base}:latest" or m.split(":")[0] == base and ":" not in model
                   for m in installed)

    def pull(self, model: str | None = None) -> bool:
        model = model or self.model
        if not shutil.which("ollama"):
            err("`ollama` command not found. Run ./install.sh first.")
            return False
        info(f"Downloading model {c_bold(model)} (this can take a few minutes)…")
        try:
            return subprocess.run(["ollama", "pull", model]).returncode == 0
        except OSError as e:
            err(f"Failed to run ollama pull: {e}")
            return False

    def chat(self, messages, use_tools: bool = True) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_ctx": self.ctx},
        }
        if use_tools:
            payload["tools"] = TOOLS
        data = self._request("/api/chat", payload)
        return data.get("message", {}) or {}

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def build_system_prompt(toolbox: Toolbox) -> str:
    today = datetime.date.today().isoformat()
    return f"""You are Milelik, a helpful local AI assistant running on the user's {distro_name()} machine.
Today is {today}. User: {getpass.getuser()} on {socket.gethostname()}. Working directory: {toolbox.cwd}. Shell: bash.

You have tools: run_command, fetch_url, web_search, read_file, write_file, list_dir, dns_lookup, check_port, system_info.

Rules:
- To inspect or change the system, call run_command. To get current/external information, call web_search or fetch_url. NEVER invent command output, file contents, or web results - call a tool instead.
- Use short, non-interactive commands (no nano/vim/htop; use flags like --no-pager, -b, -n 1). Avoid sudo unless essential, and prefer apt-get over apt in scripts.
- One logical step at a time: look at each tool result before deciding the next step. If a command fails, read the error and adapt; don't repeat the exact same failing call.
- If the user declines a command, respect that - suggest an alternative, don't just retry it.
- When the task is done, answer directly and concisely. Summarise what you ran/found, and cite URLs you actually fetched or found in search results.

If (and only if) native tool calling is unavailable, request a tool by replying with ONLY this JSON, nothing else:
{{"tool": "run_command", "arguments": {{"command": "df -h"}}}}"""

class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label="Thinking"):
        self.label = label
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if sys.stderr.isatty():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

    def _spin(self):
        i, start = 0, time.time()
        while not self._stop.wait(0.1):
            sys.stderr.write(f"\r{self.FRAMES[i % len(self.FRAMES)]} {self.label}… {int(time.time() - start)}s")
            sys.stderr.flush()
            i += 1

class Agent:
    def __init__(self, client, toolbox: Toolbox, max_steps: int = DEFAULT_MAX_STEPS,
                 verbose: bool = False):
        self.client = client
        self.toolbox = toolbox
        self.max_steps = max_steps
        self.verbose = verbose
        self.use_native_tools = True
        self.messages = [{"role": "system", "content": build_system_prompt(toolbox)}]

    def reset(self):
        self.messages = [{"role": "system", "content": build_system_prompt(self.toolbox)}]

    def _trim_history(self):
        """Keep the conversation small enough to stay fast / inside context."""
        limit = max(12000, getattr(self.client, "ctx", DEFAULT_CTX) * 3)
        total = sum(len(m.get("content") or "") for m in self.messages)
        while total > limit and len(self.messages) > 3:
            removed = self.messages.pop(1)  # drop oldest non-system message
            total -= len(removed.get("content") or "")

    def _chat(self) -> dict:
        self._trim_history()
        try:
            with Spinner("Thinking"):
                return self.client.chat(self.messages, use_tools=self.use_native_tools)
        except ToolUnsupported:
            if self.verbose:
                warn("This model has no native tool calling - falling back to JSON tool calls.")
            self.use_native_tools = False
            with Spinner("Thinking"):
                return self.client.chat(self.messages, use_tools=False)

    def run(self, task: str) -> str:
        self.messages.append({"role": "user", "content": task})
        seen_calls = {}

        for _step in range(self.max_steps):
            msg = self._chat()
            calls = normalise_tool_calls(msg)

            # Keep the assistant turn (incl. native tool_calls) in history.
            stored = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                stored["tool_calls"] = msg["tool_calls"]
            self.messages.append(stored)

            if not calls:
                return (msg.get("content") or "").strip() or "(The model returned an empty response.)"

            for call in calls:
                name, args = call["name"], call["arguments"]
                signature = json.dumps([name, args], sort_keys=True, default=str)
                seen_calls[signature] = seen_calls.get(signature, 0) + 1

                label = args.get("command") or args.get("url") or args.get("query") \
                    or args.get("path") or args.get("hostname") or ""
                if name == "check_port":
                    label = f"{args.get('host', '')}:{args.get('port', '')}"
                info(f"{c_cyan('🔧 ' + name)} {c_dim(one_line(label))}")

                if seen_calls[signature] > 3:
                    result = ("You have already called this exact tool with these exact "
                              "arguments 3 times. Stop repeating it and either try a "
                              "different approach or give your final answer.")
                else:
                    result = self.toolbox.dispatch(name, args)

                if self.verbose:
                    info(c_dim(truncate(result, 1200)))

                if self.use_native_tools:
                    self.messages.append({"role": "tool", "content": result, "tool_name": name})
                else:
                    self.messages.append({"role": "user",
                                            "content": f"Tool result for {name}:\n{result}\n\nContinue the task, or give your final answer."})

        # Out of steps: ask for a summary with no more tools.
        self.messages.append({"role": "user",
                              "content": "You have used all available steps. Do not call any more tools. Summarise what you did, what you found, and anything left unfinished."})
        try:
            with Spinner("Wrapping up"):
                final = self.client.chat(self.messages, use_tools=False)
            text = (final.get("content") or "").strip()
        except OllamaError:
            text = ""
        return text or f"Stopped after {self.max_steps} steps without a final answer. Try a smaller task or raise --max-steps."

# ---------------------------------------------------------------------------
# Config file  (~/.config/milelik/config.json)
# ---------------------------------------------------------------------------

def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "milelik" / "config.json"

def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def write_default_config() -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "model": DEFAULT_MODEL, "host": DEFAULT_HOST, "ctx": DEFAULT_CTX,
        "max_steps": DEFAULT_MAX_STEPS, "auto": False,
    }, indent=2), encoding="utf-8")
    return p

# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

HELP_TEXT = """Commands:
  /help              show this help
  /model <name>      switch model (e.g. /model qwen3:4b)
  /models            list locally installed models
  /yolo on|off       auto-approve ordinary commands (dangerous/sudo still ask)
  /info              system info
  /clear             forget conversation history
  /exit, /quit       leave
  !<command>         run a shell command yourself, e.g.  !df -h
Anything else is sent to the AI as a task."""

def interactive(agent: Agent, client: OllamaClient, toolbox: Toolbox):
    print(c_bold("\n🤖 Milelik") + f"  model={client.model}  "
          f"{'YOLO (auto-approve) ON' if toolbox.auto else 'confirmations ON'}")
    print(c_dim("Type a task, /help for commands, /exit to quit.\n"))
    while True:
        try:
            task = input(c_green("milelik> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            return
        if not task:
            continue
        low = task.lower()
        if low in ("/exit", "/quit", "exit", "quit"):
            print("Bye!")
            return
        if low == "/help":
            print(HELP_TEXT)
            continue
        if low == "/clear":
            agent.reset()
            print("History cleared.")
            continue
        if low == "/info":
            print(toolbox.system_info())
            continue
        if low == "/models":
            try:
                print("\n".join(client.tags()) or "(no models installed - try: ollama pull qwen3:8b)")
            except OllamaError as e:
                err(str(e))
            continue
        if low.startswith("/model "):
            new = task.split(None, 1)[1].strip()
            if new:
                client.model = new
                print(f"Model set to {new}"
                      + ("" if client.model_available(new) else f"  (not downloaded yet - run: ollama pull {new})"))
            continue
        if low.startswith("/yolo"):
            toolbox.auto = low.endswith("on") if len(task.split()) > 1 else not toolbox.auto
            print(f"YOLO mode {'ON - ordinary commands auto-approved' if toolbox.auto else 'OFF'}.")
            continue
        if task.startswith("!"):
            print(toolbox.run_command(task[1:].strip(), from_user=True))
            continue
        if task.startswith("/"):
            print(f"Unknown command {task.split()[0]!r} - /help lists commands.")
            continue
        try:
            print(f"\n{agent.run(task)}\n")
        except OllamaError as e:
            err(str(e))
        except KeyboardInterrupt:
            print("\n(interrupted - task stopped)")

# ---------------------------------------------------------------------------
# --check diagnostics
# ---------------------------------------------------------------------------

def run_check(client: OllamaClient) -> int:
    ok = True
    print(c_bold("Milelik check"))
    print(f"  Python : {platform.python_version()}")
    print(f"  OS     : {distro_name()}")
    print(f"  Ollama : {client.host}")
    if client.ensure_running():
        try:
            models = client.tags()
        except OllamaError:
            models = []
        print(c_green("  ✔ Ollama is running"))
        print(f"  Models : {', '.join(models) if models else '(none installed)'}")
        if client.model_available():
            print(c_green(f"  ✔ Model '{client.model}' is installed"))
        else:
            ok = False
            print(c_yellow(f"  ✖ Model '{client.model}' is NOT installed"))
            print(f"    Fix:  ollama pull {client.model}   (or ./install.sh)")
    else:
        ok = False
        print(c_red("  ✖ Ollama is not reachable"))
        print("    Fix:  ollama serve     (or ./install.sh if Ollama isn't installed)")

    print("  Network: ", end="")
    try:
        tb = Toolbox(auto=True)
        result = tb.dns_lookup("duckduckgo.com")
        print(c_green(f"✔ DNS works ({result})") if "->" in result else c_yellow(result))
        if "->" not in result:
            ok = False
    except Exception as e:  # pragma: no cover
        print(c_red(f"✖ {e}"))
        ok = False

    print(c_green("\nAll good - try:  milelik \"what is my local IP and public IP?\"")
          if ok else c_yellow("\nSomething above needs fixing before Milelik can work."))
    return 0 if ok else 1

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    cfg = load_config()
    p = argparse.ArgumentParser(
        prog="milelik",
        description="Fast local AI assistant for linux Mint (Ollama + shell + web tools).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  milelik "find the 10 biggest folders in my home directory"
  milelik "what is my public IP, and is port 22 open on 192.168.1.1?"
  milelik "search the web for how to enable SSH on linux Mint and summarise it"
  milelik --yolo "check for apt updates and list them"
  milelik --interactive""")
    p.add_argument("task", nargs="*", help="Task to perform (omit for interactive mode)")
    p.add_argument("-i", "--interactive", action="store_true", help="Start the interactive chat")
    p.add_argument("--model", default=os.environ.get("MILELIK_MODEL", cfg.get("model", DEFAULT_MODEL)),
                   help=f"Ollama model (default: {DEFAULT_MODEL}; try qwen3:4b for speed)")
    p.add_argument("--host", default=os.environ.get("OLLAMA_HOST", cfg.get("host", DEFAULT_HOST)),
                   help="Ollama address (default: http://127.0.0.1:11434)")
    p.add_argument("--yolo", "--auto", dest="auto", action="store_true",
                   default=bool(cfg.get("auto", False)),
                   help="Auto-approve ordinary commands/file writes (dangerous commands and sudo still ask)")
    p.add_argument("--max-steps", type=int, default=int(cfg.get("max_steps", DEFAULT_MAX_STEPS)),
                   help="Max tool-using steps per task (default: 12)")
    p.add_argument("--ctx", type=int, default=int(cfg.get("ctx", DEFAULT_CTX)),
                   help="Model context size in tokens (default: 8192)")
    p.add_argument("--cmd-timeout", type=int, default=DEFAULT_CMD_TIMEOUT,
                   help="Default timeout for shell commands, seconds (default: 60)")
    p.add_argument("--check", action="store_true", help="Check Ollama/model/network and exit")
    p.add_argument("--pull", action="store_true", help="Download the selected model and exit")
    p.add_argument("--init-config", action="store_true", help="Write a default config file and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="Show full tool output")
    p.add_argument("--no-color", action="store_true", help="Disable colours")
    p.add_argument("--version", action="version", version=f"milelik {__version__}")
    return p.parse_args(argv)

def main(argv=None) -> int:
    args = parse_args(argv)

    C.enabled = (not args.no_color and sys.stdout.isatty()
                 and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb")

    if args.init_config:
        print(f"Wrote {write_default_config()}")
        return 0

    client = OllamaClient(args.host, args.model, ctx=args.ctx)
    toolbox = Toolbox(auto=args.auto, verbose=args.verbose, cmd_timeout=args.cmd_timeout)

    if args.check:
        return run_check(client)

    if not client.ensure_running():
        err(f"Ollama isn't running at {client.host}.")
        print("Start it with:  ollama serve\nOr install everything with:  ./install.sh")
        return 1

    if args.pull:
        return 0 if client.pull() else 1

    if not client.model_available():
        warn(f"Model '{client.model}' is not downloaded yet.")
        if sys.stdin.isatty():
            try:
                ans = input(f"Download it now with `ollama pull {client.model}`? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return 1
            if ans in ("", "y", "yes"):
                if client.pull():
                    pass
                else:
                    return 1
            else:
                return 1
        else:
            print(f"Run:  ollama pull {client.model}")
            return 1

    agent = Agent(client, toolbox, max_steps=args.max_steps, verbose=args.verbose)
    task = " ".join(args.task).strip()

    try:
        if args.interactive or not task:
            if not sys.stdin.isatty() and not task:
                task = sys.stdin.read().strip()   # piped task: echo "..." | milelik
                if not task:
                    err("No task given.")
                    return 2
            else:
                interactive(agent, client, toolbox)
                return 0
        print(agent.run(task))
        return 0
    except OllamaError as e:
        err(str(e))
        return 1
    except KeyboardInterrupt:
        print("\n(interrupted)")
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
