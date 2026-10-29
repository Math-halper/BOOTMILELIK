#!/usr/bin/env python3
"""
Milelik - a fast, local AI assistant for linux Mint.

It talks to a *local* model served by Ollama (or by an OpenAI-compatible
local server such as LM Studio or llama.cpp) and gives that model a small,
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
import ast
import datetime
import getpass
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

__version__ = "1.1.0"

# The model Milelik is tuned for out of the box. Note the Ollama library
# spells the repo "abliterate", while the Hugging Face GGUF file uses
# "abliterated" - Milelik accepts either name (see resolve_ollama_model_name).
TARGET_MODEL_LABEL = "Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M"
TARGET_GGUF_FILENAME = TARGET_MODEL_LABEL + ".gguf"
OLLAMA_TARGET_MODEL = "huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M"
LOCAL_IMPORTED_MODEL = "milelik-coder:7b"   # name used when importing a GGUF
DEFAULT_MODEL = OLLAMA_TARGET_MODEL
DEFAULT_HOST = "http://127.0.0.1:11434"
LMSTUDIO_HOST = "http://127.0.0.1:1234"
LLAMACPP_HOST = "http://127.0.0.1:8080"
DEFAULT_CTX = 8192
DEFAULT_MAX_STEPS = 12
DEFAULT_CMD_TIMEOUT = 60
MAX_TOOL_OUTPUT = 8000          # chars of tool output fed back to the model
MAX_DOWNLOAD_BYTES = 2_000_000  # cap web downloads at ~2 MB
USER_AGENT = f"Milelik/{__version__} (+https://github.com/Math-halper/BOOTMILELIK)"

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
# Model-name handling: Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M
# ---------------------------------------------------------------------------
# The same model shows up under several different names depending on where
# it runs, e.g.:
#   Ollama library : huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M
#   GGUF file      : Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf
#   LM Studio      : qwen2.5-coder-7b-instruct-abliterated-q4_k_m (or a path)
#   Hugging Face   : bartowski/Qwen2.5-Coder-7B-Instruct-abliterated-GGUF:Q4_K_M
# Milelik treats all of those as the same model.

def model_key(name: str) -> str:
    """A punctuation-free lower-case key, for fuzzy model-name matching."""
    s = str(name or "").strip().lower()
    if s.endswith(".gguf"):
        s = s[:-5]
    return re.sub(r"[^a-z0-9]+", "", s)

def is_qwen25_coder(name: str) -> bool:
    return "qwen25coder" in model_key(name)

def is_target_model(name: str) -> bool:
    """True for Qwen2.5-Coder-7B-Instruct-abliterated (any serving name)."""
    if not name:
        return False
    if name.strip().lower() == LOCAL_IMPORTED_MODEL:
        return True
    k = model_key(name)
    return ("qwen25coder" in k and "abliterat" in k
            and ("7b" in k or "q4km" in k or "instruct" in k))

_OTHER_QUANTS = ("q80", "q6k", "q5km", "q5ks", "q40", "q3km", "q2k", "iq4", "fp16", "f16", "bf16")
_OTHER_SIZES = ("05b", "15b", "3b", "14b", "32b")

def resolve_ollama_model_name(name: str) -> str:
    """Map any alias of the target model to its Ollama-library tag."""
    if not name:
        return DEFAULT_MODEL
    raw = name.strip()
    k = model_key(raw)
    if "qwen25coder" in k and "abliterat" in k:
        if any(q in k for q in _OTHER_QUANTS) and "q4km" not in k:
            return raw                      # a different quant - leave it alone
        if "7b" not in k and any(s in k for s in _OTHER_SIZES):
            return raw                      # a different size - leave it alone
        return OLLAMA_TARGET_MODEL
    return raw

def find_model_match(requested: str, available):
    """Best match for `requested` among a server's model ids, or None."""
    available = [a for a in (available or []) if a]
    if not available:
        return None
    if requested in available:
        return requested
    rk = model_key(requested)
    for a in available:
        if model_key(a) == rk:
            return a
    req_tokens = set(re.findall(r"[a-z0-9]+", str(requested or "").lower()))
    best, best_score = None, -1
    for a in available:
        ak = model_key(a)
        a_tokens = set(re.findall(r"[a-z0-9]+", str(a).lower()))
        score = 3 * len(req_tokens & a_tokens)
        if rk and (rk in ak or ak in rk):
            score += 40
        if is_target_model(requested) and "qwen25coder" in ak and "abliterat" in ak:
            score += 30
            if "7b" in ak:
                score += 5
            if "q4km" in ak:
                score += 5
        if score > best_score:
            best, best_score = a, score
    if is_target_model(requested):
        return best if best and "qwen25coder" in model_key(best) \
            and "abliterat" in model_key(best) else None
    # For other models only accept a near-exact match (e.g. punctuation or
    # namespace differences) - never silently substitute a different variant
    # such as an abliterated build for the standard model.
    return best if best_score >= 40 else None

def infer_backend_from_host(host: str) -> str:
    try:
        parsed = urllib.parse.urlparse(normalise_host(host))
        port = parsed.port
    except ValueError:
        return "ollama"
    if (parsed.path or "").rstrip("/").endswith("/v1") or port in (1234, 8080, 8000):
        return "openai"
    return "ollama"

_SPECIAL_TOKENS = ("<|im_start|>", "<|im_end|>", "<|endoftext|>", "<|fim_prefix|>",
                   "<|fim_suffix|>", "<|fim_middle|>", "<|repo_name|>", "<|file_sep|>")

def clean_model_text(text: str) -> str:
    """Strip leaked ChatML/control tokens from a model's final answer."""
    out = str(text or "")
    for tok in _SPECIAL_TOKENS:
        out = out.replace(tok, "")
    out = re.sub(r"</?(?:tool_call|tool_response|tools)>\s*", "", out)
    out = re.sub(r"\[/?tool_call\]\s*", "", out)
    return out.strip()

def looks_like_gguf_path(value: str) -> bool:
    return bool(value) and str(value).strip().lower().endswith(".gguf")

# The official Ollama ChatML template for Qwen2.5-Coder (with tool support).
# Importing the raw GGUF *without* this template is the usual reason tool
# calls come out as plain text / the model talks to itself.
QWEN25_CODER_TEMPLATE = r"""{{- if .Suffix }}<|fim_prefix|>{{ .Prompt }}<|fim_suffix|>{{ .Suffix }}<|fim_middle|>
{{- else if .Messages }}
{{- if or .System .Tools }}<|im_start|>system
{{- if .System }}
{{ .System }}
{{- end }}
{{- if .Tools }}
# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{{- range .Tools }}
{"type": "function", "function": {{ .Function }}}
{{- end }}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>
{{- end }}<|im_end|>
{{ end }}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 -}}
{{- if eq .Role "user" }}<|im_start|>user
{{ .Content }}<|im_end|>
{{ else if eq .Role "assistant" }}<|im_start|>assistant
{{ if .Content }}{{ .Content }}
{{- else if .ToolCalls }}<tool_call>
{{ range .ToolCalls }}{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}
{{ end }}</tool_call>
{{- end }}{{ if not $last }}<|im_end|>
{{ end }}
{{- else if eq .Role "tool" }}<|im_start|>user
<tool_response>
{{ .Content }}
</tool_response><|im_end|>
{{ end }}
{{- if and (ne .Role "assistant") $last }}<|im_start|>assistant
{{ end }}
{{- end }}
{{- else }}
{{- if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{ if .Prompt }}<|im_start|>user
{{ .Prompt }}<|im_end|>
{{ end }}<|im_start|>assistant
{{ end }}{{ .Response }}{{ if .Response }}<|im_end|>{{ end }}"""

def build_qwen25_coder_modelfile(from_ref: str = "./" + TARGET_GGUF_FILENAME) -> str:
    """A correct Ollama Modelfile for the target GGUF (ChatML + tools)."""
    return ("FROM " + from_ref + "\n\n"
            'TEMPLATE """' + QWEN25_CODER_TEMPLATE + '"""\n\n'
            'PARAMETER stop "<|im_end|>"\n'
            'PARAMETER stop "<|endoftext|>"\n'
            "PARAMETER num_ctx 8192\n"
            "PARAMETER temperature 0.1\n"
            "PARAMETER top_p 0.8\n"
            "PARAMETER top_k 20\n"
            "PARAMETER repeat_penalty 1.05\n")

def import_gguf_to_ollama(gguf_path: str, ollama_name: str = LOCAL_IMPORTED_MODEL,
                          runner=None) -> bool:
    """Import a local GGUF file into Ollama with the right Qwen template."""
    runner = runner or subprocess.run
    path = Path(os.path.expanduser(str(gguf_path or ""))).resolve()
    if not path.is_file():
        err(f"GGUF file not found: {path}")
        return False
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError as e:
        err(f"Cannot read {path}: {e}")
        return False
    if magic != b"GGUF":
        err(f"{path} does not look like a GGUF file (bad header).")
        return False
    if not re.fullmatch(r"[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?", ollama_name or ""):
        err(f"Invalid Ollama model name: {ollama_name!r}")
        return False
    if not shutil.which("ollama"):
        err("`ollama` command not found. Run ./install.sh first.")
        return False

    info(f"Importing {c_bold(path.name)} into Ollama as {c_bold(ollama_name)} "
         f"(with the Qwen2.5-Coder ChatML/tools template)…")
    with tempfile.TemporaryDirectory(prefix="milelik-model-") as tmp:
        tmpdir = Path(tmp)
        link = tmpdir / "model.gguf"
        try:
            os.symlink(path, link)
        except OSError:
            try:
                os.link(path, link)
            except OSError:
                shutil.copy2(path, link)
        modelfile = tmpdir / "Modelfile"
        modelfile.write_text(build_qwen25_coder_modelfile("./model.gguf"),
                             encoding="utf-8")
        try:
            proc = runner(["ollama", "create", ollama_name, "-f", str(modelfile)])
        except OSError as e:
            err(f"Failed to run `ollama create`: {e}")
            return False
    if getattr(proc, "returncode", 1) == 0:
        info(c_green(f"✔ Imported. Milelik will use it as '{ollama_name}'."))
        return True
    err("`ollama create` failed - see the output above.")
    return False

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
        name = canonical_tool_name(name)
        args = normalise_tool_arguments(name, args) if name in TOOL_NAMES else {}
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

# Small/local models (a 7B coder included) often use a slightly wrong tool
# or argument name. Accept the obvious synonyms instead of failing the task.
TOOL_ALIASES = {
    "bash": "run_command", "shell": "run_command", "terminal": "run_command",
    "execute_command": "run_command", "run_shell": "run_command",
    "run_shell_command": "run_command", "exec_command": "run_command",
    "execute": "run_command", "command": "run_command",
    "search_web": "web_search", "google_search": "web_search",
    "search": "web_search", "duckduckgo_search": "web_search",
    "http_get": "fetch_url", "get_url": "fetch_url", "fetch": "fetch_url",
    "open_url": "fetch_url", "browse": "fetch_url", "http_request": "fetch_url",
    "read": "read_file", "cat": "read_file", "cat_file": "read_file",
    "open_file": "read_file", "view_file": "read_file",
    "create_file": "write_file", "save_file": "write_file", "edit_file": "write_file",
    "list_directory": "list_dir", "ls": "list_dir", "list_files": "list_dir",
    "resolve": "dns_lookup", "nslookup": "dns_lookup", "resolve_hostname": "dns_lookup",
    "test_port": "check_port", "port_check": "check_port", "check_tcp_port": "check_port",
    "get_system_info": "system_info", "sysinfo": "system_info", "machine_info": "system_info",
}

def canonical_tool_name(name) -> str:
    n = str(name or "").strip()
    if "." in n:                      # e.g. "functions.run_command"
        n = n.rsplit(".", 1)[-1]
    n = re.sub(r"[\s-]+", "_", n.lower()).strip("_")
    if n in TOOL_NAMES:
        return n
    if n in TOOL_ALIASES:
        return TOOL_ALIASES[n]
    squashed = n.replace("_", "")
    for real in TOOL_NAMES:           # e.g. "runcommand"
        if squashed == real.replace("_", ""):
            return real
    return n

_PRIMARY_ARG = {
    "run_command": "command", "fetch_url": "url", "web_search": "query",
    "read_file": "path", "write_file": "path", "list_dir": "path",
    "dns_lookup": "hostname", "check_port": "host", "system_info": None,
}

_PATH_ALIASES = ("path", "file_path", "filepath", "file", "filename",
                 "directory", "dir", "folder")
_TOOL_ARG_ALIASES = {
    "run_command": {
        "command": ("command", "cmd", "shell_command", "bash_command",
                    "script", "input")},
    "fetch_url": {"url": ("url", "link", "href", "address", "uri")},
    "web_search": {
        "query": ("query", "search_query", "search_term", "q", "keyword", "term")},
    "read_file": {"path": _PATH_ALIASES},
    "write_file": {"path": _PATH_ALIASES,
                   "content": ("content", "contents", "text", "data", "body")},
    "list_dir": {"path": _PATH_ALIASES},
    "dns_lookup": {
        "hostname": ("hostname", "host_name", "host", "domain", "name")},
    "check_port": {
        "host": ("host", "hostname", "host_name", "address", "ip")},
    "system_info": {},
}

def normalise_tool_arguments(name: str, args) -> dict:
    """Coerce whatever a small model produced into the expected arguments."""
    name = canonical_tool_name(name)
    if isinstance(args, str):
        text = args.strip()
        try:
            args = json.loads(text)
        except ValueError:
            primary = _PRIMARY_ARG.get(name)
            args = {primary: text} if primary else {}
    if isinstance(args, (list, tuple)):                # e.g. ["df", "-h"]
        primary = _PRIMARY_ARG.get(name)
        args = {primary: list(args)} if primary else {}
    if not isinstance(args, dict):
        primary = _PRIMARY_ARG.get(name)
        return {primary: args} if primary and args is not None else {}

    out = dict(args)
    for nested in ("input", "params", "parameters", "args"):
        if isinstance(out.get(nested), dict):          # double-wrapped args
            merged = dict(out.pop(nested))
            merged.update({k: v for k, v in out.items() if k != nested})
            out = merged
            break
    for canonical, aliases in _TOOL_ARG_ALIASES.get(name, {}).items():
        if out.get(canonical) in (None, ""):
            for alias in aliases[1:]:
                if out.get(alias) not in (None, ""):
                    out[canonical] = out[alias]
                    break
    if isinstance(out.get("command"), (list, tuple)):
        out["command"] = shlex.join(str(x) for x in out["command"])
    if out.get("content") is not None and not isinstance(out.get("content"), str):
        out["content"] = json.dumps(out["content"], indent=2, default=str)
    return out

# ---------------------------------------------------------------------------
# Fallback tool-calling for models without native function calling.
# The model emits:  {"tool": "run_command", "arguments": {"command": "df -h"}}
# ---------------------------------------------------------------------------

def _infer_tool_name(obj: dict):
    """Infer a tool from a bare argument object inside a <tool_call> block."""
    keys = set(obj)
    if keys & {"command", "cmd", "shell_command"}:
        return "run_command"
    if {"host", "hostname"} & keys and "port" in keys:
        return "check_port"
    if keys & {"url", "link", "href"}:
        return "fetch_url"
    if keys & {"query", "search_query", "q"}:
        return "web_search"
    if keys & {"hostname", "domain"}:
        return "dns_lookup"
    if "content" in keys and keys & {"path", "file_path", "filepath"}:
        return "write_file"
    return None

def _call_from_obj(obj, infer: bool = False):
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function")
    if isinstance(fn, dict):                       # OpenAI-style nested
        name, args = fn.get("name"), fn.get("arguments", {})
    else:
        name = obj.get("tool") or obj.get("name") or fn or obj.get("tool_name")
        args = obj.get("arguments")
        if args is None:
            args = obj.get("args", obj.get("parameters", obj.get("params")))
    if not name and infer:
        name = _infer_tool_name(obj)
        if name:
            args = obj
    name = canonical_tool_name(name)
    if name not in TOOL_NAMES:
        return None
    if args is None or args == {}:  # flat style: {"name": "...", "command": "ls"}
        args = {k: v for k, v in obj.items()
                if k not in ("tool", "name", "function", "tool_name",
                             "arguments", "args", "parameters", "params")}
    return {"name": name, "arguments": normalise_tool_arguments(name, args)}

def _scan_json_tool_calls(text: str, infer: bool = False):
    calls, seen_spans = [], []
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch != "{" or any(s <= i < e for s, e in seen_spans):
            continue
        try:
            obj, end = decoder.raw_decode(text[i:])
        except (json.JSONDecodeError, ValueError):
            continue
        call = _call_from_obj(obj, infer=infer)
        if call:
            calls.append(call)
            seen_spans.append((i, i + end))
    return calls

_TOOL_CALL_BLOCK_RX = (
    re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.I | re.S),
    re.compile(r"\[tool_call\]\s*(.*?)\s*\[/tool_call\]", re.I | re.S),
)
_FUNCTION_RX = re.compile(r"<function=([A-Za-z0-9_.\-]+)>\s*(.*?)</function>", re.I | re.S)
_PARAMETER_RX = re.compile(r"<parameter=([A-Za-z0-9_\-]+)>\s*(.*?)\s*</parameter>", re.I | re.S)

def _parse_function_xml(content: str):
    """Qwen/Hermes XML style: <function=run_command><parameter=command>…"""
    calls = []
    for raw_name, body in _FUNCTION_RX.findall(content or ""):
        name = canonical_tool_name(raw_name)
        if name not in TOOL_NAMES:
            continue
        args = {}
        for key, value in _PARAMETER_RX.findall(body):
            value = value.strip()
            try:
                value = json.loads(value)
            except ValueError:
                pass
            args[key] = value
        if not args and body.strip():
            args = {(_PRIMARY_ARG.get(name) or "input"): body.strip()}
        calls.append({"name": name, "arguments": normalise_tool_arguments(name, args)})
    return calls

_POSITIONAL = {
    "run_command": ("command", "timeout"), "fetch_url": ("url", "max_chars"),
    "web_search": ("query", "max_results"), "read_file": ("path", "max_chars"),
    "write_file": ("path", "content", "overwrite"), "list_dir": ("path",),
    "dns_lookup": ("hostname",), "check_port": ("host", "port", "timeout"),
    "system_info": (),
}

def _parse_python_tool_calls(content: str):
    """Coder models sometimes answer with e.g. run_command(command="df -h")."""
    snippets = re.findall(r"```(?:python|py|json)?\s*\n(.*?)```", content or "",
                          re.I | re.S)
    stripped = (content or "").strip()
    known_starts = tuple(TOOL_NAMES) + tuple(TOOL_ALIASES)
    if stripped.startswith(known_starts):
        snippets.append(stripped)
    calls = []
    for snippet in snippets:
        try:
            tree = ast.parse(snippet.strip())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            raw = node.func.attr if isinstance(node.func, ast.Attribute) else \
                getattr(node.func, "id", "")
            name = canonical_tool_name(raw)
            if name not in TOOL_NAMES:
                continue
            args = {}
            for field, value_node in zip(_POSITIONAL.get(name, ()), node.args):
                try:
                    args[field] = ast.literal_eval(value_node)
                except (ValueError, SyntaxError):
                    pass
            for kw in node.keywords:
                if kw.arg:
                    try:
                        args[kw.arg] = ast.literal_eval(kw.value)
                    except (ValueError, SyntaxError):
                        pass
            calls.append({"name": name,
                          "arguments": normalise_tool_arguments(name, args)})
    return calls

def parse_fallback_tool_calls(content: str):
    """Understand every tool-call dialect a local model commonly emits:

    Qwen2.5 native   <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    bracket variant  [tool_call]{...}[/tool_call]
    Hermes XML       <function=run_command><parameter=command>…</parameter>
    plain/fenced JSON {"tool"|"name": "...", "arguments"|"command": …}
    Python style     run_command(command="df -h")
    """
    if not content:
        return []
    for rx in _TOOL_CALL_BLOCK_RX:                 # explicit blocks first
        calls = []
        for block in rx.findall(content):
            calls.extend(_scan_json_tool_calls(block, infer=True))
        if calls:
            return calls
    calls = _parse_function_xml(content)
    if calls:
        return calls
    calls = _scan_json_tool_calls(content)
    if calls:
        return calls
    return _parse_python_tool_calls(content)

def looks_like_malformed_tool_call(content: str) -> bool:
    """The model *tried* to call a tool but produced nothing parseable."""
    text = str(content or "")
    low = text.lower()
    if any(m in low for m in ("tool_call", "<function=", "[tool_call]")):
        return True
    names = TOOL_NAMES + list(TOOL_ALIASES)
    return any(re.search(rf"\b{re.escape(n)}\b\s*(\(|{{)", low) for n in names)

def normalise_tool_calls(msg: dict):
    """Return [{'name':..., 'arguments': {...}}] from a model message."""
    out = []
    for tc in (msg or {}).get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
        name = canonical_tool_name(fn.get("name") or tc.get("name")
                                   or tc.get("tool") or tc.get("tool_name"))
        if name not in TOOL_NAMES:
            continue
        args = fn.get("arguments", tc.get("arguments", {}))
        item = {"name": name,
                "arguments": normalise_tool_arguments(name, args)}
        if tc.get("id"):
            item["id"] = tc["id"]
        out.append(item)
    if out:
        return out
    return parse_fallback_tool_calls((msg or {}).get("content") or "")

# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

class OllamaError(Exception):
    pass

class ToolUnsupported(OllamaError):
    pass

# Both backends raise the same error type, so the agent loop doesn't care
# which local server is actually serving the model.
ModelError = OllamaError
BackendError = OllamaError

class OllamaClient:
    backend = "ollama"
    display_name = "Ollama"

    def __init__(self, host: str, model: str, ctx: int = DEFAULT_CTX,
                 temperature: float = 0.2, keep_alive: str = "10m"):
        self.host = normalise_host(host)
        self.model = resolve_ollama_model_name(model)
        self.ctx = ctx
        # Low temperature / tight sampling makes a 7B coder far more
        # reliable at emitting exactly-formatted tool calls.
        self.temperature = 0.1 if is_qwen25_coder(self.model) else temperature
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

    def tags(self, timeout: int = 10):
        data = self._request("/api/tags", timeout=timeout)
        return [m.get("name", "") for m in data.get("models", [])]

    def is_up(self) -> bool:
        try:
            self.tags(timeout=3)
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

    def resolve_available_model(self, model: str | None = None, update: bool = False):
        """Installed tag matching `model` (incl. target-model aliases)."""
        requested = model or self.model
        resolved = resolve_ollama_model_name(requested)
        try:
            installed = self.tags()
        except OllamaError:
            return None
        found = None
        if resolved in installed:
            found = resolved
        elif is_target_model(requested) and LOCAL_IMPORTED_MODEL in installed:
            found = LOCAL_IMPORTED_MODEL
        else:
            found = find_model_match(requested, installed)
            if not found:
                base = resolved.split(":")[0]
                for m in installed:
                    if m == f"{base}:latest" or (":" not in resolved
                                                  and m.split(":")[0] == base):
                        found = m
                        break
        if found and update:
            self.model = found
        return found

    def model_available(self, model: str | None = None) -> bool:
        found = self.resolve_available_model(model, update=model is None)
        return bool(found)

    def set_model(self, model: str) -> str:
        self.model = resolve_ollama_model_name(model)
        if self.is_up():
            self.resolve_available_model(self.model, update=True)
        return self.model

    def pull(self, model: str | None = None) -> bool:
        model = resolve_ollama_model_name(model or self.model)
        if not shutil.which("ollama"):
            err("`ollama` command not found. Run ./install.sh first.")
            return False
        info(f"Downloading model {c_bold(model)} (this can take a few minutes)…")
        try:
            return subprocess.run(["ollama", "pull", model]).returncode == 0
        except OSError as e:
            err(f"Failed to run ollama pull: {e}")
            return False

    @staticmethod
    def _messages_for_ollama(messages):
        """Milelik's canonical history -> Ollama's /api/chat shape."""
        out = []
        for m in messages or []:
            item = {"role": m.get("role", "user"), "content": m.get("content") or ""}
            calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = canonical_tool_name(fn.get("name") or tc.get("name"))
                args = fn.get("arguments", tc.get("arguments", {}))
                calls.append({"function": {
                    "name": name,
                    "arguments": normalise_tool_arguments(name, args)}})
            if calls:
                item["tool_calls"] = calls
            tool_name = m.get("tool_name") or m.get("name")
            if m.get("role") == "tool" and tool_name:
                item["tool_name"] = canonical_tool_name(tool_name)
            out.append(item)
        return out

    def chat(self, messages, use_tools: bool = True) -> dict:
        options = {"temperature": self.temperature, "num_ctx": self.ctx}
        if is_qwen25_coder(self.model):
            options.update({"top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05})
        payload = {
            "model": self.model,
            "messages": self._messages_for_ollama(messages),
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
        }
        if use_tools:
            payload["tools"] = TOOLS
        data = self._request("/api/chat", payload)
        return data.get("message", {}) or {}

# ---------------------------------------------------------------------------
# OpenAI-compatible client (LM Studio, llama.cpp server, …)
# ---------------------------------------------------------------------------

def normalise_openai_host(host: str) -> str:
    h = normalise_host(host or LMSTUDIO_HOST)
    return h if h.rstrip("/").endswith("/v1") else h + "/v1"

class OpenAIClient:
    """Talk to a local OpenAI-compatible server (/v1/chat/completions).

    This is how Milelik uses the exact GGUF already loaded in LM Studio
    (port 1234) or served by llama.cpp's llama-server (port 8080), with no
    4.7 GB duplicate copy inside Ollama.
    """
    backend = "openai"
    display_name = "OpenAI-compatible local server"

    def __init__(self, host: str, model: str, ctx: int = DEFAULT_CTX,
                 temperature: float = 0.2, api_key: str | None = None):
        self.host = normalise_openai_host(host)
        self.model = model or ""
        self.ctx = ctx
        self.temperature = 0.1 if is_qwen25_coder(model) or is_target_model(model) \
            else temperature
        self.api_key = (api_key or os.environ.get("MILELIK_API_KEY")
                        or os.environ.get("OPENAI_API_KEY") or "local")
        self._model_resolved = False

    def _request(self, path: str, payload: dict | None = None, timeout: int = 300):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        req = urllib.request.Request(self.host + path, data=data, headers=headers,
                                       method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            low = body.lower()
            if any(s in low for s in ("does not support tools", "does not support tool",
                                      "tools are not supported", "tool calling is not",
                                      "function calling is not", "no tool")):
                raise ToolUnsupported(body or str(e))
            raise OllamaError(f"Local model server HTTP {e.code} at {self.host}: "
                              f"{body or e.reason}")
        except urllib.error.URLError as e:
            raise OllamaError(
                f"Cannot reach a local model server at {self.host} "
                f"({getattr(e, 'reason', e)}).\n"
                "Load the GGUF in LM Studio and start its server (port 1234), "
                "or run llama-server (port 8080). See README.")

    def tags(self, timeout: int = 10):
        data = self._request("/models", timeout=timeout)
        models = data.get("data") or data.get("models") or []
        out = []
        for m in models:
            if isinstance(m, dict):
                out.append(m.get("id") or m.get("name") or "")
            elif m:
                out.append(str(m))
        return [m for m in out if m]

    def is_up(self) -> bool:
        try:
            self.tags(timeout=3)
            return True
        except OllamaError:
            return False

    def ensure_running(self, auto_start: bool = True) -> bool:
        return self.is_up()      # LM Studio / llama-server are started by the user

    def resolve_available_model(self, model: str | None = None, update: bool = False):
        requested = model or self.model
        try:
            available = self.tags()
        except OllamaError:
            return None
        found = find_model_match(requested, available)
        if found and update:
            self.model = found
            self._model_resolved = True
        return found

    def model_available(self, model: str | None = None) -> bool:
        if self.resolve_available_model(model, update=model is None):
            return True
        # Some servers report no usable id list but still serve one loaded
        # model; a non-empty explicit model name is then worth trying.
        try:
            return bool(model or self.model) and not self.tags()
        except OllamaError:
            return False

    def set_model(self, model: str) -> str:
        self.model = model
        self._model_resolved = False
        if self.is_up():
            self.resolve_available_model(model, update=True)
        return self.model

    def pull(self, model: str | None = None) -> bool:
        err("Models can't be downloaded through LM Studio/llama.cpp from here.")
        print(f"Either load {TARGET_GGUF_FILENAME} in LM Studio, or use Ollama:\n"
              f"  ollama pull {OLLAMA_TARGET_MODEL}")
        return False

    @staticmethod
    def _content_to_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(part.get("text") or part.get("content") or "")
                elif part:
                    parts.append(str(part))
            return "".join(parts)
        return "" if content is None else str(content)

    @staticmethod
    def _messages_for_openai(messages):
        """Milelik's canonical history -> OpenAI chat-completions shape."""
        out = []
        for m in messages or []:
            item = {"role": m.get("role", "user"),
                    "content": OpenAIClient._content_to_text(m.get("content"))}
            calls = []
            for i, tc in enumerate(m.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                name = canonical_tool_name(fn.get("name") or tc.get("name"))
                args = normalise_tool_arguments(
                    name, fn.get("arguments", tc.get("arguments", {})))
                if not isinstance(fn.get("arguments"), str):
                    arg_str = json.dumps(args)
                else:
                    arg_str = fn["arguments"]
                calls.append({
                    "id": tc.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": arg_str}})
            if calls:
                item["tool_calls"] = calls
            if m.get("role") == "tool":
                if m.get("tool_call_id"):
                    item["tool_call_id"] = m["tool_call_id"]
                tool_name = m.get("tool_name") or m.get("name")
                if tool_name:
                    item["name"] = canonical_tool_name(tool_name)
            out.append(item)
        return out

    def chat(self, messages, use_tools: bool = True) -> dict:
        if not self._model_resolved and \
                (not self.model or is_target_model(self.model)):
            # Prefer the server's exact id for the loaded GGUF, e.g.
            # "qwen2.5-coder-7b-instruct-abliterated-q4_k_m".
            self.resolve_available_model(self.model or TARGET_MODEL_LABEL,
                                         update=True)
        payload = {
            "model": self.model,
            "messages": self._messages_for_openai(messages),
            "stream": False,
            "temperature": self.temperature,
        }
        if is_qwen25_coder(self.model) or is_target_model(self.model):
            payload["top_p"] = 0.8
        if use_tools:
            payload["tools"] = TOOLS
            payload["tool_choice"] = "auto"
        data = self._request("/chat/completions", payload)
        msg = {}
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
        if not msg and isinstance(data.get("message"), dict):
            msg = data["message"]
        result = {"content": self._content_to_text(msg.get("content"))}
        if msg.get("tool_calls"):
            result["tool_calls"] = msg["tool_calls"]
        return result

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def make_ollama_client(host, model, ctx):
    return OllamaClient(host or DEFAULT_HOST, model, ctx=ctx)

def make_openai_client(host, model, ctx, api_key=None):
    return OpenAIClient(host or LMSTUDIO_HOST, model, ctx=ctx, api_key=api_key)

def select_client(backend: str, host, model: str, ctx: int, api_key=None):
    """Pick Ollama vs LM Studio/llama.cpp, preferring whichever is already
    serving the requested model."""
    backend = (backend or "auto").lower()
    if backend == "ollama":
        return make_ollama_client(host, model, ctx)
    if backend in ("openai", "lmstudio", "llamacpp", "llama.cpp"):
        client = make_openai_client(host, model, ctx, api_key)
        if client.is_up():
            client.resolve_available_model(model, update=True)
        return client

    # auto
    if host and normalise_host(host) != DEFAULT_HOST:
        if infer_backend_from_host(host) == "openai":
            candidates = [make_openai_client(host, model, ctx, api_key),
                          make_ollama_client(host, model, ctx)]
        else:
            candidates = [make_ollama_client(host, model, ctx),
                          make_openai_client(host, model, ctx, api_key)]
    else:
        candidates = [make_ollama_client(DEFAULT_HOST, model, ctx),
                      make_openai_client(LMSTUDIO_HOST, model, ctx, api_key),
                      make_openai_client(LLAMACPP_HOST, model, ctx, api_key)]

    up = []
    for client in candidates:
        if client.is_up():
            matched = client.resolve_available_model(model, update=True)
            up.append((bool(matched), client))
    for matched, client in up:            # a server with the model wins
        if matched:
            return client
    for _matched, client in up:           # else Ollama (it can pull), else any
        if client.backend == "ollama":
            return client
    if up:
        return up[0][1]
    return candidates[0] if candidates[0].backend == "ollama" \
        else make_ollama_client(DEFAULT_HOST, model, ctx)

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def build_system_prompt(toolbox: Toolbox, model: str = "") -> str:
    today = datetime.date.today().isoformat()
    qwen_block = ""
    if is_qwen25_coder(model) or is_target_model(model):
        # Qwen2.5-Coder was trained on exactly this <tool_call> format;
        # state it bluntly (and one call per reply) or the 7B tends to
        # narrate a plan and emit several speculative calls at once.
        qwen_block = """

TOOL FORMAT - follow this exactly:
- Call ONE tool at a time, then wait for its result in <tool_response>.
- When you need a tool, reply with ONLY the tool call, no explanation, no markdown fences:
<tool_call>
{"name": "run_command", "arguments": {"command": "df -h"}}
</tool_call>
- Another valid example:
<tool_call>
{"name": "web_search", "arguments": {"query": "linux Mint enable ssh"}}
</tool_call>
- Valid tool names are only: run_command, fetch_url, web_search, read_file, write_file, list_dir, dns_lookup, check_port, system_info. The "arguments" value must be a JSON object.
- Do NOT write things like "I will now run..." without a tool call - either emit the <tool_call> or give the final answer."""
    return f"""You are Milelik, a helpful local AI assistant running on the user's {distro_name()} machine.
Today is {today}. User: {getpass.getuser()} on {socket.gethostname()}. Working directory: {toolbox.cwd}. Shell: bash.

You have tools: run_command, fetch_url, web_search, read_file, write_file, list_dir, dns_lookup, check_port, system_info.

Rules:
- To inspect or change the system, call run_command. To get current/external information, call web_search or fetch_url. NEVER invent command output, file contents, or web results - call a tool instead.
- Use short, non-interactive commands (no nano/vim/htop; use flags like --no-pager, -b, -n 1). Avoid sudo unless essential, and prefer apt-get over apt in scripts.
- One logical step at a time: look at each tool result before deciding the next step. If a command fails, read the error and adapt; don't repeat the exact same failing call.
- If the user declines a command, respect that - suggest an alternative, don't just retry it.
- When the task is done, answer directly and concisely. Summarise what you ran/found, and cite URLs you actually fetched or found in search results.{qwen_block}

If (and only if) native tool calling is unavailable, request a tool by replying with ONLY this, nothing else:
<tool_call>
{{"name": "run_command", "arguments": {{"command": "df -h"}}}}
</tool_call>"""

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
        self.messages = [{"role": "system",
                          "content": build_system_prompt(toolbox,
                                                         getattr(client, "model", ""))}]

    def reset(self):
        self.messages = [{"role": "system",
                          "content": build_system_prompt(
                              self.toolbox, getattr(self.client, "model", ""))}]

    def refresh_system_prompt(self):
        prompt = build_system_prompt(self.toolbox,
                                     getattr(self.client, "model", ""))
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": prompt}
        else:
            self.messages.insert(0, {"role": "system", "content": prompt})

    def _one_tool_at_a_time(self) -> bool:
        # A 7B model reasons much better seeing one result at a time than
        # firing off a batch of guessed calls in parallel.
        model = getattr(self.client, "model", "")
        return is_qwen25_coder(model) or is_target_model(model)

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
        format_repairs = 0

        for step in range(self.max_steps):
            msg = self._chat()
            calls = normalise_tool_calls(msg)
            raw_content = msg.get("content") or ""

            ignored_extra = 0
            if calls and self._one_tool_at_a_time() and len(calls) > 1:
                ignored_extra = len(calls) - 1
                calls = calls[:1]

            # Keep the assistant turn in a canonical form both backends can
            # render (Ollama wants argument objects, OpenAI wants strings
            # plus call ids; each client converts from this).
            stored = {"role": "assistant", "content": raw_content}
            if calls and self.use_native_tools:
                canonical = []
                for i, call in enumerate(calls):
                    call_id = call.get("id") or f"call_{step}_{i}"
                    call["id"] = call_id
                    canonical.append({
                        "id": call_id, "type": "function",
                        "function": {"name": call["name"],
                                     "arguments": call["arguments"]}})
                stored["tool_calls"] = canonical
                if parse_fallback_tool_calls(raw_content) and \
                        len(raw_content.strip()) < 2000:
                    stored["content"] = ""   # avoid duplicating the call as text
            self.messages.append(stored)

            if not calls:
                if format_repairs < 2 and looks_like_malformed_tool_call(raw_content):
                    format_repairs += 1
                    self.messages.append({
                        "role": "user",
                        "content": ("That tool call could not be parsed. Reply with "
                                    "ONLY a valid tool call in exactly this form, "
                                    "with arguments as a JSON object and no other "
                                    "text:\n<tool_call>\n{\"name\": \"run_command\", "
                                    "\"arguments\": {\"command\": \"df -h\"}}\n</tool_call>")})
                    continue
                return clean_model_text(raw_content) \
                    or "(The model returned an empty response.)"

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

                if ignored_extra:
                    result += (f"\n\nNote: you sent {ignored_extra + 1} tool calls at "
                               "once; only the first was run. Call the next tool "
                               "now if you still need it, one at a time.")
                    ignored_extra = 0

                if self.verbose:
                    info(c_dim(truncate(result, 1200)))

                if self.use_native_tools:
                    tool_msg = {"role": "tool", "content": result,
                                "tool_name": name, "name": name}
                    if call.get("id"):
                        tool_msg["tool_call_id"] = call["id"]
                    self.messages.append(tool_msg)
                else:
                    self.messages.append({"role": "user",
                                            "content": f"Tool result for {name}:\n<tool_response>\n{result}\n</tool_response>\n\nContinue the task with ONE next tool call, or give your final answer."})

        # Out of steps: ask for a summary with no more tools.
        self.messages.append({"role": "user",
                              "content": "You have used all available steps. Do not call any more tools. Summarise what you did, what you found, and anything left unfinished."})
        try:
            with Spinner("Wrapping up"):
                final = self.client.chat(self.messages, use_tools=False)
            text = clean_model_text(final.get("content") or "")
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
        "model": DEFAULT_MODEL, "backend": "auto", "host": "",
        "ctx": DEFAULT_CTX, "max_steps": DEFAULT_MAX_STEPS, "auto": False,
    }, indent=2), encoding="utf-8")
    return p

def update_config(**updates) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    cfg.update(updates)
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p

# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

HELP_TEXT = """Commands:
  /help              show this help
  /model <name>      switch model (e.g. /model Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M)
  /models            list locally installed models
  /yolo on|off       auto-approve ordinary commands (dangerous/sudo still ask)
  /info              system info
  /clear             forget conversation history
  /exit, /quit       leave
  !<command>         run a shell command yourself, e.g.  !df -h
Anything else is sent to the AI as a task."""

def interactive(agent: Agent, client, toolbox: Toolbox):
    print(c_bold("\n🤖 Milelik") + f"  model={client.model}  "
          f"backend={getattr(client, 'backend', 'ollama')}  "
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
                print("\n".join(client.tags())
                      or f"(no models reported by {client.host})")
            except OllamaError as e:
                err(str(e))
            continue
        if low.startswith("/model "):
            new = task.split(None, 1)[1].strip()
            if new:
                resolved = client.set_model(new) \
                    if hasattr(client, "set_model") else new
                client.model = resolved
                agent.refresh_system_prompt()
                available = client.model_available(resolved)
                hint = "" if available else (
                    f"  (not found on this backend - for Ollama run: "
                    f"ollama pull {resolve_ollama_model_name(new)})")
                print(f"Model set to {resolved}{hint}")
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

def run_check(client, requested_model: str = "") -> int:
    ok = True
    print(c_bold("Milelik check"))
    print(f"  Python : {platform.python_version()}")
    print(f"  OS     : {distro_name()}")
    print(f"  Target : {TARGET_MODEL_LABEL}")
    requested_model = requested_model or getattr(client, "model", "") \
        or TARGET_MODEL_LABEL

    # Probe every local backend, so the user can see where their model is.
    probes = [
        ("Ollama", make_ollama_client(DEFAULT_HOST, requested_model,
                                      getattr(client, "ctx", DEFAULT_CTX))),
        ("LM Studio", make_openai_client(LMSTUDIO_HOST, requested_model,
                                         getattr(client, "ctx", DEFAULT_CTX))),
        ("llama.cpp", make_openai_client(LLAMACPP_HOST, requested_model,
                                         getattr(client, "ctx", DEFAULT_CTX))),
    ]
    if getattr(client, "host", "") and all(
            normalise_host(p.host) != normalise_host(client.host) for _n, p in probes):
        probes.append((getattr(client, "display_name", "Selected"), client))

    any_up = False
    for label, probe in probes:
        if probe.backend == "ollama":
            probe.ensure_running()
        if not probe.is_up():
            print(f"  {label:<10} {probe.host}  " + c_dim("(not running)"))
            continue
        any_up = True
        try:
            models = probe.tags()
        except OllamaError:
            models = []
        found = probe.resolve_available_model(requested_model, update=False)
        mark = c_green(f"✔ up - target model found as '{found}'") if found \
            else c_yellow("✔ up - target model NOT loaded/installed here")
        print(f"  {label:<10} {probe.host}  {mark}")
        if models:
            shown = ", ".join(models[:6]) + ("…" if len(models) > 6 else "")
            print(f"             models: {shown}")

    print(f"  Selected: backend={getattr(client, 'backend', '?')}  "
          f"host={getattr(client, 'host', '?')}  model={getattr(client, 'model', '?')}")
    if not any_up:
        ok = False
        print(c_red("  ✖ No local model server is reachable"))
        print("    Fix (pick one):")
        print(f"      ollama pull {OLLAMA_TARGET_MODEL}   # then: ollama serve")
        print("      ./setup-model.sh ~/Downloads/" + TARGET_GGUF_FILENAME)
        print("      …or load the GGUF in LM Studio and start its server (port 1234)")
    elif client.model_available():
        print(c_green(f"  ✔ Selected model '{client.model}' is available"))
    else:
        ok = False
        print(c_yellow(f"  ✖ Selected model '{client.model}' is not available "
                       "on the selected backend"))
        if getattr(client, "backend", "") == "ollama":
            print(f"    Fix:  ollama pull {client.model}")
            print("          …or import your GGUF:  ./setup-model.sh /path/to/"
                  + TARGET_GGUF_FILENAME)
        else:
            print(f"    Fix: load {TARGET_GGUF_FILENAME} in LM Studio / "
                  "llama-server, then re-run.")

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
        epilog=f"""examples:
  milelik "find the 10 biggest folders in my home directory"
  milelik "what is my public IP, and is port 22 open on 192.168.1.1?"
  milelik "search the web for how to enable SSH on linux Mint and summarise it"
  milelik --yolo "check for apt updates and list them"
  milelik --interactive
  milelik --model {TARGET_MODEL_LABEL} "check my disk and memory"
  milelik --backend openai --host http://127.0.0.1:1234 "check my disk"   # LM Studio
  milelik --import-gguf ~/Downloads/{TARGET_GGUF_FILENAME}""")
    p.add_argument("task", nargs="*", help="Task to perform (omit for interactive mode)")
    p.add_argument("-i", "--interactive", action="store_true", help="Start the interactive chat")
    p.add_argument("--model", default=os.environ.get("MILELIK_MODEL", cfg.get("model", DEFAULT_MODEL)),
                   help=f"Model to use (default: {DEFAULT_MODEL}). "
                        f"'{TARGET_MODEL_LABEL}', its .gguf filename, and its "
                        "LM Studio / Hugging Face names are all accepted.")
    p.add_argument("--backend", choices=("auto", "ollama", "openai"),
                   default=os.environ.get("MILELIK_BACKEND",
                                          cfg.get("backend", "auto")),
                   help="Where the model runs: auto = use whichever of Ollama "
                        "(11434), LM Studio (1234) or llama.cpp (8080) is already "
                        "serving the model; openai = an OpenAI-compatible local server "
                        "(LM Studio / llama-server). Default: auto")
    host_default = (os.environ.get("MILELIK_HOST")
                    or os.environ.get("OPENAI_BASE_URL")
                    or os.environ.get("OLLAMA_HOST")
                    or cfg.get("host") or None)
    p.add_argument("--host", default=host_default,
                   help="Model server address. Ollama: http://127.0.0.1:11434, "
                        "LM Studio: http://127.0.0.1:1234, llama.cpp: "
                        "http://127.0.0.1:8080 (default: auto-detect)")
    p.add_argument("--api-key", default=None,
                   help="API key for an OpenAI-compatible server (local servers "
                        "usually ignore it; env MILELIK_API_KEY also works)")
    p.add_argument("--yolo", "--auto", dest="auto", action="store_true",
                   default=bool(cfg.get("auto", False)),
                   help="Auto-approve ordinary commands/file writes (dangerous commands and sudo still ask)")
    p.add_argument("--max-steps", type=int, default=int(cfg.get("max_steps", DEFAULT_MAX_STEPS)),
                   help="Max tool-using steps per task (default: 12)")
    p.add_argument("--ctx", type=int, default=int(cfg.get("ctx", DEFAULT_CTX)),
                   help="Model context size in tokens (default: 8192)")
    p.add_argument("--cmd-timeout", type=int, default=DEFAULT_CMD_TIMEOUT,
                   help="Default timeout for shell commands, seconds (default: 60)")
    p.add_argument("--check", action="store_true",
                   help="Check Ollama/LM Studio/llama.cpp, the model, and network, then exit")
    p.add_argument("--pull", action="store_true",
                   help="Download the selected model with Ollama and exit")
    p.add_argument("--import-gguf", metavar="PATH", default=None,
                   help="Import a local GGUF (e.g. " + TARGET_GGUF_FILENAME + ") into "
                        "Ollama with the correct Qwen2.5-Coder ChatML/tools template, "
                        "make it Milelik's default model, and exit")
    p.add_argument("--import-name", default=LOCAL_IMPORTED_MODEL,
                   help=f"Ollama name for --import-gguf (default: {LOCAL_IMPORTED_MODEL})")
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

    # --import-gguf, or `--model /path/to/model.gguf`, means: put this exact
    # file into Ollama with the correct Qwen2.5-Coder template and use it.
    import_path = args.import_gguf
    implicit_import = False
    if not import_path and looks_like_gguf_path(args.model) \
            and Path(os.path.expanduser(args.model)).is_file():
        import_path = args.model
        implicit_import = True
    if import_path:
        host = args.host if args.host and infer_backend_from_host(args.host) == "ollama" \
            else DEFAULT_HOST
        importer = make_ollama_client(host, args.import_name, args.ctx)
        if not importer.ensure_running():
            err(f"Ollama isn't running at {importer.host}; can't import the GGUF.")
            print("Start it with:  ollama serve")
            return 1
        if not import_gguf_to_ollama(import_path, args.import_name):
            return 1
        update_config(model=args.import_name, backend="ollama")
        print(f"Saved as Milelik's default model in {config_path()}")
        if not implicit_import:
            print('Now just run:  milelik "your task"')
            return 0
        args.model, args.backend = args.import_name, "ollama"

    client = select_client(args.backend, args.host, args.model,
                           args.ctx, api_key=args.api_key)
    toolbox = Toolbox(auto=args.auto, verbose=args.verbose, cmd_timeout=args.cmd_timeout)

    if args.check:
        return run_check(client, requested_model=args.model)

    if args.pull:
        pull_client = client if client.backend == "ollama" else make_ollama_client(
            args.host if args.host and infer_backend_from_host(args.host) == "ollama"
            else DEFAULT_HOST, args.model, args.ctx)
        if not pull_client.ensure_running():
            err(f"Ollama isn't running at {pull_client.host}.")
            print("Start it with:  ollama serve")
            return 1
        return 0 if pull_client.pull() else 1

    if not client.ensure_running():
        if client.backend == "ollama":
            err(f"Ollama isn't running at {client.host}.")
            print("Start it with:  ollama serve\n"
                  "Or install everything with:  ./install.sh")
        else:
            err(f"No local model server is running at {client.host}.")
            print("Pick one:\n"
                  f"  • LM Studio: load {TARGET_GGUF_FILENAME}, then start the "
                  "local server (Developer tab, port 1234)\n"
                  f"  • llama.cpp: llama-server -m /path/to/{TARGET_GGUF_FILENAME} "
                  "--host 127.0.0.1 --port 8080 -c 8192 --jinja\n"
                  f"  • Ollama instead: ollama pull {OLLAMA_TARGET_MODEL}")
        return 1

    if not client.model_available():
        if client.backend == "ollama":
            warn(f"Model '{client.model}' is not installed in Ollama yet.")
            print("Already have the GGUF file? Press n, then run:  "
                  "./setup-model.sh /path/to/" + TARGET_GGUF_FILENAME)
            if sys.stdin.isatty():
                try:
                    ans = input(f"Download it now with `ollama pull {client.model}` "
                                "(~4.7 GB)? [Y/n] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return 1
                if ans in ("", "y", "yes"):
                    if not client.pull():
                        return 1
                else:
                    return 1
            else:
                print(f"Run:  ollama pull {client.model}")
                return 1
        else:
            try:
                loaded = client.tags()
            except OllamaError:
                loaded = []
            err(f"Model '{args.model}' is not loaded at {client.host}.")
            print("Loaded there: " + (", ".join(loaded) if loaded else "(none)"))
            print(f"Load {TARGET_GGUF_FILENAME} in LM Studio / llama-server, "
                  "or pass one of the loaded ids with --model.")
            return 1

    if args.verbose:
        print(c_dim(f"Using {client.model} via {client.display_name} at "
                    f"{client.host}"), file=sys.stderr)

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
