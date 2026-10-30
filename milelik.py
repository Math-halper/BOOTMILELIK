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
    python3 setup_milelik.py     # browser setup + Python installer + web app
    milelik                      # open the Milelik web app
    milelik "find the 10 biggest folders in my home directory"
"""

from __future__ import annotations

import argparse
import ast
import datetime
import getpass
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import struct
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

__version__ = "1.7.0"

# Milelik's bundled, ready-to-run default is the tiny Apache-2.0
# HuggingFaceTB SmolLM2 135M Instruct GGUF supplied in models/. It is fast
# and private, but necessarily much weaker at coding/tool use than a larger
# model. The Qwen2.5-Coder target below remains fully supported as an
# optional upgrade. Note the Ollama library spells that repo "abliterate",
# while the Hugging Face GGUF file uses "abliterated" - Milelik accepts
# either name (see resolve_ollama_model_name).
BUNDLED_MODEL_FILENAME = "smollm2-135m-instruct-q4_1.gguf"
BUNDLED_MODEL_LABEL = "SmolLM2-135M-Instruct-Q4_1"
BUNDLED_MODEL_STEM = "smollm2-135m-instruct-q4_1"
BUNDLED_MODEL_OLLAMA_NAME = "milelik-smollm2:135m"
TARGET_MODEL_LABEL = "Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M"
TARGET_GGUF_FILENAME = TARGET_MODEL_LABEL + ".gguf"
OLLAMA_TARGET_MODEL = "huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M"
LOCAL_IMPORTED_MODEL = "milelik-coder:7b"   # Qwen GGUF import name
DEFAULT_MODEL = BUNDLED_MODEL_OLLAMA_NAME
DEFAULT_MODEL_FILE = BUNDLED_MODEL_FILENAME
DEFAULT_HOST = "http://127.0.0.1:11434"
LMSTUDIO_HOST = "http://127.0.0.1:1234"
LLAMACPP_HOST = "http://127.0.0.1:8080"
DEFAULT_CTX = 8192
DEFAULT_MAX_STEPS = 12
DEFAULT_CMD_TIMEOUT = 60
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8765
DEFAULT_WORKSPACE = "~/MilelikWorkspace"
MODELS_DIR_NAME = "models"
MODEL_FILE_EXTENSIONS = {
    ".gguf": ("GGUF", True),
    ".ggml": ("GGML (legacy)", False),
    ".safetensors": ("SafeTensors", False),
    ".onnx": ("ONNX", False),
    ".pt": ("PyTorch", False),
    ".pth": ("PyTorch", False),
    ".bin": ("Binary model weights", False),
}
MAX_TOOL_OUTPUT = 8000          # chars of tool output fed back to the model
MAX_DOWNLOAD_BYTES = 2_000_000  # cap web downloads at ~2 MB
USER_AGENT = f"Milelik/{__version__} (+https://github.com/Math-halper/BOOTMILELIK)"
_APP_DIR = Path(__file__).resolve().parent
DEPENDENCY_INSTALLER = _APP_DIR / "dependencies" / "install_dependencies.py"


def dependency_install_command() -> str:
    """Return the bundled dependency installer command for user hints.

    The dependency manifests and installer live beside this file both in a
    source checkout and in the installed copy under ~/.local/share/milelik.
    Keeping the hint generated from __file__ means a missing helper can be
    installed from Milelik's own repository payload, rather than by telling
    the user an ad-hoc list of apt packages.
    """
    if not DEPENDENCY_INSTALLER.exists():
        return "python3 setup_milelik.py"
    try:
        command = "./" + str(DEPENDENCY_INSTALLER.relative_to(Path.cwd().resolve()))
    except ValueError:
        command = str(DEPENDENCY_INSTALLER)
    return "python3 " + shlex.quote(command)

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
    print(c_yellow(f"Warning: {msg}"), file=sys.stderr)

def err(msg):
    print(c_red(f"Error: {msg}"), file=sys.stderr)

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

def is_bundled_default_model(name: str) -> bool:
    """True for Milelik's bundled SmolLM2 135M Q4_1 default model."""
    if not name:
        return False
    if name.strip().lower() in (BUNDLED_MODEL_OLLAMA_NAME,
                                BUNDLED_MODEL_FILENAME.lower()):
        return True
    k = model_key(name)
    return "smollm2" in k and "135m" in k and "q41" in k

_OTHER_QUANTS = ("q80", "q6k", "q5km", "q5ks", "q40", "q3km", "q2k", "iq4", "fp16", "f16", "bf16")
_OTHER_SIZES = ("05b", "15b", "3b", "14b", "32b")

def resolve_ollama_model_name(name: str) -> str:
    """Map bundled-model aliases to their import name and Qwen aliases to Ollama."""
    if not name:
        return DEFAULT_MODEL
    raw = name.strip()
    k = model_key(raw)
    if is_bundled_default_model(raw):
        return BUNDLED_MODEL_OLLAMA_NAME
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
        if is_bundled_default_model(requested) and "smollm2" in ak and "135m" in ak:
            score += 30
            if "q41" in ak or str(a).lower() == BUNDLED_MODEL_OLLAMA_NAME:
                score += 10
        if score > best_score:
            best, best_score = a, score
    if is_bundled_default_model(requested):
        return best if best and "smollm2" in model_key(best) \
            and "135m" in model_key(best) else None
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

# ---------------------------------------------------------------------------
# Local model files: Milelik's models/ folders
# ---------------------------------------------------------------------------
# At startup Milelik inventories the repository/installed models/ folders.
# The inventory is deliberately JSON-friendly so the web front end can show
# exactly the same choices as the terminal.

def _format_file_size(size: int) -> str:
    value = float(max(0, int(size or 0)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def model_directories(models_dir=None, cfg: dict | None = None) -> list[Path]:
    """Directories scanned for local model files, in priority order."""
    cfg = load_config() if cfg is None else cfg
    explicit = models_dir
    if explicit is None:
        explicit = os.environ.get("MILELIK_MODELS_DIR") or cfg.get("models_dir") or ""

    raw_dirs: list[object] = []
    if isinstance(explicit, (list, tuple)):
        raw_dirs.extend(explicit)
    elif explicit:
        raw_dirs.extend(str(explicit).split(os.pathsep))

    app_dir = Path(__file__).resolve().parent
    raw_dirs.extend([app_dir / MODELS_DIR_NAME, Path.cwd() / MODELS_DIR_NAME])

    out: list[Path] = []
    seen: set[str] = set()
    for raw in raw_dirs:
        if raw is None or str(raw).strip() == "":
            continue
        try:
            path = Path(os.path.expanduser(str(raw))).resolve()
        except (OSError, RuntimeError):
            continue
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _split_gguf_info(path: Path):
    match = re.match(r"^(.*)-(\d{5})-of-(\d{5})$", path.stem)
    if not match:
        return None
    return match.group(1), int(match.group(2)), int(match.group(3))


def ollama_name_for_model_file(path, size: int | None = None) -> str:
    """A stable, unique Ollama name for an imported local model file."""
    p = Path(path)
    if is_bundled_default_model(p.name):
        return BUNDLED_MODEL_OLLAMA_NAME
    if is_target_model(p.name):
        return LOCAL_IMPORTED_MODEL
    stem = p.stem
    split = _split_gguf_info(p)
    if split:
        stem = split[0]
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "model"
    slug = slug[:56].strip("-") or "model"
    if size is None:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
    digest = hashlib.sha256(f"{p.name.lower()}|{int(size)}".encode()).hexdigest()[:8]
    return f"milelik-{slug}-{digest}:latest"


def read_gguf_metadata(path) -> dict:
    """Read a small allow-list of scalar GGUF header metadata safely.

    This only reads the header, never the tensor data. Any malformed or
    surprising file simply returns {}, leaving filename-based discovery to
    work as before.
    """
    scalar_formats = {
        0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
        6: "<f", 7: "<B", 10: "<Q", 11: "<q", 12: "<d",
    }
    keep = {
        "general.architecture", "general.name", "general.file_type",
        "general.size_label", "general.quantization_version",
        "general.parameter_count",
    }
    try:
        with open(path, "rb") as fh:
            def read(fmt):
                size = struct.calcsize(fmt)
                data = fh.read(size)
                if len(data) != size:
                    raise EOFError
                return struct.unpack(fmt, data)[0]

            if fh.read(4) != b"GGUF":
                return {}
            version = read("<I")
            if version == 1:
                _tensor_count = read("<I")
                metadata_count = read("<I")
                length_fmt = "<I"
            elif version in (2, 3):
                _tensor_count = read("<Q")
                metadata_count = read("<Q")
                length_fmt = "<Q"
            else:
                return {"gguf_version": version, "metadata": {}}
            if metadata_count > 4096:
                return {"gguf_version": version, "metadata": {}}

            def read_string():
                length = read(length_fmt)
                if length > 1_000_000:
                    raise ValueError("GGUF string is implausibly large")
                data = fh.read(length)
                if len(data) != length:
                    raise EOFError
                return data.decode("utf-8", errors="replace")

            def read_value(value_type, collect=True, depth=0):
                if value_type == 8:  # string
                    return read_string()
                if value_type == 9:  # array
                    if depth > 2:
                        raise ValueError("GGUF metadata is nested too deeply")
                    element_type = read("<I")
                    count = read(length_fmt)
                    if count > 1_000_000:
                        raise ValueError("GGUF metadata array is implausibly large")
                    values = []
                    for i in range(count):
                        value = read_value(element_type, collect and i < 20, depth + 1)
                        if collect and i < 20:
                            values.append(value)
                    return values
                fmt = scalar_formats.get(value_type)
                if fmt is None:
                    raise ValueError(f"unknown GGUF metadata type {value_type}")
                value = read(fmt)
                return bool(value) if value_type == 7 else value

            metadata = {}
            for _ in range(metadata_count):
                key = read_string()
                value_type = read("<I")
                value = read_value(value_type, collect=key in keep)
                if key in keep and isinstance(value, (str, int, float, bool)):
                    metadata[key] = value
                # The fields Milelik actually needs for display/selection
                # normally appear before large tokenizer arrays. Stop early
                # when both are known so startup stays fast.
                if {"general.architecture", "general.name"} <= metadata.keys():
                    break
            return {"gguf_version": version, "metadata": metadata}
    except (EOFError, OSError, ValueError, struct.error):
        return {}


def _model_file_record(path: Path, root: Path, parts=None,
                       missing_first_shard: bool = False) -> dict:
    parts = list(parts or [path])
    extension = path.suffix.lower()
    format_label, format_runnable = MODEL_FILE_EXTENSIONS.get(
        extension, (extension.lstrip(".").upper() or "Unknown", False))
    errors: list[str] = []
    size = 0
    modified = 0.0
    for part in parts:
        try:
            stat = part.stat()
            size += stat.st_size
            modified = max(modified, stat.st_mtime)
        except OSError:
            errors.append(f"Cannot read {part.name}")

    header: dict = {}
    valid_gguf = False
    if extension in (".gguf", ".bin"):
        try:
            with open(path, "rb") as fh:
                valid_gguf = fh.read(4) == b"GGUF"
        except OSError:
            valid_gguf = False
        if valid_gguf:
            header = read_gguf_metadata(path)
            if extension == ".bin":
                format_label = "GGUF (.bin)"
        elif extension == ".gguf":
            errors.append("Not a valid GGUF file header")

    try:
        physical = path.resolve()
    except (OSError, RuntimeError):
        physical = path.absolute()
    try:
        relative = path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        relative = path.name

    split = _split_gguf_info(path)
    expected_parts = split[2] if split else 1
    if missing_first_shard:
        errors.append("The first shard of this split GGUF is missing")
    elif len(parts) != expected_parts:
        errors.append(f"Split GGUF is incomplete ({len(parts)}/{expected_parts} shards)")

    runnable_container = format_runnable or (extension == ".bin" and valid_gguf)
    supported = bool(runnable_container and valid_gguf and not errors)
    metadata = header.get("metadata", {}) if isinstance(header, dict) else {}
    display_name = str(metadata.get("general.name") or path.stem)
    identifier = hashlib.sha256(
        f"{physical}|{size}".encode("utf-8", errors="replace")).hexdigest()[:16]
    return {
        "id": identifier,
        "name": path.name,
        "stem": path.stem,
        "display_name": display_name,
        "path": str(physical),
        "relative_path": relative,
        "directory": str(root),
        "extension": extension,
        "format": format_label,
        "size_bytes": size,
        "size_human": _format_file_size(size),
        "modified": datetime.datetime.fromtimestamp(modified).isoformat(timespec="seconds") if modified else None,
        "supported": supported,
        "runnable": supported,
        "reason": "; ".join(errors) if errors else (
            "" if runnable_container else
            "Milelik's Ollama/LM Studio/llama.cpp runtimes use GGUF files"),
        "is_target": is_target_model(path.name),
        "is_bundled_default": is_bundled_default_model(path.name),
        "is_default": is_bundled_default_model(path.name),
        "is_qwen25_coder": is_qwen25_coder(path.name) or (
            str(metadata.get("general.architecture", "")).lower() == "qwen2"
            and "coder" in display_name.lower()),
        "ollama_name": ollama_name_for_model_file(path, size),
        "architecture": metadata.get("general.architecture"),
        "gguf_version": header.get("gguf_version") if isinstance(header, dict) else None,
        "metadata": metadata,
        "parts": [str(part.resolve()) if part.exists() else str(part) for part in parts],
        "part_count": len(parts),
    }


def model_file_record_for_path(path) -> dict:
    """Build a discovery record for one explicit model file path."""
    p = Path(os.path.expanduser(str(path or "")))
    if not p.is_absolute():
        p = Path.cwd() / p
    parts = [p]
    missing_first = False
    split = _split_gguf_info(p)
    if split:
        base, part_number, total = split
        found = []
        for candidate in p.parent.glob(f"{base}-*-of-{total:05d}.gguf"):
            info = _split_gguf_info(candidate)
            if info and info[0] == base and info[2] == total:
                found.append(candidate)
        parts = sorted(found, key=lambda item: (_split_gguf_info(item) or ("", 99999, 0))[1])
        missing_first = part_number != 1 and (not parts or (_split_gguf_info(parts[0]) or ("", 0, 0))[1] != 1)
        if parts:
            p = parts[0]
    return _model_file_record(p, p.parent, parts, missing_first)


def discover_model_files(directories=None, cfg: dict | None = None) -> list[dict]:
    """Recursively inventory every recognised model file in models/ folders."""
    if directories is None:
        roots = model_directories(cfg=cfg)
    elif isinstance(directories, (str, Path)):
        roots = [Path(os.path.expanduser(str(directories))).resolve()]
    else:
        roots = [Path(os.path.expanduser(str(d))).resolve() for d in directories]

    plain: list[tuple[Path, Path]] = []
    split_groups: dict[tuple[str, str, int], list[tuple[Path, Path]]] = {}
    ignored_suffixes = (".part", ".partial", ".download", ".tmp", ".aria2")

    for root in roots:
        if not root.is_dir():
            continue
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in (".git", "__pycache__") and not d.startswith("."))
            for filename in sorted(filenames, key=str.lower):
                lower = filename.lower()
                if filename.startswith(".") or lower.endswith(ignored_suffixes):
                    continue
                path = Path(current) / filename
                if path.suffix.lower() not in MODEL_FILE_EXTENSIONS:
                    continue
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue
                split = _split_gguf_info(path) if path.suffix.lower() == ".gguf" else None
                if split:
                    try:
                        parent_key = str(path.parent.resolve())
                    except (OSError, RuntimeError):
                        parent_key = str(path.parent)
                    key = (parent_key, split[0].lower(), split[2])
                    split_groups.setdefault(key, []).append((path, root))
                else:
                    plain.append((path, root))

    records: list[dict] = []
    seen_physical: set[str] = set()

    def add(path, root, parts=None, missing_first=False):
        try:
            physical_key = str(path.resolve())
        except (OSError, RuntimeError):
            physical_key = str(path.absolute())
        if physical_key in seen_physical:
            return
        seen_physical.add(physical_key)
        records.append(_model_file_record(path, root, parts, missing_first))

    # If the same physical model is visible through both a real path and a
    # symlink (for example an installed link to the repository file), prefer
    # the real file's name/path for display and selection.
    best_plain: dict[str, tuple[tuple[bool, int], Path, Path]] = {}
    for index, (path, root) in enumerate(plain):
        try:
            physical_key = str(path.resolve())
        except (OSError, RuntimeError):
            physical_key = str(path.absolute())
        rank = (path.is_symlink(), index)
        if physical_key not in best_plain or rank < best_plain[physical_key][0]:
            best_plain[physical_key] = (rank, path, root)
    for _rank, path, root in sorted(best_plain.values(), key=lambda item: item[0][1]):
        add(path, root)
    for group in split_groups.values():
        ordered = sorted(
            group, key=lambda item: (_split_gguf_info(item[0]) or ("", 99999, 0))[1])
        first_path, first_root = ordered[0]
        first_info = _split_gguf_info(first_path)
        add(first_path, first_root, [item[0] for item in ordered],
            missing_first=not first_info or first_info[1] != 1)

    records.sort(key=lambda r: (
        0 if r.get("supported") else 1,
        0 if r.get("is_bundled_default") else 1,
        0 if r.get("is_target") else 1,
        str(r.get("name", "")).lower(),
    ))
    return records


def find_model_file(requested: str, records) -> dict | None:
    """Find a discovered model by id, path, filename, stem or Ollama name."""
    records = list(records or [])
    if not requested or not records:
        return None
    raw = str(requested).strip()
    try:
        requested_path = Path(os.path.expanduser(raw)).resolve()
    except (OSError, RuntimeError):
        requested_path = None
    for record in records:
        candidates = {
            str(record.get("id", "")), str(record.get("name", "")),
            str(record.get("stem", "")), str(record.get("ollama_name", "")),
            str(record.get("relative_path", "")),
        }
        if raw in candidates:
            return record
        if requested_path is not None and str(requested_path) == str(record.get("path", "")):
            return record
    if is_bundled_default_model(raw):
        for record in records:
            if record.get("is_bundled_default"):
                return record
    if is_target_model(raw):
        for record in records:
            if record.get("is_target"):
                return record
    key = model_key(raw)
    for record in records:
        if key and key in {
            model_key(record.get("name", "")), model_key(record.get("stem", "")),
            model_key(record.get("ollama_name", "")),
            model_key(record.get("display_name", "")),
        }:
            return record
    return None


def format_model_inventory(records, directories=None) -> str:
    lines = []
    if directories:
        lines.append("Model folders scanned:")
        lines.extend(f"  • {directory}" for directory in directories)
    if not records:
        lines.append("No AI model files found. Put a .gguf file in Milelik's models/ folder.")
        return "\n".join(lines)
    lines.append(f"Found {len(records)} model file(s):")
    for index, record in enumerate(records, 1):
        state = "ready" if record.get("supported") else "unsupported"
        detail = f"{record.get('format')} · {record.get('size_human')}"
        if record.get("part_count", 1) > 1:
            detail += f" · {record['part_count']} shards"
        if record.get("reason"):
            detail += f" · {record['reason']}"
        lines.append(f"  {index}. {record.get('name')} [{state}] — {detail}")
        lines.append(f"     {record.get('path')}")
    return "\n".join(lines)


def choose_model_file(records=None, configured_model: str = "",
                      selected_model_file: str = "", interactive: bool = False,
                      input_fn=input, print_fn=print,
                      allow_configured: bool = True,
                      strict_selected: bool = False) -> dict | None:
    """Choose one local model file, optionally by asking in the terminal."""
    records = list(records if records is not None else discover_model_files())
    explicit = find_model_file(selected_model_file, records)
    if selected_model_file:
        p = Path(os.path.expanduser(str(selected_model_file)))
        if p.is_file():
            explicit = explicit or model_file_record_for_path(p)
        elif strict_selected and not explicit:
            return None
    if explicit and not interactive:
        return explicit

    configured = find_model_file(configured_model, records)
    supported = [record for record in records if record.get("supported")]
    if not supported:
        return explicit or configured

    if interactive and supported:
        default = explicit if explicit in supported else (
            configured if configured in supported else supported[0])
        default_index = supported.index(default) + 1
        print_fn("\nAI model files in Milelik's models/ folder:")
        for index, record in enumerate(supported, 1):
            marker = " [default]" if index == default_index else ""
            print_fn(f"  {index}. {record['name']} — {record['size_human']}{marker}")
            print_fn(f"     {record['path']}")
        unsupported = [r for r in records if not r.get("supported")]
        if unsupported:
            print_fn("  Also found but not runnable by Milelik's GGUF backends:")
            for record in unsupported:
                print_fn(f"     • {record['name']} ({record.get('format')})")
        if allow_configured:
            print_fn("  0. Use the configured/server model instead")
        while True:
            try:
                answer = str(input_fn(
                    f"Choose a model [1-{len(supported)}] (Enter = {default_index}): "
                ) or "").strip()
            except (EOFError, KeyboardInterrupt):
                return default
            if not answer:
                return default
            if allow_configured and answer == "0":
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(supported):
                return supported[int(answer) - 1]
            selected = find_model_file(answer, supported)
            if selected:
                return selected
            print_fn("Please choose one of the numbers above.")

    if explicit:
        return explicit
    return configured if configured and configured.get("supported") else supported[0]

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

def build_generic_modelfile(from_ref: str) -> str:
    """A minimal Modelfile that lets Ollama use a GGUF's own chat template."""
    return ("FROM " + from_ref + "\n\n"
            "PARAMETER num_ctx 8192\n"
            "PARAMETER temperature 0.2\n")


def build_modelfile_for_gguf(path, from_ref: str) -> str:
    p = Path(path)
    header = read_gguf_metadata(p)
    metadata = header.get("metadata", {}) if isinstance(header, dict) else {}
    architecture = str(metadata.get("general.architecture") or "").lower()
    name_text = f"{p.name} {metadata.get('general.name') or ''}"
    if is_qwen25_coder(name_text) or (architecture == "qwen2"
                                      and "coder" in model_key(name_text)):
        return build_qwen25_coder_modelfile(from_ref)
    return build_generic_modelfile(from_ref)


def _gguf_shards(path: Path) -> list[Path]:
    split = _split_gguf_info(path)
    if not split:
        return [path]
    base, _part, total = split
    found = []
    for candidate in path.parent.glob(f"{base}-*-of-{total:05d}.gguf"):
        info = _split_gguf_info(candidate)
        if info and info[0] == base and info[2] == total:
            found.append(candidate)
    return sorted(found, key=lambda item: (_split_gguf_info(item) or ("", 99999, 0))[1])

def import_gguf_to_ollama(gguf_path: str, ollama_name: str = LOCAL_IMPORTED_MODEL,
                          runner=None) -> bool:
    """Import a local GGUF into Ollama, using Qwen's template only for Qwen."""
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
        err("`ollama` command not found. Run python3 setup_milelik.py first.")
        return False

    shards = _gguf_shards(path)
    split = _split_gguf_info(path)
    if split and split[1] != 1:
        err("Select the first shard (…-00001-of-…) when importing a split GGUF.")
        return False
    if split and len(shards) != split[2]:
        err(f"Split GGUF is incomplete: found {len(shards)} of {split[2]} shards beside {path}.")
        return False

    modelfile_text_probe = build_modelfile_for_gguf(path, "./model.gguf")
    is_qwen = "<|im_start|>" in modelfile_text_probe
    info(f"Importing {c_bold(path.name)} into Ollama as {c_bold(ollama_name)}"
         + (" (with the Qwen2.5-Coder ChatML/tools template)" if is_qwen else "")
         + "…")
    with tempfile.TemporaryDirectory(prefix="milelik-model-") as tmp:
        tmpdir = Path(tmp)
        if split:
            from_ref = "./" + path.name
            links = [(shard, tmpdir / shard.name) for shard in shards]
        else:
            from_ref = "./model.gguf"
            links = [(path, tmpdir / "model.gguf")]
        for source, link in links:
            try:
                os.symlink(source, link)
            except OSError:
                try:
                    os.link(source, link)
                except OSError:
                    shutil.copy2(source, link)
        modelfile = tmpdir / "Modelfile"
        modelfile.write_text(build_modelfile_for_gguf(path, from_ref),
                             encoding="utf-8")
        try:
            proc = runner(["ollama", "create", ollama_name, "-f", str(modelfile)])
        except OSError as e:
            err(f"Failed to run `ollama create`: {e}")
            return False
    if getattr(proc, "returncode", 1) == 0:
        info(c_green(f"Imported. Milelik will use it as '{ollama_name}'."))
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
                 cwd: str | None = None, cmd_timeout: int = DEFAULT_CMD_TIMEOUT,
                 app_dirs=None, github_api_base: str | None = None,
                 confirm_callback=None):
        self.auto = auto                # --yolo: skip normal confirmations
        self.verbose = verbose
        # Optional UI hook used by the web front end. It receives the same
        # keyword arguments as confirm() and returns True/False.
        self.confirm_callback = confirm_callback
        self.cwd = cwd or os.getcwd()
        self.cmd_timeout = cmd_timeout
        if app_dirs is None:
            data_home = Path(os.environ.get(
                "XDG_DATA_HOME", Path.home() / ".local" / "share"))
            bases = [data_home] + [Path(p) for p in os.environ.get(
                "XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if p]
            bases += [data_home / "flatpak" / "exports" / "share",
                      Path("/var/lib/flatpak/exports/share"),
                      Path("/var/lib/snapd/desktop")]
            seen, self.app_dirs = set(), []
            for base in bases:
                d = base / "applications"
                if str(d) not in seen:
                    seen.add(str(d))
                    self.app_dirs.append(d)
        else:
            self.app_dirs = [Path(p) for p in app_dirs]
        self.github_api_base = (github_api_base
                                or os.environ.get("GITHUB_API_URL")
                                or "https://api.github.com").rstrip("/")
        self.prefer_gh = True   # use the gh CLI for GitHub writes when present

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
            if self.confirm_callback is not None:
                return bool(self.confirm_callback(
                    action=action, detail=detail,
                    dangerous_reason=dangerous_reason, sudo=sudo,
                    from_user=from_user))
            print(c_red(f"\nDANGEROUS — this looks like {dangerous_reason}:"))
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
        if self.confirm_callback is not None:
            return bool(self.confirm_callback(
                action=action, detail=detail, dangerous_reason=None,
                sudo=sudo, from_user=from_user))
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

    # -- generic process helpers (list-args, never a shell) ------------------
    @staticmethod
    def _is_url(value: str) -> bool:
        return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", str(value or ""))) \
            or str(value or "").startswith("mailto:")

    @staticmethod
    def _format_proc(code, out: str = "", error: str = "") -> str:
        parts = [f"exit_code: {code}"]
        if (out or "").strip():
            parts.append(f"stdout:\n{out.strip()}")
        if (error or "").strip():
            parts.append(f"stderr:\n{error.strip()}")
        if not (out or "").strip() and not (error or "").strip():
            parts.append("(no output)")
        return truncate("\n".join(parts))

    def _exec(self, args, timeout: int = 30, cwd: str | None = None,
              input_text: str | None = None):
        """Run a program with an argument list. Returns (code, stdout, stderr)."""
        try:
            timeout = max(1, min(int(timeout or 30), 900))
        except (TypeError, ValueError):
            timeout = 30
        try:
            proc = subprocess.run([str(a) for a in args], cwd=cwd or self.cwd,
                                  capture_output=True, text=True, timeout=timeout,
                                  input=input_text, errors="replace")
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except FileNotFoundError:
            return 127, "", f"{args[0] if args else 'program'}: command not found"
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else ""
            error = e.stderr if isinstance(e.stderr, str) else ""
            return 124, out, (error + f"\nTimed out after {timeout}s").strip()
        except OSError as e:
            return 1, "", str(e)

    def _exec_text(self, args, timeout: int = 30, cwd: str | None = None,
                   input_text: str | None = None) -> str:
        return self._format_proc(*self._exec(args, timeout, cwd, input_text))

    @staticmethod
    def _which_any(names):
        for name in names:
            found = shutil.which(str(name))
            if found:
                return found
        return None

    def _launch_detached(self, args, action: str, detail: str = "",
                         confirm_needed: bool = True, cwd: str | None = None,
                         dangerous_reason: str | None = None,
                         sudo: bool = False) -> str:
        if confirm_needed and not self.confirm(
                action, detail or " ".join(str(a) for a in args),
                dangerous_reason, sudo):
            return (f"User declined to {action}. Do not retry it unchanged; "
                    "suggest an alternative or move on.")
        try:
            proc = subprocess.Popen([str(a) for a in args], cwd=cwd or self.cwd,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True, close_fds=True)
        except FileNotFoundError:
            return f"Error: `{args[0] if args else 'program'}` is not installed."
        except OSError as e:
            return f"Error launching {args[0] if args else 'program'}: {e}"
        return f"Launched {' '.join(str(a) for a in args)} (pid {proc.pid})."

    def _prepare_target(self, target: str):
        """A file/dir/URL ready to hand to xdg-open/an app, or (None, error)."""
        value = str(target or "").strip()
        if not value:
            return None, "Error: no file, folder or URL was given."
        if self._is_url(value):
            return value, None
        candidate = self._resolve(value)
        if candidate.exists():
            return str(candidate), None
        looks_path = ("/" in value or value.startswith(("~", ".")))
        if looks_path:
            return None, f"Error: no such file or folder: {candidate}"
        if "." in value and " " not in value:      # bare domain, e.g. github.com
            return "https://" + value, None
        return value, None

    @staticmethod
    def _clamp_int(value, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(int(value), high))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _split_csv(value):
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [v.strip() for v in re.split(r"[,\n]", str(value or "")) if v.strip()]

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

    # -- desktop applications ------------------------------------------------
    @staticmethod
    def _as_bool(value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "y", "1", "on")
        return bool(value) if value is not None else default

    @staticmethod
    def _parse_desktop_file(path: Path):
        data, in_entry = {}, False
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for raw in lines:
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                in_entry = line == "[Desktop Entry]"
                continue
            if not in_entry or "=" not in line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if "[" not in key and key not in data:   # skip localised duplicates
                data[key] = value.strip()
        if data.get("Type", "Application") != "Application":
            return None
        if data.get("NoDisplay", "").lower() == "true" \
                or data.get("Hidden", "").lower() == "true":
            return None
        return {
            "id": path.stem,
            "name": data.get("Name") or path.stem,
            "comment": data.get("Comment", ""),
            "categories": data.get("Categories", ""),
            "exec": data.get("Exec", ""),
            "path": str(path),
        }

    def _desktop_entries(self):
        entries, seen = [], set()
        for directory in self.app_dirs:
            try:
                files = sorted(Path(directory).glob("*.desktop"))
            except OSError:
                continue
            for fp in files:
                if fp.stem in seen:
                    continue
                entry = self._parse_desktop_file(fp)
                if entry:
                    seen.add(fp.stem)
                    entries.append(entry)
        return sorted(entries, key=lambda e: e["name"].lower())

    @staticmethod
    def _desktop_exec_args(entry, target: str = ""):
        raw = (entry or {}).get("exec", "")
        raw = raw.replace("%%", "\x00")
        raw = re.sub(r"%[fFuUdDnNickvm]", "", raw).replace("\x00", "%")
        try:
            args = [a for a in shlex.split(raw) if a]
        except ValueError:
            args = [a for a in raw.split() if a]
        if target:
            args.append(str(target))
        return args

    @staticmethod
    def _exec_basename(entry) -> str:
        args = Toolbox._desktop_exec_args(entry)
        i = 0
        if args and Path(args[0]).name == "env":
            i = 1
            while i < len(args) and "=" in args[i] and not args[i].startswith("-"):
                i += 1
        return Path(args[i]).name.lower() if i < len(args) else ""

    def _find_app(self, query: str):
        q = str(query or "").strip().lower()
        if q.endswith(".desktop"):
            q = q[:-8]
        if not q:
            return None
        q_tokens = set(re.findall(r"[a-z0-9]+", q))
        best = []
        for e in self._desktop_entries():
            name, eid = e["name"].lower(), e["id"].lower()
            corpus = f"{name} {eid} {e['comment']} {e['categories']}".lower()
            corpus_tokens = set(re.findall(r"[a-z0-9]+", corpus))
            score = 0
            if q in (name, eid, self._exec_basename(e)):
                score = 100
            elif name.startswith(q) or eid.startswith(q):
                score = 80
            elif q_tokens and all(
                    any(t in token or token in t for token in corpus_tokens)
                    for t in q_tokens):
                score = 60
            elif q in name or q in eid or q in self._exec_basename(e):
                score = 50
            elif q in e["comment"].lower() or q in e["categories"].lower():
                score = 30
            if score:
                best.append((-score, name, e))
        if not best:
            return None
        best.sort(key=lambda x: (x[0], x[1]))
        return best[0][2]

    def list_apps(self, query: str = "", max_results: int = 30) -> str:
        entries = self._desktop_entries()
        if not entries:
            dirs = ", ".join(str(d) for d in self.app_dirs)
            return f"No desktop applications found in: {dirs}"
        tokens = re.findall(r"[a-z0-9]+", str(query or "").lower())
        if tokens:
            entries = [e for e in entries if all(
                t in f"{e['name']} {e['id']} {e['comment']} "
                     f"{e['categories']} {e['exec']}".lower()
                for t in tokens)]
        if not entries:
            return f"No installed applications match {query!r}."
        limit = self._clamp_int(max_results, 30, 1, 100)
        lines = [f"Installed applications ({len(entries)}"
                 + (f" matching {query!r}" if query else "") + "):"]
        for e in entries[:limit]:
            detail = f" — {e['comment']}" if e["comment"] else ""
            lines.append(f"- {e['name']} (id: {e['id']}){detail}")
        if len(entries) > limit:
            lines.append(f"… and {len(entries) - limit} more")
        return truncate("\n".join(lines))

    def open_app(self, app: str, target: str = "") -> str:
        wanted = str(app or "").strip()
        if not wanted:
            return "Error: no application was named. Use list_apps to see choices."
        target_value = ""
        if target:
            target_value, error = self._prepare_target(target)
            if error:
                return error
        entry = self._find_app(wanted)
        if entry:
            gtk_launch = shutil.which("gtk-launch")
            if gtk_launch:
                args = [gtk_launch, entry["id"]]
                if target_value:
                    args.append(target_value)
            else:
                args = self._desktop_exec_args(entry, target_value)
            if not args:
                return f"Error: {entry['name']} has no launch command."
            detail = entry["name"] + (f" with {target_value}" if target_value else "")
            return self._launch_detached(args, "open application", detail)
        executable = shutil.which(wanted) if "/" not in wanted else (
            wanted if Path(wanted).is_file() else None)
        if executable:
            args = [executable] + ([target_value] if target_value else [])
            return self._launch_detached(args, "open application",
                                          " ".join(args))
        return (f"Application not found: {wanted!r}. "
                "Use list_apps to see installed applications and their ids.")

    def open_target(self, target: str, application: str = "") -> str:
        if application:
            return self.open_app(application, target)
        value, error = self._prepare_target(target)
        if error:
            return error
        opener = shutil.which("xdg-open")
        if not opener:
            return ("Error: xdg-open is not installed, so there is no default "
                    "opener. Name an application instead.")
        return self._launch_detached(
            [opener, value], "open with the default application", value)

    def open_terminal(self, path: str = ".", command: str = "") -> str:
        directory = self._resolve(path or ".")
        if directory.is_file():
            directory = directory.parent
        if not directory.is_dir():
            return f"Error: no such directory: {directory}"
        term = self._which_any([
            "gnome-terminal", "xfce4-terminal", "mate-terminal", "tilix",
            "konsole", "alacritty", "kitty", "x-terminal-emulator", "xterm"])
        if not term:
            return "Error: no terminal emulator found on this system."
        name = Path(term).name
        shell_cmd = str(command or "").strip()
        keep_open = f"{shell_cmd}; exec bash" if shell_cmd else ""
        args = [term]
        if name == "gnome-terminal":
            args += [f"--working-directory={directory}"]
            if keep_open:
                args += ["--", "bash", "-lc", keep_open]
        elif name == "konsole":
            args += ["--workdir", str(directory)]
            if keep_open:
                args += ["-e", "bash", "-lc", keep_open]
        elif name in ("xfce4-terminal", "mate-terminal"):
            args += [f"--working-directory={directory}"]
            if keep_open:
                args += [f"--command=bash -lc {shlex.quote(keep_open)}"]
        elif keep_open:
            args += ["-e", "bash", "-lc", keep_open]
        detail = f"{name} in {directory}" + (f" running: {shell_cmd}" if shell_cmd else "")
        return self._launch_detached(
            args, "open terminal", detail, cwd=str(directory),
            dangerous_reason=check_dangerous(shell_cmd) if shell_cmd else None,
            sudo=uses_sudo(shell_cmd) if shell_cmd else False)

    # -- media players (VLC and any MPRIS player) ------------------------------
    def _playerctl(self, args, player: str = ""):
        ctl = shutil.which("playerctl")
        if not ctl:
            return None
        cmd = [ctl]
        if player:
            cmd.append(f"--player={str(player).strip()}")
        return self._exec(cmd + [str(a) for a in args], timeout=10)

    @staticmethod
    def _fmt_seconds(seconds) -> str:
        try:
            total = int(float(seconds))
        except (TypeError, ValueError):
            return "?"
        h, rem = divmod(max(total, 0), 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _media_status(self, player: str = "") -> str:
        probe = self._playerctl(["status"], player)
        if probe is None:
            return ("playerctl is not installed, so playback can't be inspected/"
                    "controlled. Install Milelik's bundled dependencies with: "
                    + dependency_install_command())
        code, status, error = probe
        if code != 0:
            return ("No media player is running"
                    + (f" ({player})" if player else "") + "."
                    + (f" {error.strip()}" if error.strip() else ""))

        def meta(*cmd):
            r = self._playerctl(list(cmd), player)
            return r[1].strip() if r and r[0] == 0 else ""

        title = meta("metadata", "title") or meta("metadata", "xesam:title")
        artist = meta("metadata", "artist") or meta("metadata", "xesam:artist")
        album = meta("metadata", "album") or meta("metadata", "xesam:album")
        url = meta("metadata", "xesam:url")
        length_us = meta("metadata", "mpris:length")
        pos = meta("position")
        volume = meta("volume")
        who = meta("--list-all") if not player else player
        lines = [f"Player: {player or who or '(default)'}",
                 f"Status: {status.strip() or 'unknown'}"]
        if title:
            lines.append("Title: " + title)
        if artist:
            lines.append("Artist: " + artist)
        if album:
            lines.append("Album: " + album)
        if url:
            lines.append("Source: " + url)
        try:
            length = self._fmt_seconds(int(length_us) / 1_000_000) if length_us else ""
        except ValueError:
            length = ""
        if pos or length:
            lines.append(f"Position: {self._fmt_seconds(pos)}"
                         + (f" / {length}" if length else ""))
        if volume:
            try:
                lines.append(f"Volume: {round(float(volume) * 100)}%")
            except ValueError:
                lines.append("Volume: " + volume)
        return "\n".join(lines)

    def media_player(self, action: str = "status", player: str = "",
                     value: str = "") -> str:
        act = str(action or "status").strip().lower().replace("-", "_").replace(" ", "_")
        act = {"toggle": "play_pause", "pause_play": "play_pause",
               "nowplaying": "now_playing", "info": "status"}.get(act, act)
        player = str(player or "").strip()
        if player and not re.fullmatch(r"[A-Za-z0-9_.,*+-]+", player):
            return f"Error: invalid player name {player!r}."
        if act in ("status", "now_playing"):
            return self._media_status(player)
        if act in ("list", "list_players", "players"):
            result = self._playerctl(["--list-all"])
            if result is None:
                return self._media_status(player)
            names = [n for n in result[1].splitlines() if n.strip()]
            return "Running media players: " + (", ".join(names) if names else "(none)")
        simple = {"play": "play", "pause": "pause", "play_pause": "play-pause",
                  "stop": "stop", "next": "next", "previous": "previous"}
        if act in simple:
            result = self._playerctl([simple[act]], player)
            if result is None:
                return self._media_status(player)
            if result[0] != 0:
                return f"Could not {act}: {result[2].strip() or 'no player is running.'}"
            return f"{simple[act]} sent" + (f" to {player}" if player else "") + ".\n" \
                + self._media_status(player)
        if act in ("seek_forward", "seek_backward", "seek_to"):
            try:
                secs = float(value or 10)
            except (TypeError, ValueError):
                return f"Error: seek value must be a number of seconds, got {value!r}."
            pos = f"{secs:g}+" if act == "seek_forward" else \
                f"{secs:g}-" if act == "seek_backward" else f"{secs:g}"
            result = self._playerctl(["position", pos], player)
            if result is None:
                return self._media_status(player)
            return self._format_proc(*result) if result[0] else self._media_status(player)
        if act in ("volume", "volume_set", "volume_up", "volume_down", "mute"):
            if act == "mute":
                target = 0.0
            else:
                try:
                    amount = float(value if value != "" else 10)
                except (TypeError, ValueError):
                    return f"Error: volume must be 0-100, got {value!r}."
                if act in ("volume_up", "volume_down"):
                    cur = self._playerctl(["volume"], player)
                    try:
                        current = float(cur[1].strip()) * 100 if cur and cur[0] == 0 else 50
                    except ValueError:
                        current = 50
                    amount = current + amount if act == "volume_up" else current - amount
                target = max(0.0, min(amount, 100.0)) / 100
            result = self._playerctl(["volume", f"{target:.2f}"], player)
            if result is None:
                return self._media_status(player)
            return self._format_proc(*result) if result[0] else \
                f"Volume set to {round(target * 100)}%.\n" + self._media_status(player)
        return ("Error: unknown media_player action. Use status, list_players, "
                "play, pause, play_pause, stop, next, previous, seek_forward, "
                "seek_backward, seek_to, volume_set, volume_up, volume_down or mute.")

    def media_info(self, path: str) -> str:
        if self._is_url(path):
            return f"media_info works on local files; use fetch_url for {path!r}."
        p = self._resolve(path or "")
        if not p.is_file():
            return f"Error: no such file: {p}"
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            code, out, error = self._exec([
                ffprobe, "-v", "error",
                "-show_entries",
                "format=duration,size,bit_rate,format_name:"
                "stream=codec_type,codec_name,width,height,r_frame_rate,channels",
                "-of", "json", str(p)], timeout=30)
            if code == 0:
                try:
                    data = json.loads(out)
                except ValueError:
                    data = {}
                fmt = data.get("format", {})
                lines = [f"File: {p}",
                         f"Format: {fmt.get('format_name', 'unknown')}",
                         f"Duration: {self._fmt_seconds(fmt.get('duration'))}",
                         f"Size: {fmt.get('size', p.stat().st_size)} bytes"]
                if fmt.get("bit_rate"):
                    lines.append(f"Bitrate: {round(int(fmt['bit_rate']) / 1000)} kb/s")
                for i, s in enumerate(data.get("streams", [])):
                    desc = f"Stream {i}: {s.get('codec_type', '?')} {s.get('codec_name', '?')}"
                    if s.get("width"):
                        desc += f" {s['width']}x{s.get('height')}"
                    fps = s.get("r_frame_rate", "")
                    if "/" in str(fps):
                        num, den = str(fps).split("/", 1)
                        try:
                            if float(den):
                                desc += f" @ {float(num) / float(den):.2f} fps"
                        except ValueError:
                            pass
                    if s.get("channels"):
                        desc += f", {s['channels']}ch"
                    lines.append(desc)
                return "\n".join(lines)
            return truncate(f"ffprobe could not read {p}:\n{error.strip()}")
        mediainfo = shutil.which("mediainfo")
        if mediainfo:
            return truncate(self._exec_text([mediainfo, str(p)], timeout=30), 4000)
        lines = [f"File: {p}", f"Size: {p.stat().st_size} bytes"]
        file_cmd = shutil.which("file")
        if file_cmd:
            _c, out, _e = self._exec([file_cmd, "-b", str(p)])
            if out.strip():
                lines.append("Type: " + out.strip())
        lines.append("(Install ffmpeg for full media details with Milelik's "
                     f"bundled dependency installer: {dependency_install_command()})")
        return "\n".join(lines)

    def vlc(self, action: str = "", source: str = "", value: str = "",
            fullscreen: bool = False, start_paused: bool = False) -> str:
        act = str(action or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not act:
            act = "play" if source else "status"
        act = {"open": "play", "add": "enqueue", "toggle": "play_pause",
               "nowplaying": "status", "info": "status"}.get(act, act)
        if act in ("play", "enqueue"):
            binary = shutil.which("vlc")
            if not binary:
                # Flatpak/Snap installs may only exist as a desktop app.
                if self._find_app("vlc"):
                    return self.open_app("vlc", source) + \
                        ("\n(Launched via its desktop entry; fullscreen/"
                         "start-paused options only apply to the vlc binary.)"
                         if self._as_bool(fullscreen) or self._as_bool(start_paused)
                         else "")
                return ("Error: VLC is not installed. Install Milelik's "
                        "bundled dependencies with: "
                        + dependency_install_command())
            if not source:
                args = [binary]
                return self._launch_detached(args, "open VLC", "VLC")
            value_target, error = self._prepare_target(source)
            if error:
                return error
            args = [binary]
            if act == "enqueue":
                args += ["--one-instance", "--playlist-enqueue"]
            if self._as_bool(fullscreen):
                args.append("--fullscreen")
            if self._as_bool(start_paused):
                args.append("--start-paused")
            args.append(value_target)
            return self._launch_detached(
                args, "play in VLC" if act == "play" else "add to VLC playlist",
                value_target)
        if act == "quit":
            pkill = shutil.which("pkill")
            if not pkill:
                return "Error: pkill not found; cannot close VLC."
            if not self.confirm("close VLC", "Terminate the VLC process"):
                return ("User declined to close VLC. Do not retry it unchanged; "
                        "suggest an alternative or move on.")
            code, _out, _err = self._exec([pkill, "-TERM", "-x", "vlc"])
            return "VLC closed." if code == 0 else "VLC was not running."
        if act == "status":
            status = self._media_status("vlc")
            if status.startswith("playerctl is not installed"):
                pgrep = shutil.which("pgrep")
                if pgrep:
                    code, out, _e = self._exec([pgrep, "-a", "-x", "vlc"])
                    if code == 0 and out.strip():
                        return "VLC is running:\n" + out.strip() + "\n" + status
                    return "VLC is not running.\n" + status
            return status
        if act == "fullscreen" and not source:
            return ("Fullscreen can be chosen when starting playback "
                    "(vlc action=play, fullscreen=true); it can't be toggled "
                    "reliably over MPRIS.")
        return self.media_player(act, player="vlc", value=value)

    # -- desktop controls ------------------------------------------------------
    def _audio_volume(self, act: str, value: str = "") -> str:
        wpctl, pactl = shutil.which("wpctl"), shutil.which("pactl")
        if act == "volume_get":
            if wpctl:
                _c, out, _e = self._exec([wpctl, "get-volume", "@DEFAULT_AUDIO_SINK@"])
                m = re.search(r"Volume:\s*([0-9.]+)", out)
                if m:
                    return f"Volume: {round(float(m.group(1)) * 100)}%" \
                        + (" (muted)" if "MUTED" in out else "")
            if pactl:
                _c, out, _e = self._exec([pactl, "get-sink-volume", "@DEFAULT_SINK@"])
                m = re.search(r"(\d+)%", out)
                _c2, muted, _e2 = self._exec([pactl, "get-sink-mute", "@DEFAULT_SINK@"])
                if m:
                    return f"Volume: {m.group(1)}%" + \
                        (" (muted)" if "yes" in muted.lower() else "")
            return "No wpctl/pactl found to read the volume."
        try:
            amount = float(value if value != "" else 5)
        except (TypeError, ValueError):
            return f"Error: volume must be a percentage, got {value!r}."
        if act in ("volume_increase", "volume_decrease"):
            current_text = self._audio_volume("volume_get")
            m = re.search(r"(\d+)%", current_text)
            current = int(m.group(1)) if m else 50
            amount = current + amount if act == "volume_increase" else current - amount
        pct = max(0, min(int(round(amount)), 100))
        if wpctl:
            code, out, err = self._exec(
                [wpctl, "set-volume", "@DEFAULT_AUDIO_SINK@", f"{pct / 100:.2f}"])
        elif pactl:
            code, out, err = self._exec(
                [pactl, "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"])
        else:
            return "No wpctl/pactl found to set the volume."
        return self._format_proc(code, out, err) if code else f"Volume set to {pct}%."

    def _brightness(self, act: str, value: str = "") -> str:
        bctl = shutil.which("brightnessctl")
        if not bctl:
            return ("brightnessctl is not installed. Install Milelik's "
                    "bundled dependencies with: "
                    + dependency_install_command())
        if act == "brightness_get":
            _c, cur, _e = self._exec([bctl, "get"])
            _c, mx, _e = self._exec([bctl, "max"])
            try:
                return f"Brightness: {round(int(cur) / int(mx) * 100)}% " \
                       f"({cur.strip()}/{mx.strip()})"
            except (ValueError, ZeroDivisionError):
                return "Could not read the brightness."
        try:
            amount = int(float(value if value != "" else 10))
        except (TypeError, ValueError):
            return f"Error: brightness must be a percentage, got {value!r}."
        if act == "brightness_set":
            setting = f"{max(1, min(amount, 100))}%"
        elif act == "brightness_increase":
            setting = f"{max(1, amount)}%+"
        else:
            setting = f"{max(1, amount)}%-"
        code, out, err = self._exec([bctl, "set", setting])
        return self._format_proc(code, out, err) if code else self._brightness("brightness_get")

    def desktop_control(self, action: str = "", value: str = "",
                        title: str = "Milelik", path: str = "") -> str:
        act = str(action or "").strip().lower().replace("-", "_").replace(" ", "_")
        if act in ("volume",):
            act = "volume_set" if value != "" else "volume_get"
        if act in ("brightness",):
            act = "brightness_set" if value != "" else "brightness_get"
        if act == "notify":
            binary = shutil.which("notify-send")
            if not binary:
                return "Error: notify-send is not installed (libnotify-bin)."
            return self._exec_text([binary, str(title or "Milelik"),
                                    str(value or "")], timeout=10)
        if act.startswith("volume_"):
            if act == "volume_mute_toggle":
                pactl = shutil.which("pactl")
                wpctl = shutil.which("wpctl")
                if pactl:
                    return self._exec_text(
                        [pactl, "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
                if wpctl:
                    return self._exec_text(
                        [wpctl, "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
                return "No wpctl/pactl found to mute."
            return self._audio_volume(act, value)
        if act.startswith("brightness_"):
            return self._brightness(act, value)
        if act == "battery":
            lines = []
            try:
                batteries = sorted(Path("/sys/class/power_supply").glob("BAT*"))
            except OSError:
                batteries = []
            for bat in batteries:
                def read(name):
                    try:
                        return (bat / name).read_text().strip()
                    except OSError:
                        return ""
                cap, status = read("capacity"), read("status")
                if cap:
                    lines.append(f"{bat.name}: {cap}% ({status or 'unknown'})")
            return "\n".join(lines) or "No battery found (desktop PC?)."
        if act == "screenshot":
            if path:
                target = self._resolve(path)
            else:
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                target = Path.home() / "Pictures" / f"milelik-screenshot-{stamp}.png"
            if not self.confirm("take a screenshot", str(target)):
                return ("User declined to take a screenshot. Do not retry it "
                        "unchanged; suggest an alternative or move on.")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return f"Error creating {target.parent}: {e}"
            candidates = [
                ("gnome-screenshot", ["-f", str(target)]),
                ("xfce4-screenshooter", ["-f", "-s", str(target)]),
                ("spectacle", ["-b", "-n", "-o", str(target)]),
                ("maim", [str(target)]),
                ("scrot", [str(target)]),
                ("import", ["-window", "root", str(target)]),
            ]
            for binary, extra in candidates:
                found = shutil.which(binary)
                if found:
                    code, _o, err = self._exec([found] + extra, timeout=30)
                    if code == 0 and target.exists():
                        return f"Screenshot saved to {target}."
                    return truncate(f"{binary} failed:\n{err.strip()}")
            return ("No screenshot tool found. Install Milelik's bundled "
                    "dependencies with: " + dependency_install_command())
        if act in ("clipboard_get", "clipboard_set"):
            if not self.confirm("read the clipboard" if act.endswith("get")
                                else "replace the clipboard contents",
                                "(clipboard contents can be sensitive)"
                                if act.endswith("get") else one_line(value, 80)):
                return ("User declined clipboard access. Do not retry it "
                        "unchanged; suggest an alternative or move on.")
            if act == "clipboard_get":
                xclip, xsel, wlpaste = (shutil.which("xclip"), shutil.which("xsel"),
                                        shutil.which("wl-paste"))
                if xclip:
                    cmd = [xclip, "-o", "-selection", "clipboard"]
                elif xsel:
                    cmd = [xsel, "--clipboard", "--output"]
                elif wlpaste:
                    cmd = [wlpaste]
                else:
                    return ("No clipboard tool found. Install Milelik's "
                            "bundled dependencies with: "
                            + dependency_install_command())
                code, out, err = self._exec(cmd, timeout=10)
                return truncate(out) if code == 0 else \
                    f"Could not read the clipboard: {err.strip()}"
            xclip, xsel, wlcopy = (shutil.which("xclip"), shutil.which("xsel"),
                                   shutil.which("wl-copy"))
            if xclip:
                cmd = [xclip, "-i", "-selection", "clipboard"]
            elif xsel:
                cmd = [xsel, "--clipboard", "--input"]
            elif wlcopy:
                cmd = [wlcopy]
            else:
                return ("No clipboard tool found. Install Milelik's "
                        "bundled dependencies with: "
                        + dependency_install_command())
            code, _o, err = self._exec(cmd, timeout=10, input_text=str(value or ""))
            return "Clipboard updated." if code == 0 else \
                f"Could not set the clipboard: {err.strip()}"
        if act == "lock_screen":
            command = None
            for name, extra in [("cinnamon-screensaver-command", ["--lock"]),
                                ("mate-screensaver-command", ["--lock"]),
                                ("xfce4-screensaver-command", ["--lock"]),
                                ("gnome-screensaver-command", ["--lock"]),
                                ("xdg-screensaver", ["lock"]),
                                ("loginctl", ["lock-session"])]:
                found = shutil.which(name)
                if found:
                    command = [found] + extra
                    break
            if not command:
                return "No screen-lock command found on this system."
            return self._launch_detached(command, "lock the screen",
                                          "The screen will lock immediately")
        if act == "wallpaper":
            image, error = self._prepare_target(path or value)
            if error:
                return error
            image_path = Path(image)
            if not image_path.is_file():
                return f"Error: wallpaper image not found: {image_path}"
            if not self.confirm("change the desktop wallpaper", str(image_path)):
                return ("User declined to change the wallpaper. Do not retry "
                        "it unchanged; suggest an alternative or move on.")
            uri = image_path.resolve().as_uri()
            gsettings = shutil.which("gsettings")
            desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            if gsettings and "mate" in desktop:
                return self._exec_text([gsettings, "set", "org.mate.background",
                                        "picture-filename", str(image_path)])
            if gsettings:
                schema = "org.cinnamon.desktop.background" \
                    if "cinnamon" in desktop else "org.gnome.desktop.background"
                c1 = self._exec([gsettings, "set", schema, "picture-uri", uri])
                self._exec([gsettings, "set", schema, "picture-uri-dark", uri])
                return self._format_proc(*c1) if c1[0] else f"Wallpaper set to {image_path}."
            feh = shutil.which("feh")
            if feh:
                return self._exec_text([feh, "--bg-fill", str(image_path)])
            return "Could not find gsettings/feh to set the wallpaper."
        return ("Error: unknown desktop_control action. Use notify, screenshot, "
                "clipboard_get, clipboard_set, volume_get, volume_set, "
                "volume_increase, volume_decrease, volume_mute_toggle, "
                "brightness_get, brightness_set, brightness_increase, "
                "brightness_decrease, battery, lock_screen or wallpaper.")

    # -- LibreOffice documents --------------------------------------------------
    def office_document(self, action: str = "", path: str = "", format: str = "",
                        app: str = "", output_dir: str = "") -> str:
        binary = self._which_any(["libreoffice", "soffice"])
        act = str(action or "").strip().lower().replace("-", "_")
        if not act:
            act = "convert" if format else "open"
        if not binary:
            return ("Error: LibreOffice is not installed. Install Milelik's "
                    "bundled dependencies with: "
                    + dependency_install_command())
        if act == "open":
            flags = {"writer": "--writer", "calc": "--calc",
                     "impress": "--impress", "draw": "--draw", "base": "--base"}
            args = [binary]
            if str(app or "").lower() in flags:
                args.append(flags[str(app).lower()])
            detail = "LibreOffice"
            if path:
                p = self._resolve(path)
                if not p.exists():
                    return f"Error: no such file: {p}"
                args.append(str(p))
                detail = str(p)
            return self._launch_detached(args, "open in LibreOffice", detail)
        if act == "convert":
            if not path:
                return "Error: no document path was given to convert."
            fmt = str(format or "").strip().lower().lstrip(".")
            if not re.fullmatch(r"[a-z0-9]+", fmt):
                return ("Error: give a simple output format like pdf, docx, "
                        f"odt, txt or csv (got {format!r}).")
            src = self._resolve(path)
            if not src.is_file():
                return f"Error: no such file: {src}"
            outdir = self._resolve(output_dir) if output_dir else src.parent
            if not self.confirm("convert document with LibreOffice",
                                 f"{src} -> {outdir / (src.stem + '.' + fmt)} "
                                 "(may overwrite an existing file)"):
                return ("User declined to convert the document. Do not retry "
                        "it unchanged; suggest an alternative or move on.")
            try:
                outdir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return f"Error creating {outdir}: {e}"
            profile = Path(tempfile.gettempdir()) / \
                f"milelik-libreoffice-{os.getuid() if hasattr(os, 'getuid') else 'profile'}"
            code, out, err = self._exec(
                [binary, f"-env:UserInstallation={profile.as_uri()}",
                 "--headless", "--convert-to", fmt,
                 "--outdir", str(outdir), str(src)], timeout=180)
            result = outdir / (src.stem + "." + fmt)
            if code == 0 and result.exists():
                return f"Converted {src} to {result}."
            return truncate(f"LibreOffice conversion failed:\n{out}\n{err}".strip())
        return "Error: office_document action must be open or convert."

    # -- local Git ---------------------------------------------------------------
    @staticmethod
    def parse_github_repo(value: str):
        """owner/repo from a slug, github.com URL, or git remote URL."""
        s = str(value or "").strip()
        s = re.sub(r"^(https?://|ssh://)github\.com[:/]", "", s)
        s = re.sub(r"^git@github\.com:", "", s)
        s = s.rstrip("/")
        if s.endswith(".git"):
            s = s[:-4]
        return s if re.fullmatch(r"[\w.-]+/[\w.-]+", s) else None

    def _repo_directory(self, path: str = ".") -> Path:
        p = self._resolve(path or ".")
        return p.parent if p.is_file() else p

    def _git(self, args, path: str = ".", timeout: int = 120):
        binary = shutil.which("git")
        if not binary:
            return (127, "", "git: command not found (install Milelik's "
                    "bundled dependencies with: "
                    + dependency_install_command() + ")")
        directory = self._repo_directory(path)
        return self._exec([binary, "-C", str(directory)] + [str(a) for a in args],
                          timeout=timeout, cwd=str(directory))

    def _current_github_repo(self, path: str = "."):
        code, out, _e = self._git(["remote", "get-url", "origin"], path, timeout=15)
        return self.parse_github_repo(out) if code == 0 else None

    def _github_repo_arg(self, repo: str = "", path: str = "."):
        if repo:
            slug = self.parse_github_repo(repo)
            return slug, None if slug else \
                f"Error: {repo!r} is not a GitHub repository (expected owner/repo)."
        slug = self._current_github_repo(path)
        if slug:
            return slug, None
        return None, ("Error: no repository given and the current directory "
                      "has no GitHub 'origin' remote. Pass repo=\"owner/repo\".")

    @staticmethod
    def _valid_branch(branch: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9][\w./-]*", str(branch or ""))) \
            and ".." not in str(branch)

    def git_repo(self, action: str = "status", path: str = ".", message: str = "",
                 branch: str = "", remote: str = "origin", files="",
                 max_results: int = 10) -> str:
        act = str(action or "status").strip().lower().replace("-", "_").replace(" ", "_")
        directory = self._repo_directory(path)
        if act != "init" and not directory.exists():
            return f"Error: no such directory: {directory}"
        limit = self._clamp_int(max_results, 10, 1, 100)
        read_actions = {
            "status": ["status", "--short", "--branch"],
            "diff": ["diff"],
            "diff_staged": ["diff", "--cached"],
            "diff_stat": ["diff", "--stat"],
            "log": ["log", "--oneline", "--decorate", f"-n{limit}"],
            "branches": ["branch", "-a", "-vv"],
            "remotes": ["remote", "-v"],
            "current_branch": ["branch", "--show-current"],
        }
        if act in read_actions:
            return self._format_proc(*self._git(read_actions[act], path))
        write_labels = {
            "init": "initialise a Git repository",
            "add": "stage files with Git",
            "commit": "create a Git commit",
            "push": "push with Git",
            "pull": "pull with Git",
            "fetch": "fetch with Git",
            "checkout": "switch Git branch",
            "create_branch": "create and switch to a Git branch",
            "stash": "stash local changes",
            "stash_pop": "restore stashed changes",
        }
        if act not in write_labels:
            return ("Error: unknown git_repo action. Use status, diff, "
                    "diff_staged, diff_stat, log, branches, remotes, "
                    "current_branch, init, add, commit, push, pull, fetch, "
                    "checkout, create_branch, stash or stash_pop.")
        if act == "add":
            wanted = self._split_csv(files)
            if not wanted:
                return ("Error: list the files to stage (files=\"a.py,b.py\") - "
                        "refusing to guess/add everything silently.")
            if any(f.startswith("-") for f in wanted):
                return "Error: file names may not start with '-'."
            args = ["add", "--"] + wanted
            detail = f"{directory}: git add {' '.join(wanted)}"
        elif act == "commit":
            if not str(message or "").strip():
                return "Error: a commit message is required."
            args = ["commit", "-m", str(message).strip()]
            detail = f"{directory}: {one_line(message, 100)}"
        elif act == "push":
            if branch and not self._valid_branch(branch):
                return f"Error: invalid branch name {branch!r}."
            if not re.fullmatch(r"[\w.-]+", str(remote or "origin")):
                return f"Error: invalid remote name {remote!r}."
            args = ["push", str(remote or "origin"), branch or "HEAD"]
            detail = f"{directory}: push {branch or 'HEAD'} to {remote or 'origin'}"
        elif act == "pull":
            if branch and not self._valid_branch(branch):
                return f"Error: invalid branch name {branch!r}."
            args = ["pull", str(remote or "origin")] + ([branch] if branch else [])
            detail = f"{directory}: pull from {remote or 'origin'} {branch}".strip()
        elif act == "fetch":
            args = ["fetch", str(remote or "origin")]
            detail = f"{directory}: fetch {remote or 'origin'}"
        elif act in ("checkout", "create_branch"):
            if not self._valid_branch(branch):
                return "Error: give a valid branch name."
            args = ["switch"] + (["-c"] if act == "create_branch" else []) + [branch]
            detail = f"{directory}: switch to {branch}"
        elif act == "stash":
            args = ["stash", "push"]
            if message:
                args += ["-m", str(message)]
            detail = f"{directory}: stash local changes"
        elif act == "stash_pop":
            args = ["stash", "pop"]
            detail = f"{directory}: pop the latest stash"
        elif act == "init":
            args = ["init"]
            detail = f"{directory}: git init"
        else:  # pragma: no cover - guarded above
            args = []
            detail = str(directory)
        if not self.confirm(write_labels[act], detail):
            return (f"User declined to {write_labels[act]}. Do not retry it "
                    "unchanged; suggest an alternative or move on.")
        if act == "init":
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return f"Error creating {directory}: {e}"
        timeout = 600 if act in ("push", "pull", "fetch") else 120
        return self._format_proc(*self._git(args, path, timeout=timeout))

    # -- GitHub --------------------------------------------------------------------
    @staticmethod
    def _github_token() -> str:
        return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""

    def _gh_binary(self):
        return shutil.which("gh") if self.prefer_gh else None

    def _gh_exec(self, args, timeout: int = 120, cwd: str | None = None):
        binary = self._gh_binary()
        if not binary:
            return None
        return self._exec([binary] + [str(a) for a in args], timeout=timeout, cwd=cwd)

    def _github_api(self, path: str, method: str = "GET", body=None,
                    params=None, accept: str | None = None):
        query = ""
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                query = "?" + urllib.parse.urlencode(clean, doseq=True)
        url = self.github_api_base + "/" + path.lstrip("/") + query
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._github_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read(2_000_000).decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, None, f"Could not contact GitHub: {getattr(e, 'reason', e)}"
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = None
        return status, parsed, raw

    @staticmethod
    def _github_error(status, data, raw, what: str = "GitHub request"):
        if 200 <= status < 300:
            return None
        if status == 0:
            return truncate(raw or "Could not contact GitHub.")
        msg = data.get("message") if isinstance(data, dict) else ""
        text = f"{what} failed: HTTP {status}" + (f" - {msg}" if msg else "")
        if status in (401, 403):
            text += ("\nHint: authenticate with `gh auth login`, or set "
                     "GITHUB_TOKEN/GH_TOKEN. (403 can also mean rate limiting.)")
        elif status == 404:
            text += "\nHint: check the owner/repo spelling; private repos need auth."
        elif status == 422:
            text += "\nHint: check the branch/title/fields - GitHub rejected the input."
        return truncate(text)

    def _github_get(self, path: str, params=None, what: str = "GitHub request"):
        status, data, raw = self._github_api(path, params=params)
        error = self._github_error(status, data, raw, what)
        return (None, error) if error else (data, None)

    def _gh_api_json(self, endpoint: str, what: str = "GitHub request"):
        result = self._gh_exec(["api", endpoint], timeout=60)
        if result is None:
            return None, ("Authentication required. Run `gh auth login`, or "
                          "set GITHUB_TOKEN/GH_TOKEN.")
        code, out, err = result
        if code != 0:
            return None, truncate(f"{what} failed via gh: {err.strip() or out.strip()}")
        try:
            return json.loads(out or "{}"), None
        except ValueError:
            return None, f"{what} returned non-JSON output via gh."

    def _github_get_auth(self, path: str, params=None, what: str = "GitHub request"):
        if self._github_token():
            return self._github_get(path, params, what)
        endpoint = path.lstrip("/")
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                endpoint += "?" + urllib.parse.urlencode(clean, doseq=True)
        return self._gh_api_json(endpoint, what)

    @staticmethod
    def _gh_date(value: str) -> str:
        return str(value or "")[:10]

    @classmethod
    def _gh_repo_line(cls, r: dict) -> str:
        bits = [r.get("full_name", "?")]
        if r.get("private"):
            bits.append("(private)")
        if r.get("language"):
            bits.append(str(r["language"]))
        bits.append(f"stars:{r.get('stargazers_count', 0)}")
        bits.append(f"forks:{r.get('forks_count', 0)}")
        bits.append(f"issues:{r.get('open_issues_count', 0)}")
        if r.get("updated_at"):
            bits.append("updated " + cls._gh_date(r["updated_at"]))
        line = "  ".join(bits)
        if r.get("description"):
            line += f"\n   {one_line(r['description'], 160)}"
        if r.get("html_url"):
            line += f"\n   {r['html_url']}"
        return line

    @classmethod
    def _gh_issue_line(cls, i: dict) -> str:
        labels = ",".join(l.get("name", "") for l in i.get("labels", [])
                          if isinstance(l, dict))
        who = (i.get("user") or {}).get("login", "?")
        return (f"#{i.get('number')} [{i.get('state')}] {one_line(i.get('title', ''), 110)}"
                f"\n   by {who}, updated {cls._gh_date(i.get('updated_at'))}"
                + (f", labels: {labels}" if labels else "")
                + f"\n   {i.get('html_url', '')}")

    @classmethod
    def _gh_pr_line(cls, p: dict) -> str:
        who = (p.get("user") or {}).get("login", "?")
        head = ((p.get("head") or {}).get("ref") or "?")
        base = ((p.get("base") or {}).get("ref") or "?")
        draft = " draft" if p.get("draft") else ""
        return (f"#{p.get('number')} [{p.get('state')}{draft}] "
                f"{one_line(p.get('title', ''), 110)}\n"
                f"   by {who}: {head} -> {base}, updated {cls._gh_date(p.get('updated_at'))}\n"
                f"   {p.get('html_url', '')}")

    @classmethod
    def _gh_run_line(cls, r: dict) -> str:
        status = r.get("status", "?")
        conclusion = r.get("conclusion")
        state = f"{status}/{conclusion}" if conclusion else status
        return (f"run {r.get('id')} [{state}] {r.get('name') or r.get('display_title') or ''}"
                f"\n   branch {r.get('head_branch')}, event {r.get('event')}, "
                f"{cls._gh_date(r.get('created_at'))}\n   {r.get('html_url', '')}")

    def _github_declined(self, action_label: str) -> str:
        return (f"User declined to {action_label} on GitHub. Do not retry it "
                "unchanged; suggest an alternative or move on.")

    def _github_write_result(self, gh_result, api_status_data_raw=None,
                             success_hint: str = ""):
        if gh_result is not None:
            code, out, err = gh_result
            if code == 0:
                return truncate((out.strip() or success_hint or "Done."))
            return truncate(f"gh failed:\n{err.strip() or out.strip()}")
        status, data, raw = api_status_data_raw
        error = self._github_error(status, data, raw, "GitHub write")
        if error:
            return error
        if isinstance(data, dict) and data.get("html_url"):
            return (success_hint + "\n" if success_hint else "") + data["html_url"]
        return success_hint or "Done."

    def github_account(self, action: str = "auth_status", username: str = "",
                       max_results: int = 10) -> str:
        act = str(action or "auth_status").strip().lower().replace("-", "_")
        limit = self._clamp_int(max_results, 10, 1, 50)
        if act == "auth_status":
            lines = []
            gh = self._gh_binary()
            if gh:
                code, out, err = self._exec([gh, "auth", "status"], timeout=20)
                lines.append("gh CLI: installed")
                combined = (out + "\n" + err).strip()
                if combined:
                    lines.append(truncate(combined, 1200))
                lines.append("gh auth: " + ("logged in" if code == 0 else "not logged in"))
            else:
                lines.append("gh CLI: not installed (run "
                             f"{dependency_install_command()})")
            lines.append("GITHUB_TOKEN/GH_TOKEN env var: "
                         + ("set (value hidden)" if self._github_token() else "not set"))
            data, error = self._github_get_auth("/user", what="whoami")
            if not error and isinstance(data, dict) and data.get("login"):
                lines.append(f"GitHub user: {data['login']}")
            return "\n".join(lines)
        if act in ("whoami", "me"):
            data, error = self._github_get_auth("/user", what="whoami")
            if error:
                return error
            return (f"{data.get('login')} ({data.get('name') or 'no display name'})\n"
                    f"public repos: {data.get('public_repos')}, "
                    f"followers: {data.get('followers')}, following: {data.get('following')}\n"
                    f"{data.get('html_url', '')}")
        if act in ("view", "user", "profile"):
            if not username:
                return "Error: a GitHub username is required."
            if not re.fullmatch(r"[\w-]+", str(username)):
                return f"Error: invalid GitHub username {username!r}."
            data, error = self._github_get(
                f"/users/{urllib.parse.quote(str(username), safe='')}",
                what="user lookup")
            if error:
                return error
            return truncate(
                f"{data.get('login')} ({data.get('name') or ''}) [{data.get('type')}]\n"
                f"bio: {data.get('bio') or ''}\n"
                f"public repos: {data.get('public_repos')}, gists: {data.get('public_gists')}, "
                f"followers: {data.get('followers')}, following: {data.get('following')}\n"
                f"company: {data.get('company') or ''}  location: {data.get('location') or ''}\n"
                f"{data.get('html_url', '')}")
        if act == "notifications":
            data, error = self._github_get_auth(
                "/notifications", {"per_page": limit}, "notifications")
            if error:
                return error
            if not data:
                return "No unread GitHub notifications."
            lines = []
            for n in data[:limit]:
                subject = n.get("subject", {})
                repo = (n.get("repository") or {}).get("full_name", "?")
                lines.append(f"- [{n.get('reason')}] {subject.get('title', '')}"
                             f"\n  {repo} ({subject.get('type')}), "
                             f"{self._gh_date(n.get('updated_at'))}")
            return truncate("\n".join(lines))
        if act == "gists":
            data, error = self._github_get_auth("/gists", {"per_page": limit}, "gists")
            if error:
                return error
            if not data:
                return "No gists found."
            lines = []
            for g in data[:limit]:
                names = ", ".join((g.get("files") or {}).keys())
                lines.append(f"- {g.get('id')} {g.get('description') or names}\n"
                             f"  {g.get('html_url', '')}")
            return truncate("\n".join(lines))
        return ("Error: github_account action must be auth_status, whoami, "
                "view, notifications or gists.")

    def github_search(self, kind: str = "repositories", query: str = "",
                      repo: str = "", max_results: int = 10) -> str:
        kind = str(kind or "repositories").strip().lower().replace("-", "_")
        kind = {"repos": "repositories", "repo": "repositories",
                "issues_and_prs": "issues"}.get(kind, kind)
        if kind not in ("repositories", "code", "issues", "users", "commits"):
            return "Error: kind must be repositories, code, issues, users or commits."
        q = str(query or "").strip()
        if not q:
            return "Error: a search query is required."
        slug = None
        if repo:
            slug, error = self._github_repo_arg(repo)
            if error:
                return error
            if "repo:" not in q:
                q += f" repo:{slug}"
        limit = self._clamp_int(max_results, 10, 1, 50)
        data, error = self._github_get(f"/search/{kind}",
                                       {"q": q, "per_page": limit},
                                       "GitHub search")
        if error:
            if kind == "code" and not self._github_token():
                error += "\nNote: code search normally requires authentication."
            return error
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            return f"No GitHub {kind} results for {query!r}."
        lines = [f"GitHub {kind} search for {query!r} "
                 f"(showing {len(items)} of {data.get('total_count', '?')}):"]
        for item in items[:limit]:
            if kind == "repositories":
                lines.append("- " + self._gh_repo_line(item))
            elif kind == "code":
                repo_name = (item.get("repository") or {}).get("full_name", slug or "?")
                lines.append(f"- {repo_name}: {item.get('path')}\n  {item.get('html_url')}")
            elif kind == "issues":
                kind_word = "PR" if "pull_request" in item else "issue"
                lines.append(f"- [{kind_word}] " + self._gh_issue_line(item))
            elif kind == "users":
                lines.append(f"- {item.get('login')} ({item.get('type')})\n"
                             f"  {item.get('html_url')}")
            else:
                commit = item.get("commit") or {}
                repo_name = (item.get("repository") or {}).get("full_name", "?")
                lines.append(f"- {repo_name} {str(item.get('sha', ''))[:8]}: "
                             f"{one_line((commit.get('message') or '').splitlines()[0] if commit.get('message') else '', 100)}\n"
                             f"  {item.get('html_url')}")
        return truncate("\n".join(lines))

    def github_repo(self, action: str = "", repo: str = "", owner: str = "",
                    name: str = "", destination: str = "", description: str = "",
                    private: bool = True, max_results: int = 20, path: str = ".") -> str:
        act = str(action or "").strip().lower().replace("-", "_")
        if not act:
            act = "clone" if destination else "create" if name else \
                "view" if repo else "list"
        limit = self._clamp_int(max_results, 20, 1, 50)
        if act in ("view", "clone", "fork"):
            slug, error = self._github_repo_arg(repo, path)
            if error:
                return error
        if act == "view":
            data, error = self._github_get(f"/repos/{slug}", what="repo lookup")
            if error:
                return error
            topics = ", ".join(data.get("topics") or [])
            lic = ((data.get("license") or {}).get("spdx_id")
                   or (data.get("license") or {}).get("name") or "")
            return truncate(
                self._gh_repo_line(data)
                + f"\ndefault branch: {data.get('default_branch')}"
                + (f"\nlicense: {lic}" if lic else "")
                + (f"\ntopics: {topics}" if topics else "")
                + f"\ncreated: {self._gh_date(data.get('created_at'))}"
                + f"\nclone: {data.get('clone_url', '')}")
        if act == "list":
            who = str(owner or "").strip()
            if not who and repo:
                slug0 = self.parse_github_repo(repo)
                who = slug0.split("/")[0] if slug0 else str(repo).strip()
            if who:
                if not re.fullmatch(r"[\w-]+", who):
                    return f"Error: invalid GitHub owner {who!r}."
                data, error = self._github_get(
                    f"/users/{urllib.parse.quote(who, safe='')}/repos",
                    {"per_page": limit, "sort": "updated"}, "repo list")
            else:
                data, error = self._github_get_auth(
                    "/user/repos", {"per_page": limit, "sort": "updated"}, "repo list")
            if error:
                return error
            if not data:
                return "No repositories found."
            return truncate("\n".join("- " + self._gh_repo_line(r) for r in data[:limit]))
        if act == "clone":
            dest = self._resolve(destination) if destination else \
                self._repo_directory(path) / slug.split("/")[1]
            if dest.exists() and any(dest.iterdir()):
                return f"Error: destination already exists and is not empty: {dest}"
            if not self.confirm("clone GitHub repository", f"{slug} -> {dest}"):
                return self._github_declined("clone a repository")
            result = self._gh_exec(["repo", "clone", slug, str(dest)], timeout=600)
            if result is not None:
                code, out, err = result
                return self._format_proc(code, out, err) if code else \
                    f"Cloned {slug} into {dest}."
            code, out, err = self._exec(
                ["git", "clone", f"https://github.com/{slug}.git", str(dest)],
                timeout=600)
            return self._format_proc(code, out, err) if code else \
                f"Cloned {slug} into {dest}."
        if act == "create":
            repo_name = str(name or "").strip() or (
                str(repo).strip().split("/")[-1] if repo else "")
            if not re.fullmatch(r"[\w.-]+", repo_name):
                return "Error: give a valid new repository name."
            is_private = self._as_bool(private, default=True)
            visibility = "private" if is_private else "public"
            if not self.confirm("create GitHub repository",
                                 f"{repo_name} ({visibility})"):
                return self._github_declined("create a repository")
            gh_result = self._gh_exec(
                ["repo", "create", repo_name,
                 "--private" if is_private else "--public"]
                + (["--description", str(description)] if description else []),
                timeout=120)
            if gh_result is not None:
                return self._github_write_result(
                    gh_result, success_hint=f"Created {visibility} repo {repo_name}.")
            if not self._github_token():
                return ("Creating a repo needs the gh CLI (`gh auth login`) or "
                        "a GITHUB_TOKEN with repo scope.")
            result = self._github_api("/user/repos", "POST", {
                "name": repo_name, "description": str(description or ""),
                "private": is_private})
            return self._github_write_result(
                None, result, f"Created {visibility} repo:")
        if act == "fork":
            if not self.confirm("fork GitHub repository", slug):
                return self._github_declined("fork a repository")
            gh_result = self._gh_exec(["repo", "fork", slug, "--clone=false"],
                                      timeout=180)
            if gh_result is not None:
                return self._github_write_result(gh_result,
                                                  success_hint=f"Forked {slug}.")
            if not self._github_token():
                return ("Forking needs the gh CLI (`gh auth login`) or a "
                        "GITHUB_TOKEN with repo scope.")
            result = self._github_api(f"/repos/{slug}/forks", "POST", {})
            return self._github_write_result(None, result, f"Forked {slug}:")
        if act == "delete":
            return ("Milelik deliberately does not delete GitHub repositories. "
                    "Do that manually in the repo's Settings if you're sure.")
        return ("Error: github_repo action must be view, list, clone, create "
                "or fork.")

    def github_issues(self, action: str = "", repo: str = "", number: int = 0,
                      title: str = "", body: str = "", state: str = "open",
                      labels="", max_results: int = 20, path: str = ".") -> str:
        act = str(action or "").strip().lower().replace("-", "_")
        try:
            number = int(number or 0)
        except (TypeError, ValueError):
            return f"Error: invalid issue number {number!r}."
        if not act:
            act = "create" if title else "comment" if number and body else \
                "view" if number else "list"
        slug, error = self._github_repo_arg(repo, path)
        if error:
            return error
        limit = self._clamp_int(max_results, 20, 1, 50)
        wanted_labels = self._split_csv(labels)
        if act == "list":
            st = str(state or "open").lower()
            if st not in ("open", "closed", "all"):
                return "Error: state must be open, closed or all."
            data, error = self._github_get(
                f"/repos/{slug}/issues",
                {"state": st, "per_page": limit,
                 "labels": ",".join(wanted_labels) if wanted_labels else None},
                "issue list")
            if error:
                return error
            issues = [i for i in data if "pull_request" not in i]
            if not issues:
                return f"No {st} issues found in {slug}."
            return truncate(f"Issues in {slug}:\n" + "\n".join(
                "- " + self._gh_issue_line(i) for i in issues[:limit]))
        if act == "view":
            if not number:
                return "Error: an issue number is required."
            data, error = self._github_get(f"/repos/{slug}/issues/{number}",
                                           what="issue lookup")
            if error:
                return error
            return truncate(self._gh_issue_line(data)
                            + f"\n\n{data.get('body') or '(no description)'}", 6000)
        if act == "create":
            if not str(title or "").strip():
                return "Error: an issue title is required."
            detail = f"{slug}: {one_line(title, 100)}"
            if not self.confirm("create GitHub issue", detail):
                return self._github_declined("create an issue")
            gh_args = ["issue", "create", "--repo", slug, "--title", str(title),
                       "--body", str(body or "")]
            for lab in wanted_labels:
                gh_args += ["--label", lab]
            gh_result = self._gh_exec(gh_args, timeout=120)
            if gh_result is not None:
                return self._github_write_result(gh_result)
            if not self._github_token():
                return "Creating issues needs `gh auth login` or a GITHUB_TOKEN."
            result = self._github_api(f"/repos/{slug}/issues", "POST", {
                "title": str(title), "body": str(body or ""),
                "labels": wanted_labels})
            return self._github_write_result(None, result, "Created issue:")
        if act in ("comment", "close", "reopen"):
            if not number:
                return "Error: an issue number is required."
            if act == "comment" and not str(body or "").strip():
                return "Error: comment body text is required."
            label = {"comment": "comment on GitHub issue",
                     "close": "close GitHub issue",
                     "reopen": "reopen GitHub issue"}[act]
            if not self.confirm(label, f"{slug}#{number}"):
                return self._github_declined(label.split(" GitHub")[0] + " an issue")
            if act == "comment":
                gh_result = self._gh_exec(
                    ["issue", "comment", str(number), "--repo", slug,
                     "--body", str(body)], timeout=120)
                api = None
                if gh_result is None:
                    if not self._github_token():
                        return "Commenting needs `gh auth login` or a GITHUB_TOKEN."
                    api = self._github_api(
                        f"/repos/{slug}/issues/{number}/comments", "POST",
                        {"body": str(body)})
                return self._github_write_result(gh_result, api, "Comment posted:")
            gh_result = self._gh_exec(
                ["issue", act, str(number), "--repo", slug], timeout=120)
            api = None
            if gh_result is None:
                if not self._github_token():
                    return "This needs `gh auth login` or a GITHUB_TOKEN."
                api = self._github_api(
                    f"/repos/{slug}/issues/{number}", "PATCH",
                    {"state": "closed" if act == "close" else "open"})
            return self._github_write_result(
                gh_result, api, f"Issue {slug}#{number} {'closed' if act == 'close' else 'reopened'}:")
        return ("Error: github_issues action must be list, view, create, "
                "comment, close or reopen.")

    def github_pull_requests(self, action: str = "", repo: str = "", number: int = 0,
                             title: str = "", body: str = "", base: str = "",
                             head: str = "", state: str = "open",
                             merge_method: str = "merge", draft: bool = False,
                             max_results: int = 20, path: str = ".") -> str:
        act = str(action or "").strip().lower().replace("-", "_")
        try:
            number = int(number or 0)
        except (TypeError, ValueError):
            return f"Error: invalid pull request number {number!r}."
        if not act:
            act = "create" if title else "comment" if number and body else \
                "view" if number else "list"
        slug, error = self._github_repo_arg(repo, path)
        if error:
            return error
        limit = self._clamp_int(max_results, 20, 1, 50)
        if act == "list":
            st = str(state or "open").lower()
            if st not in ("open", "closed", "all"):
                return "Error: state must be open, closed or all."
            data, error = self._github_get(
                f"/repos/{slug}/pulls",
                {"state": st, "per_page": limit, "sort": "updated",
                 "direction": "desc"}, "pull request list")
            if error:
                return error
            if not data:
                return f"No {st} pull requests found in {slug}."
            return truncate(f"Pull requests in {slug}:\n" + "\n".join(
                "- " + self._gh_pr_line(p) for p in data[:limit]))
        if act in ("view", "checks", "diff", "files"):
            if not number:
                return "Error: a pull request number is required."
            data, error = self._github_get(f"/repos/{slug}/pulls/{number}",
                                           what="pull request lookup")
            if error:
                return error
            if act == "view":
                extra = (f"\nmergeable: {data.get('mergeable')} "
                         f"({data.get('mergeable_state')}), "
                         f"+{data.get('additions')}/-{data.get('deletions')} "
                         f"in {data.get('changed_files')} files, "
                         f"{data.get('commits')} commits")
                return truncate(self._gh_pr_line(data) + extra
                                + f"\n\n{data.get('body') or '(no description)'}", 6000)
            if act == "files":
                files, error = self._github_get(
                    f"/repos/{slug}/pulls/{number}/files", {"per_page": 100},
                    "pull request files")
                if error:
                    return error
                return truncate("\n".join(
                    f"- {f.get('filename')} [{f.get('status')}] "
                    f"+{f.get('additions')}/-{f.get('deletions')}" for f in files))
            if act == "diff":
                status, _d, raw = self._github_api(
                    f"/repos/{slug}/pulls/{number}",
                    accept="application/vnd.github.diff")
                error = self._github_error(status, _d, raw, "pull request diff")
                return error or truncate(raw)
            sha = ((data.get("head") or {}).get("sha") or "")
            checks, error = self._github_get(
                f"/repos/{slug}/commits/{sha}/check-runs", {"per_page": 100},
                "pull request checks")
            if error:
                return error
            runs = checks.get("check_runs", []) if isinstance(checks, dict) else []
            if not runs:
                return f"No checks reported for {slug}#{number} (head {sha[:8]})."
            lines = [f"Checks for {slug}#{number}:"]
            for r in runs:
                state_text = r.get("conclusion") or r.get("status")
                lines.append(f"- {r.get('name')}: {state_text}\n  {r.get('html_url', '')}")
            return truncate("\n".join(lines))
        if act == "create":
            if not str(title or "").strip():
                return "Error: a pull request title is required."
            head_branch = str(head or "").strip()
            if not head_branch:
                code, out, _e = self._git(["branch", "--show-current"], path)
                head_branch = out.strip() if code == 0 else ""
            if not head_branch:
                return ("Error: could not determine the head branch. Pass "
                        "head=\"my-branch\" (it must already be pushed).")
            if not self._valid_branch(head_branch):
                return f"Error: invalid head branch {head_branch!r}."
            base_branch = str(base or "").strip()
            if not base_branch:
                repo_data, _err = self._github_get(f"/repos/{slug}",
                                                   what="repo lookup")
                base_branch = (repo_data or {}).get("default_branch") or "main"
            detail = f"{slug}: {head_branch} -> {base_branch} - {one_line(title, 90)}"
            if not self.confirm("create GitHub pull request", detail):
                return self._github_declined("create a pull request")
            gh_args = ["pr", "create", "--repo", slug, "--title", str(title),
                       "--body", str(body or ""), "--base", base_branch,
                       "--head", head_branch]
            if self._as_bool(draft):
                gh_args.append("--draft")
            gh_result = self._gh_exec(gh_args, timeout=180,
                                      cwd=str(self._repo_directory(path)))
            if gh_result is not None:
                return self._github_write_result(gh_result)
            if not self._github_token():
                return "Creating PRs needs `gh auth login` or a GITHUB_TOKEN."
            result = self._github_api(f"/repos/{slug}/pulls", "POST", {
                "title": str(title), "body": str(body or ""),
                "head": head_branch, "base": base_branch,
                "draft": self._as_bool(draft)})
            return self._github_write_result(None, result, "Created pull request:")
        if act in ("comment", "close", "reopen", "merge"):
            if not number:
                return "Error: a pull request number is required."
            if act == "comment":
                if not str(body or "").strip():
                    return "Error: comment body text is required."
                if not self.confirm("comment on GitHub pull request",
                                     f"{slug}#{number}"):
                    return self._github_declined("comment on a pull request")
                gh_result = self._gh_exec(
                    ["pr", "comment", str(number), "--repo", slug,
                     "--body", str(body)], timeout=120)
                api = None
                if gh_result is None:
                    if not self._github_token():
                        return "Commenting needs `gh auth login` or a GITHUB_TOKEN."
                    api = self._github_api(
                        f"/repos/{slug}/issues/{number}/comments", "POST",
                        {"body": str(body)})
                return self._github_write_result(gh_result, api, "Comment posted:")
            if act in ("close", "reopen"):
                if not self.confirm(f"{act} GitHub pull request",
                                     f"{slug}#{number}"):
                    return self._github_declined(f"{act} a pull request")
                gh_result = self._gh_exec(
                    ["pr", act, str(number), "--repo", slug], timeout=120)
                api = None
                if gh_result is None:
                    if not self._github_token():
                        return "This needs `gh auth login` or a GITHUB_TOKEN."
                    api = self._github_api(
                        f"/repos/{slug}/pulls/{number}", "PATCH",
                        {"state": "closed" if act == "close" else "open"})
                return self._github_write_result(
                    gh_result, api, f"Pull request {slug}#{number} {'closed' if act == 'close' else 'reopened'}:")
            method = str(merge_method or "merge").strip().lower()
            if method not in ("merge", "squash", "rebase"):
                return "Error: merge_method must be merge, squash or rebase."
            if not self.confirm(
                    "merge GitHub pull request", f"{slug}#{number} via {method}",
                    dangerous_reason="merging code into the target branch"):
                return self._github_declined("merge a pull request")
            gh_result = self._gh_exec(
                ["pr", "merge", str(number), "--repo", slug, f"--{method}"],
                timeout=180)
            api = None
            if gh_result is None:
                if not self._github_token():
                    return "Merging needs `gh auth login` or a GITHUB_TOKEN."
                api = self._github_api(f"/repos/{slug}/pulls/{number}/merge",
                                       "PUT", {"merge_method": method})
            return self._github_write_result(
                gh_result, api, f"Merged {slug}#{number} via {method}.")
        return ("Error: github_pull_requests action must be list, view, checks, "
                "diff, files, create, comment, close, reopen or merge.")

    def github_actions(self, action: str = "list_runs", repo: str = "",
                       run_id: str = "", workflow: str = "", branch: str = "",
                       max_results: int = 20, path: str = ".") -> str:
        act = str(action or "list_runs").strip().lower().replace("-", "_")
        slug, error = self._github_repo_arg(repo, path)
        if error:
            return error
        limit = self._clamp_int(max_results, 20, 1, 50)
        run_id = str(run_id or "").strip()
        if act == "list_workflows":
            data, error = self._github_get(
                f"/repos/{slug}/actions/workflows", {"per_page": limit},
                "workflow list")
            if error:
                return error
            flows = data.get("workflows", []) if isinstance(data, dict) else []
            if not flows:
                return f"No GitHub Actions workflows found in {slug}."
            return "\n".join(
                f"- {w.get('name')} (id {w.get('id')}, {w.get('state')}) "
                f"{w.get('path')}" for w in flows[:limit])
        if act == "list_runs":
            params = {"per_page": limit}
            if branch:
                params["branch"] = branch
            endpoint = (f"/repos/{slug}/actions/workflows/"
                        f"{urllib.parse.quote(str(workflow), safe='')}/runs"
                        if workflow else f"/repos/{slug}/actions/runs")
            data, error = self._github_get(endpoint, params, "workflow runs")
            if error:
                return error
            runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
            if not runs:
                return f"No workflow runs found in {slug}."
            return truncate("\n".join("- " + self._gh_run_line(r) for r in runs[:limit]))
        if act in ("view_run", "jobs", "logs", "rerun", "cancel"):
            if not run_id.isdigit():
                return "Error: a numeric run_id is required (see list_runs)."
            if act == "view_run":
                data, error = self._github_get(
                    f"/repos/{slug}/actions/runs/{run_id}", what="workflow run")
                if error:
                    return error
                return self._gh_run_line(data) + \
                    f"\nactor: {(data.get('actor') or {}).get('login')}" \
                    f"\nstarted: {data.get('run_started_at')}"
            if act == "jobs":
                data, error = self._github_get(
                    f"/repos/{slug}/actions/runs/{run_id}/jobs",
                    {"per_page": 100}, "workflow jobs")
                if error:
                    return error
                jobs = data.get("jobs", []) if isinstance(data, dict) else []
                lines = []
                for j in jobs:
                    state_text = j.get("conclusion") or j.get("status")
                    lines.append(f"- {j.get('name')}: {state_text}")
                    for s in j.get("steps", []):
                        lines.append(f"    · {s.get('name')}: "
                                     f"{s.get('conclusion') or s.get('status')}")
                return truncate("\n".join(lines) or "No jobs found for that run.")
            if act == "logs":
                result = self._gh_exec(
                    ["run", "view", run_id, "--repo", slug, "--log"], timeout=180)
                if result is None:
                    return ("Run logs need the gh CLI (the API returns them as "
                            "a zip). Install it with "
                            f"{dependency_install_command()}, then run "
                            "`gh auth login`.")
                return self._format_proc(*result)
            if not self.confirm(f"{act} GitHub Actions run",
                                 f"{slug} run {run_id}"):
                return self._github_declined(f"{act} a workflow run")
            gh_result = self._gh_exec(
                ["run", act, run_id, "--repo", slug], timeout=120)
            api = None
            if gh_result is None:
                if not self._github_token():
                    return "This needs `gh auth login` or a GITHUB_TOKEN."
                api = self._github_api(
                    f"/repos/{slug}/actions/runs/{run_id}/"
                    f"{'rerun' if act == 'rerun' else 'cancel'}", "POST", {})
            return self._github_write_result(
                gh_result, api, f"Run {run_id}: {act} requested.")
        if act in ("dispatch", "run_workflow"):
            if not workflow:
                return "Error: workflow (file name or id) is required."
            ref = str(branch or "").strip()
            if not ref:
                code, out, _e = self._git(["branch", "--show-current"], path)
                ref = out.strip() if code == 0 and out.strip() else "main"
            if not self.confirm("run GitHub Actions workflow",
                                 f"{slug}: {workflow} on {ref}"):
                return self._github_declined("run a workflow")
            gh_result = self._gh_exec(
                ["workflow", "run", str(workflow), "--repo", slug, "--ref", ref],
                timeout=120)
            api = None
            if gh_result is None:
                if not self._github_token():
                    return "This needs `gh auth login` or a GITHUB_TOKEN."
                api = self._github_api(
                    f"/repos/{slug}/actions/workflows/"
                    f"{urllib.parse.quote(str(workflow), safe='')}/dispatches",
                    "POST", {"ref": ref})
            return self._github_write_result(
                gh_result, api, f"Workflow {workflow} dispatched on {ref}.")
        return ("Error: github_actions action must be list_workflows, "
                "list_runs, view_run, jobs, logs, rerun, cancel or dispatch.")

    def github_releases(self, action: str = "list", repo: str = "", tag: str = "",
                        max_results: int = 10, path: str = ".", title: str = "",
                        body: str = "", draft: bool = True,
                        prerelease: bool = False, target: str = "",
                        file_path: str = "") -> str:
        act = str(action or "list").strip().lower().replace("-", "_")
        slug, error = self._github_repo_arg(repo, path)
        if error:
            return error
        limit = self._clamp_int(max_results, 10, 1, 50)

        def fmt(r):
            lines = [f"{r.get('tag_name')} - {r.get('name') or ''} "
                     f"[{self._gh_date(r.get('published_at'))}]"
                     + (" (prerelease)" if r.get("prerelease") else ""),
                     str(r.get("html_url", ""))]
            assets = r.get("assets") or []
            if assets:
                lines.append("assets:")
                for a in assets[:20]:
                    size = a.get("size", 0)
                    human = f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 \
                        else f"{size} bytes"
                    lines.append(f"  - {a.get('name')} ({human}, "
                                 f"{a.get('download_count')} downloads)")
            if r.get("body"):
                lines.append(one_line(r["body"], 500))
            return "\n".join(lines)

        if act == "list":
            data, error = self._github_get(f"/repos/{slug}/releases",
                                           {"per_page": limit}, "releases")
            if error:
                return error
            if not data:
                return f"No releases found in {slug}."
            return truncate("\n\n".join(fmt(r) for r in data[:limit]))
        if act in ("latest", "view"):
            endpoint = f"/repos/{slug}/releases/latest" if act == "latest" or not tag \
                else f"/repos/{slug}/releases/tags/" \
                     f"{urllib.parse.quote(str(tag), safe='')}"
            data, error = self._github_get(endpoint, what="release lookup")
            if error:
                return error
            return truncate(fmt(data))
        if act == "create":
            if not str(tag or "").strip():
                return "Error: a release tag is required (e.g. v1.0.0)."
            if not re.fullmatch(r"[\w.+/-]+", str(tag).strip()):
                return f"Error: invalid release tag {tag!r}."
            is_draft = self._as_bool(draft, default=True)
            detail = f"{slug} release {tag}" + (" (draft)" if is_draft else "")
            if not self.confirm("create GitHub release", detail):
                return self._github_declined("create a release")
            gh_args = ["release", "create", str(tag).strip(), "--repo", slug,
                       "--title", str(title or tag), "--notes", str(body or "")]
            if is_draft:
                gh_args.append("--draft")
            if self._as_bool(prerelease):
                gh_args.append("--prerelease")
            if target:
                gh_args += ["--target", str(target)]
            gh_result = self._gh_exec(gh_args, timeout=180)
            if gh_result is not None:
                return self._github_write_result(gh_result)
            if not self._github_token():
                return "Creating releases needs `gh auth login` or a GITHUB_TOKEN."
            payload = {"tag_name": str(tag).strip(), "name": str(title or tag),
                       "body": str(body or ""), "draft": is_draft,
                       "prerelease": self._as_bool(prerelease)}
            if target:
                payload["target_commitish"] = str(target)
            result = self._github_api(f"/repos/{slug}/releases", "POST", payload)
            return self._github_write_result(None, result, "Created release:")
        if act == "upload":
            if not str(tag or "").strip():
                return "Error: the release tag to upload to is required."
            asset = self._resolve(file_path or "")
            if not asset.is_file():
                return f"Error: asset file not found: {asset}"
            if not self.confirm("upload a GitHub release asset",
                                 f"{asset} -> {slug} {tag}"):
                return self._github_declined("upload a release asset")
            gh_result = self._gh_exec(
                ["release", "upload", str(tag).strip(), str(asset),
                 "--repo", slug], timeout=900)
            if gh_result is None:
                return ("Uploading release assets needs the gh CLI (run "
                        f"{dependency_install_command()}, then "
                        "`gh auth login`).")
            return self._github_write_result(
                gh_result, success_hint=f"Uploaded {asset.name} to release {tag}.")
        return ("Error: github_releases action must be list, latest, view, "
                "create or upload.")

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
            if name == "list_apps":
                return self.list_apps(args.get("query", ""), args.get("max_results", 30))
            if name == "open_app":
                return self.open_app(args.get("app", ""), args.get("target", ""))
            if name == "open_target":
                return self.open_target(args.get("target", ""), args.get("application", ""))
            if name == "open_terminal":
                return self.open_terminal(args.get("path", "."), args.get("command", ""))
            if name == "vlc":
                return self.vlc(args.get("action", ""), args.get("source", ""),
                                args.get("value", ""), args.get("fullscreen", False),
                                args.get("start_paused", False))
            if name == "media_player":
                return self.media_player(args.get("action", "status"),
                                         args.get("player", ""), args.get("value", ""))
            if name == "media_info":
                return self.media_info(args.get("path", ""))
            if name == "desktop_control":
                return self.desktop_control(args.get("action", ""), args.get("value", ""),
                                            args.get("title", "Milelik"),
                                            args.get("path", ""))
            if name == "office_document":
                return self.office_document(args.get("action", ""), args.get("path", ""),
                                            args.get("format", ""), args.get("app", ""),
                                            args.get("output_dir", ""))
            if name == "git_repo":
                return self.git_repo(args.get("action", "status"), args.get("path", "."),
                                     args.get("message", ""), args.get("branch", ""),
                                     args.get("remote", "origin"), args.get("files", ""),
                                     args.get("max_results", 10))
            if name == "github_search":
                return self.github_search(args.get("kind", "repositories"),
                                          args.get("query", ""), args.get("repo", ""),
                                          args.get("max_results", 10))
            if name == "github_repo":
                return self.github_repo(args.get("action", ""), args.get("repo", ""),
                                        args.get("owner", ""), args.get("name", ""),
                                        args.get("destination", ""),
                                        args.get("description", ""),
                                        args.get("private", True),
                                        args.get("max_results", 20),
                                        args.get("path", "."))
            if name == "github_issues":
                return self.github_issues(args.get("action", ""), args.get("repo", ""),
                                          args.get("number", 0), args.get("title", ""),
                                          args.get("body", ""), args.get("state", "open"),
                                          args.get("labels", ""),
                                          args.get("max_results", 20),
                                          args.get("path", "."))
            if name == "github_pull_requests":
                return self.github_pull_requests(
                    args.get("action", ""), args.get("repo", ""), args.get("number", 0),
                    args.get("title", ""), args.get("body", ""), args.get("base", ""),
                    args.get("head", ""), args.get("state", "open"),
                    args.get("merge_method", "merge"), args.get("draft", False),
                    args.get("max_results", 20), args.get("path", "."))
            if name == "github_actions":
                return self.github_actions(args.get("action", "list_runs"),
                                           args.get("repo", ""), args.get("run_id", ""),
                                           args.get("workflow", ""),
                                           args.get("branch", ""),
                                           args.get("max_results", 20),
                                           args.get("path", "."))
            if name == "github_releases":
                return self.github_releases(args.get("action", "list"),
                                            args.get("repo", ""), args.get("tag", ""),
                                            args.get("max_results", 10),
                                            args.get("path", "."),
                                            args.get("title", ""), args.get("body", ""),
                                            args.get("draft", True),
                                            args.get("prerelease", False),
                                            args.get("target", ""),
                                            args.get("file_path", ""))
            if name == "github_account":
                return self.github_account(args.get("action", "auth_status"),
                                           args.get("username", ""),
                                           args.get("max_results", 10))
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
    {"type": "function", "function": {
        "name": "list_apps",
        "description": "List installed desktop applications (from .desktop files), optionally filtered by a search word. Use this when unsure of an app's exact name/id.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Optional filter, e.g. 'video', 'editor', 'browser'"},
            "max_results": {"type": "integer"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "open_app",
        "description": "Open/launch a desktop application by name, desktop id or executable (e.g. VLC, Firefox, Files/Nemo, Text Editor/Xed, LibreOffice, Calculator, Settings). Optional target file/URL to open in it. Requires confirmation.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string"},
            "target": {"type": "string", "description": "Optional file, folder or URL"}},
            "required": ["app"]}}},
    {"type": "function", "function": {
        "name": "open_target",
        "description": "Open a file, folder or URL with the default application (xdg-open): web pages in the browser, folders in the file manager, PDFs/images/documents in their viewers. Optional application overrides the default. Requires confirmation.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"},
            "application": {"type": "string", "description": "Optional app name/id instead of the default"}},
            "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "open_terminal",
        "description": "Open a terminal emulator window, optionally in a directory and/or running a command that stays open afterwards. Requires confirmation.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory, default current"},
            "command": {"type": "string", "description": "Optional command to run in it"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "vlc",
        "description": "Control VLC media player. action: play (source=file/URL; optional fullscreen, start_paused), enqueue, status/now_playing, play,pause,play_pause,stop,next,previous, seek_forward/seek_backward/seek_to (value=seconds), volume_set/volume_up/volume_down/mute (value=percent), quit. Playback control uses playerctl/MPRIS.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "source": {"type": "string", "description": "Media file path or stream URL for play/enqueue"},
            "value": {"type": "string", "description": "Seconds or percent for seek/volume"},
            "fullscreen": {"type": "boolean"},
            "start_paused": {"type": "boolean"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "media_player",
        "description": "Control any MPRIS media player (VLC, Celluloid, Rhythmbox, Spotify, browsers) via playerctl. action: status, list_players, play, pause, play_pause, stop, next, previous, seek_forward, seek_backward, seek_to, volume_set, volume_up, volume_down, mute. player optionally names one player; value is seconds/percent.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "player": {"type": "string"},
            "value": {"type": "string"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "media_info",
        "description": "Show duration, format, codecs, resolution/fps and size of a local audio/video file (ffprobe/mediainfo).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "desktop_control",
        "description": "Desktop helpers. action: notify (title + value=message), screenshot (path optional, default ~/Pictures), clipboard_get, clipboard_set (value=text), volume_get/volume_set/volume_increase/volume_decrease/volume_mute_toggle (value=percent), brightness_get/brightness_set/brightness_increase/brightness_decrease (value=percent), battery, lock_screen, wallpaper (path=image). Screenshot/clipboard/lock/wallpaper require confirmation.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "value": {"type": "string"},
            "title": {"type": "string"},
            "path": {"type": "string"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "office_document",
        "description": "LibreOffice documents. action=open opens a document (app=writer/calc/impress/draw optional). action=convert converts path to format (pdf, docx, odt, txt, csv, html...) headlessly, optionally into output_dir.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "description": "open or convert"},
            "path": {"type": "string"},
            "format": {"type": "string"},
            "app": {"type": "string"},
            "output_dir": {"type": "string"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "git_repo",
        "description": "Local Git operations in a repo directory (path). Read actions: status, diff, diff_staged, diff_stat, log, branches, remotes, current_branch. Write actions (confirmation): init, add (files='a.py,b.py'), commit (message required), push, pull, fetch, checkout (branch), create_branch (branch), stash, stash_pop.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "path": {"type": "string", "description": "Repo directory, default current"},
            "message": {"type": "string"},
            "branch": {"type": "string"},
            "remote": {"type": "string"},
            "files": {"type": "string", "description": "Comma-separated files for add"},
            "max_results": {"type": "integer"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "github_search",
        "description": "Search GitHub. kind: repositories, code (optionally within repo), issues, users or commits. query uses GitHub search syntax.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string"},
            "query": {"type": "string"},
            "repo": {"type": "string", "description": "Optional owner/repo to search within"},
            "max_results": {"type": "integer"}},
            "required": ["kind", "query"]}}},
    {"type": "function", "function": {
        "name": "github_repo",
        "description": "GitHub repositories. action: view, list (owner, or your repos if owner omitted), clone (repo + destination), create (name, private default true, description), fork. Write actions require confirmation; repo deletion is not supported. repo is owner/repo; omit it to use the current directory's origin remote where relevant.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "repo": {"type": "string"},
            "owner": {"type": "string"},
            "name": {"type": "string"},
            "destination": {"type": "string"},
            "description": {"type": "string"},
            "private": {"type": "boolean"},
            "max_results": {"type": "integer"},
            "path": {"type": "string"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "github_issues",
        "description": "GitHub issues in a repo (repo=owner/repo, or current repo if omitted). action: list (state open/closed/all, labels filter), view (number), create (title, body, labels comma-separated), comment (number, body), close, reopen. Write actions require confirmation and GitHub auth (gh login or GITHUB_TOKEN).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "repo": {"type": "string"},
            "number": {"type": "integer"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "state": {"type": "string"},
            "labels": {"type": "string"},
            "max_results": {"type": "integer"},
            "path": {"type": "string"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "github_pull_requests",
        "description": "GitHub pull requests in a repo (repo=owner/repo, or current repo if omitted). action: list, view, checks, files, diff, create (title, body, head branch - default current branch, base - default repo default branch, draft), comment (number, body), close, reopen, merge (merge_method merge/squash/rebase; requires typing YES). Write actions require auth.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "repo": {"type": "string"},
            "number": {"type": "integer"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "base": {"type": "string"},
            "head": {"type": "string"},
            "state": {"type": "string"},
            "merge_method": {"type": "string"},
            "draft": {"type": "boolean"},
            "max_results": {"type": "integer"},
            "path": {"type": "string"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "github_actions",
        "description": "GitHub Actions CI in a repo. action: list_workflows, list_runs (branch/workflow optional), view_run (run_id), jobs (run_id), logs (run_id, needs gh CLI), rerun, cancel, dispatch (workflow + branch=ref). Rerun/cancel/dispatch require confirmation and auth.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "repo": {"type": "string"},
            "run_id": {"type": "string"},
            "workflow": {"type": "string"},
            "branch": {"type": "string"},
            "max_results": {"type": "integer"},
            "path": {"type": "string"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "github_releases",
        "description": "GitHub releases in a repo. action: list, latest, view (tag), create (tag, title, body; draft defaults TRUE for safety, prerelease/target optional), upload (tag + file_path asset, needs gh). Create/upload require confirmation and auth.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "repo": {"type": "string"},
            "tag": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "draft": {"type": "boolean"},
            "prerelease": {"type": "boolean"},
            "target": {"type": "string", "description": "Branch/commit for a new tag"},
            "file_path": {"type": "string", "description": "Local asset file for upload"},
            "max_results": {"type": "integer"},
            "path": {"type": "string"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "github_account",
        "description": "GitHub account info. action: auth_status (is gh/GITHUB_TOKEN set up? never shows token values), whoami, view (username profile), notifications, gists.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"},
            "username": {"type": "string"},
            "max_results": {"type": "integer"}},
            "required": ["action"]}}},
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
    "list_applications": "list_apps", "find_app": "list_apps", "installed_apps": "list_apps",
    "launch_app": "open_app", "open_application": "open_app", "start_app": "open_app",
    "open_in_browser": "open_target", "open_browser": "open_target",
    "open_folder": "open_target", "open_file_manager": "open_target",
    "file_manager": "open_target", "open_pdf": "open_target", "open_image": "open_target",
    "terminal_open": "open_terminal", "launch_terminal": "open_terminal",
    "vlc_play": "vlc", "vlc_control": "vlc", "play_video": "vlc", "play_media": "vlc",
    "media_control": "media_player", "now_playing": "media_player",
    "player_control": "media_player", "video_info": "media_info",
    "audio_info": "media_info", "ffprobe": "media_info",
    "desktop": "desktop_control", "notify": "desktop_control",
    "screenshot": "desktop_control", "take_screenshot": "desktop_control",
    "libreoffice": "office_document", "convert_document": "office_document",
    "office": "office_document",
    "git": "git_repo", "git_status": "git_repo",
    "search_github": "github_search", "github_code_search": "github_search",
    "github_repository": "github_repo", "github_repos": "github_repo",
    "github_issue": "github_issues", "list_issues": "github_issues",
    "create_issue": "github_issues",
    "github_pr": "github_pull_requests", "github_pull_request": "github_pull_requests",
    "pull_requests": "github_pull_requests", "create_pr": "github_pull_requests",
    "github_ci": "github_actions", "workflow_runs": "github_actions",
    "github_release": "github_releases", "releases": "github_releases",
    "github_user": "github_account", "github_whoami": "github_account",
    "github_notifications": "github_account",
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
    "list_apps": "query", "open_app": "app", "open_target": "target",
    "open_terminal": "path", "vlc": "source", "media_player": "action",
    "media_info": "path", "desktop_control": "action",
    "office_document": "path", "git_repo": "action",
    "github_search": "query", "github_repo": "repo",
    "github_issues": "repo", "github_pull_requests": "repo",
    "github_actions": "repo", "github_releases": "repo",
    "github_account": "action",
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
    "list_apps": {
        "query": ("query", "search", "filter", "name")},
    "open_app": {
        "app": ("app", "application", "name", "program", "desktop_id", "id"),
        "target": ("target", "path", "file", "url", "argument")},
    "open_target": {
        "target": ("target", "path", "file", "url", "folder", "directory"),
        "application": ("application", "app", "with", "program")},
    "open_terminal": {
        "path": ("path", "directory", "dir", "cwd", "folder"),
        "command": ("command", "cmd", "run")},
    "vlc": {
        "source": ("source", "path", "file", "url", "media", "video")},
    "media_player": {
        "player": ("player", "name", "app")},
    "media_info": {
        "path": ("path", "file", "source", "media")},
    "desktop_control": {
        "value": ("value", "message", "text", "percent", "level"),
        "path": ("path", "file", "image", "output")},
    "office_document": {
        "path": ("path", "file", "document"),
        "format": ("format", "to", "output_format", "type")},
    "git_repo": {
        "path": ("path", "repo", "repository", "directory", "dir")},
    "github_search": {
        "query": ("query", "q", "search", "term"),
        "repo": ("repo", "repository", "full_name")},
    "github_repo": {
        "repo": ("repo", "repository", "full_name", "slug")},
    "github_issues": {
        "repo": ("repo", "repository", "full_name"),
        "number": ("number", "issue_number", "id")},
    "github_pull_requests": {
        "repo": ("repo", "repository", "full_name"),
        "number": ("number", "pr_number", "pull_request_number", "id")},
    "github_actions": {
        "repo": ("repo", "repository", "full_name"),
        "run_id": ("run_id", "id", "run")},
    "github_releases": {
        "repo": ("repo", "repository", "full_name"),
        "tag": ("tag", "version", "release"),
        "file_path": ("file_path", "file", "asset")},
    "github_account": {
        "username": ("username", "user", "login", "owner")},
}

_ALIAS_DEFAULT_ARGS = {
    "notify": {"action": "notify"},
    "screenshot": {"action": "screenshot"},
    "take_screenshot": {"action": "screenshot"},
    "play_video": {"action": "play"},
    "play_media": {"action": "play"},
    "vlc_play": {"action": "play"},
    "now_playing": {"action": "status"},
    "convert_document": {"action": "convert"},
    "git_status": {"action": "status"},
    "create_issue": {"action": "create"},
    "list_issues": {"action": "list"},
    "create_pr": {"action": "create"},
    "github_whoami": {"action": "whoami"},
    "github_notifications": {"action": "notifications"},
}

def normalise_tool_arguments(name: str, args) -> dict:
    """Coerce whatever a small model produced into the expected arguments."""
    raw_key = re.sub(r"[\s-]+", "_", str(name or "").strip().lower()).strip("_")
    if "." in raw_key:
        raw_key = raw_key.rsplit(".", 1)[-1]
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
    for _k, _v in _ALIAS_DEFAULT_ARGS.get(raw_key, {}).items():
        out.setdefault(_k, _v)
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
    "list_apps": ("query", "max_results"), "open_app": ("app", "target"),
    "open_target": ("target", "application"),
    "open_terminal": ("path", "command"),
    "vlc": ("action", "source", "value"),
    "media_player": ("action", "player", "value"),
    "media_info": ("path",),
    "desktop_control": ("action", "value", "title", "path"),
    "office_document": ("action", "path", "format"),
    "git_repo": ("action", "path"),
    "github_search": ("kind", "query", "repo"),
    "github_repo": ("action", "repo"),
    "github_issues": ("action", "repo", "number"),
    "github_pull_requests": ("action", "repo", "number"),
    "github_actions": ("action", "repo", "run_id"),
    "github_releases": ("action", "repo", "tag"),
    "github_account": ("action", "username"),
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
        # Low temperature / tight sampling makes small local models far more
        # reliable at emitting exactly-formatted tool calls.
        self.temperature = 0.1 if is_qwen25_coder(self.model) or \
            is_bundled_default_model(self.model) else temperature
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
                f"Is it running? Try:  ollama serve    (or python3 setup_milelik.py)")

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
            err("`ollama` command not found. Run python3 setup_milelik.py first.")
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
        if is_qwen25_coder(self.model) or is_bundled_default_model(self.model):
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
            or is_bundled_default_model(model) else temperature
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
        print(f"Either load {BUNDLED_MODEL_FILENAME} in LM Studio, or use Ollama "
              "with Milelik's bundled model:\n"
              f"  milelik --import-gguf models/{BUNDLED_MODEL_FILENAME}")
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
                (not self.model or is_target_model(self.model)
                 or is_bundled_default_model(self.model)):
            # Prefer the server's exact id for the loaded GGUF.
            self.resolve_available_model(self.model or BUNDLED_MODEL_LABEL,
                                         update=True)
        payload = {
            "model": self.model,
            "messages": self._messages_for_openai(messages),
            "stream": False,
            "temperature": self.temperature,
        }
        if is_qwen25_coder(self.model) or is_target_model(self.model) or \
                is_bundled_default_model(self.model):
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
    tool_names = ", ".join(TOOL_NAMES)
    qwen_block = ""
    if is_qwen25_coder(model) or is_target_model(model) or \
            is_bundled_default_model(model):
        # Small local models need the tool format stated bluntly (and one
        # call per reply), or they tend to narrate a plan and emit several
        # speculative calls at once.
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
- Valid tool names are only: __TOOL_NAMES__. The "arguments" value must be a JSON object.
- Do NOT write things like "I will now run..." without a tool call - either emit the <tool_call> or give the final answer.""".replace("__TOOL_NAMES__", tool_names)
    return f"""You are Milelik, a helpful local AI assistant running on the user's {distro_name()} machine.
Today is {today}. User: {getpass.getuser()} on {socket.gethostname()}. Working directory: {toolbox.cwd}. Shell: bash.

You have tools: {tool_names}.

Rules:
- Prefer a dedicated tool over run_command when one exists: vlc / media_player for playback, list_apps / open_app / open_target / open_terminal for desktop apps and files, desktop_control for notifications/screenshots/volume/clipboard, office_document for LibreOffice, and the git_repo / github_* tools for Git and GitHub. Use list_apps first if unsure whether/how an app is installed.
- For GitHub, repo can usually be omitted inside a cloned repo (it defaults to the origin remote). NEVER close, merge, create, comment, push or rerun anything unless the user has explicitly asked for that action; read-only lookups are fine. Never ask the user to paste a GitHub token into chat - auth is `gh auth login` or the GITHUB_TOKEN environment variable.
- To inspect or change the system in ways no dedicated tool covers, call run_command. To get current/external information, call web_search or fetch_url. NEVER invent command output, file contents, app/GitHub state, or web results - call a tool instead.
- Use short, non-interactive commands (no nano/vim/htop; use flags like --no-pager, -b, -n 1). Avoid sudo unless essential, and prefer apt-get over apt in scripts.
- One logical step at a time: look at each tool result before deciding the next step. If a command fails, read the error and adapt; don't repeat the exact same failing call.
- If the user declines a command, respect that - suggest an alternative, don't just retry it.
- When the task is done, answer directly and concisely. Summarise what you ran/found, and cite URLs you actually fetched or found in search results.{qwen_block}

If (and only if) native tool calling is unavailable, request a tool by replying with ONLY this, nothing else:
<tool_call>
{{"name": "run_command", "arguments": {{"command": "df -h"}}}}
</tool_call>"""

class Spinner:
    FRAMES = "|/-\\"

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
        # Small models reason much better seeing one result at a time than
        # firing off a batch of guessed calls in parallel.
        model = getattr(self.client, "model", "")
        return is_qwen25_coder(model) or is_target_model(model) or \
            is_bundled_default_model(model)

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

                label_bits = [
                    str(args.get(k) or "") for k in (
                        "action", "command", "url", "query", "app", "target",
                        "source", "path", "repo", "number", "title",
                        "hostname")]
                label = " ".join(bit for bit in label_bits if bit).strip()
                if name == "check_port":
                    label = f"{args.get('host', '')}:{args.get('port', '')}"
                info(f"{c_cyan('Tool: ' + name)} {c_dim(one_line(label))}")

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

def default_config() -> dict:
    """All user preferences understood by the CLI, installer and web UI."""
    return {
        "model": DEFAULT_MODEL,
        "backend": "auto",
        "host": "",
        "models_dir": "",
        "model_file": DEFAULT_MODEL_FILE,
        "model_discovery": True,
        "ctx": DEFAULT_CTX,
        "max_steps": DEFAULT_MAX_STEPS,
        "auto": False,
        "cmd_timeout": DEFAULT_CMD_TIMEOUT,
        "workspace": DEFAULT_WORKSPACE,
        "web_host": DEFAULT_WEB_HOST,
        "web_port": DEFAULT_WEB_PORT,
        "open_browser": True,
        "frontend_enabled": True,
        "ui_theme": "system",
        "ui_default_view": "chat",
    }

def write_default_config() -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(default_config(), indent=2), encoding="utf-8")
    return p

def update_config(**updates) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    cfg.update(updates)
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def save_model_file_preference(record: dict, model: str = "",
                               models_dir: str = "") -> None:
    """Remember which file in models/ Milelik selected at startup."""
    updates = {"model_file": str(record.get("path") or "")}
    if model:
        updates["model"] = model
    if models_dir:
        updates["models_dir"] = models_dir
    try:
        update_config(**updates)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

HELP_TEXT = """Commands:
  /help              show this help
  /model <name>      switch model (e.g. /model Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M)
  /models            list model files in models/ and models on the backend
  /yolo on|off       auto-approve ordinary commands (dangerous/sudo still ask)
  /info              system info
  /clear             forget conversation history
  /exit, /quit       leave
  !<command>         run a shell command yourself, e.g.  !df -h
Anything else is sent to the AI as a task."""

def interactive(agent: Agent, client, toolbox: Toolbox):
    print(c_bold("\nMilelik") + f"  model={client.model}  "
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
            print(format_model_inventory(discover_model_files(), model_directories()))
            try:
                server_models = client.tags()
                print("\nBackend models:\n" + ("\n".join(server_models)
                      or f"(no models reported by {client.host})"))
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

def run_check(client, requested_model: str = "", local_models=None,
              local_model_directories=None, selected_local_model=None) -> int:
    ok = True
    print(c_bold("Milelik check"))
    print(f"  Python : {platform.python_version()}")
    print(f"  OS     : {distro_name()}")
    print(f"  Default: {BUNDLED_MODEL_LABEL} (bundled in models/)")
    requested_model = requested_model or getattr(client, "model", "") \
        or BUNDLED_MODEL_LABEL

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
        mark = c_green(f"Up - requested model found as '{found}'") if found \
            else c_yellow("Up - requested model NOT loaded/installed here")
        print(f"  {label:<10} {probe.host}  {mark}")
        if models:
            shown = ", ".join(models[:6]) + ("…" if len(models) > 6 else "")
            print(f"             models: {shown}")

    if local_models is None:
        local_model_directories = model_directories()
        local_models = discover_model_files(local_model_directories)
    local_dirs_text = ", ".join(str(d) for d in (local_model_directories or []))
    ready_local = [m for m in local_models if m.get("supported")]
    print(f"  Local models: {len(ready_local)} runnable GGUF(s), "
          f"{len(local_models) - len(ready_local)} other model file(s)")
    if local_dirs_text:
        print(f"             folders: {local_dirs_text}")
    for record in local_models[:8]:
        mark = "[ok]" if record.get("supported") else "•"
        suffix = " (selected)" if selected_local_model and \
            record.get("id") == selected_local_model.get("id") else ""
        print(f"             {mark} {record.get('name')} — "
              f"{record.get('size_human')}{suffix}")
    if len(local_models) > 8:
        print("             … run `milelik --list-models` for the full list")

    print(f"  Selected: backend={getattr(client, 'backend', '?')}  "
          f"host={getattr(client, 'host', '?')}  model={getattr(client, 'model', '?')}")
    if not any_up:
        ok = False
        print(c_red("  No local model server is reachable"))
        print("    Fix (pick one):")
        print("      python3 setup_model.py   # imports the bundled SmolLM2 GGUF")
        print("      ollama serve          # if Ollama is installed but stopped")
        print(f"      …or load {BUNDLED_MODEL_FILENAME} in LM Studio and start its server (port 1234)")
    elif client.model_available():
        print(c_green(f"  Selected model '{client.model}' is available"))
    else:
        ok = False
        print(c_yellow(f"  Selected model '{client.model}' is not available "
                       "on the selected backend"))
        if getattr(client, "backend", "") == "ollama":
            if is_bundled_default_model(client.model):
                print("    Fix:  python3 setup_model.py   # imports the bundled GGUF")
            else:
                print(f"    Fix:  ollama pull {client.model}")
                print("          …or import a GGUF:  python3 setup_model.py /path/to/model.gguf")
        else:
            print(f"    Fix: load {BUNDLED_MODEL_FILENAME} in LM Studio / "
                  "llama-server, then re-run.")

    print("  Network: ", end="")
    try:
        tb = Toolbox(auto=True)
        result = tb.dns_lookup("duckduckgo.com")
        print(c_green(f"DNS works ({result})") if "->" in result else c_yellow(result))
        if "->" not in result:
            ok = False
    except Exception as e:  # pragma: no cover
        print(c_red(f"Error: {e}"))
        ok = False

    optional = [("VLC", "vlc"), ("playerctl", "playerctl"), ("gh", "gh"),
                ("git", "git"), ("xdg-open", "xdg-open"),
                ("gtk-launch", "gtk-launch"), ("LibreOffice", "libreoffice"),
                ("ffprobe", "ffprobe"), ("notify-send", "notify-send"),
                ("xclip", "xclip"), ("brightnessctl", "brightnessctl"),
                ("node", "node"), ("npm", "npm")]
    found = [name for name, binary in optional if shutil.which(binary)]
    missing = [name for name, binary in optional if not shutil.which(binary)]
    try:
        app_count = len(Toolbox(auto=True)._desktop_entries())
    except Exception:
        app_count = 0
    print(f"  Apps   : {app_count} desktop apps found; helpers present: "
          + (", ".join(found) if found else "(none)"))
    if missing:
        print(c_dim("           optional, missing: " + ", ".join(missing)
                    + f"  (install with: {dependency_install_command()})"))
    print("  GitHub : " + (
        "GITHUB_TOKEN/GH_TOKEN is set" if Toolbox._github_token() else
        "`gh auth login` recommended for GitHub write actions"
        if shutil.which("gh") else
        "install gh + `gh auth login`, or set GITHUB_TOKEN, for write actions"))
    web_ready = (_APP_DIR / "milelik_web.py").is_file() and \
        (_APP_DIR / "frontend" / "index.html").is_file()
    web_cfg = default_config()
    web_cfg.update(load_config())
    print("  Web UI : " + (
        f"ready — run `milelik --gui` (http://{web_cfg.get('web_host')}:"
        f"{web_cfg.get('web_port')}, workspace {web_cfg.get('workspace')})"
        if web_ready else
        "not installed — re-run python3 setup_milelik.py"))

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
        description="Fast local AI assistant for linux Mint (local model + shell, web, apps/VLC and Git/GitHub tools).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""examples:
  milelik                        # open the Milelik web app
  milelik "find the 10 biggest folders in my home directory"
  milelik "what is my public IP, and is port 22 open on 192.168.1.1?"
  milelik "search the web for how to enable SSH on linux Mint and summarise it"
  milelik --yolo "check for apt updates and list them"
  milelik "play ~/Videos/demo.mp4 in VLC fullscreen, then tell me its length"
  milelik "list the open pull requests and failing CI runs in this repo"
  milelik "open my Documents folder and take a screenshot"
  milelik --gui                  # browser front end + workspace + local APIs
  milelik --interactive
  milelik --list-models          # models found in Milelik's models/ folder
  milelik --choose-model -i       # choose a model file, then chat
  milelik --model-file {BUNDLED_MODEL_FILENAME} "check my disk"  # bundled default
  milelik --model {TARGET_MODEL_LABEL} "write and test code"     # optional Qwen upgrade
  milelik --backend openai --host http://127.0.0.1:1234 "check my disk"   # LM Studio
  milelik --import-gguf models/{BUNDLED_MODEL_FILENAME}""")
    p.add_argument("task", nargs="*", help="Task to perform (omit with no other arguments to open the web app)")
    p.add_argument("-i", "--interactive", action="store_true", help="Start the interactive chat")
    p.add_argument("--model", default=os.environ.get("MILELIK_MODEL", cfg.get("model", DEFAULT_MODEL)),
                   help=f"Model to use (default: {DEFAULT_MODEL}, Milelik's bundled "
                        f"{BUNDLED_MODEL_LABEL}). The optional "
                        f"'{TARGET_MODEL_LABEL}', its .gguf filename, and its "
                        "LM Studio / Hugging Face names are also accepted.")
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
    p.add_argument("--cmd-timeout", type=int,
                   default=int(cfg.get("cmd_timeout", DEFAULT_CMD_TIMEOUT)),
                   help="Default timeout for shell commands, seconds (default: 60)")
    p.add_argument("--models-dir", default=os.environ.get(
        "MILELIK_MODELS_DIR", cfg.get("models_dir") or None),
                   help="folder searched recursively for local model files "
                        "(default: Milelik's models/ folder)")
    p.add_argument("--model-file", default=os.environ.get(
        "MILELIK_MODEL_FILE", cfg.get("model_file") or None),
                   help="choose a local model by path, filename, id or Ollama name")
    p.add_argument("--list-models", action="store_true",
                   help="read and list all AI model files in models/, then exit")
    p.add_argument("--choose-model", action="store_true",
                   help="show the models/ file chooser at startup")
    p.add_argument("--no-model-scan", action="store_true",
                   help="do not read the models/ folder at startup")
    p.add_argument("--gui", "--web", "--frontend", dest="gui", action="store_true",
                   help="Start Milelik's browser front end, local workspace and APIs, then exit when stopped")
    p.add_argument("--workspace-dir", "--workspace", dest="workspace_dir",
                   default=os.environ.get("MILELIK_WORKSPACE",
                                          cfg.get("workspace", DEFAULT_WORKSPACE)),
                   help="Folder used by the web workspace/code runner")
    p.add_argument("--web-host", default=os.environ.get(
        "MILELIK_WEB_HOST", cfg.get("web_host", DEFAULT_WEB_HOST)),
                   help="Address for --gui (default: 127.0.0.1; use 0.0.0.0 only on a trusted LAN)")
    p.add_argument("--web-port", type=int, default=int(
        os.environ.get("MILELIK_WEB_PORT", cfg.get("web_port", DEFAULT_WEB_PORT))),
                   help="Port for --gui (default: 8765)")
    p.add_argument("--open-browser", dest="open_browser", action="store_true",
                   default=argparse.SUPPRESS,
                   help="Open a browser when starting --gui")
    p.add_argument("--no-open-browser", dest="open_browser", action="store_false",
                   default=argparse.SUPPRESS,
                   help="Do not open a browser when starting --gui")
    p.add_argument("--check", action="store_true",
                   help="Check Ollama/LM Studio/llama.cpp, the model, and network, then exit")
    p.add_argument("--pull", action="store_true",
                   help="Download the selected model with Ollama and exit")
    p.add_argument("--import-gguf", metavar="PATH", default=None,
                   help="Import a local GGUF (e.g. " + BUNDLED_MODEL_FILENAME + ") into "
                        "Ollama with an appropriate template (Qwen2.5-Coder "
                        "ChatML/tools for Qwen2.5-Coder files), make it "
                        "Milelik's default model, and exit")
    p.add_argument("--import-name", default=None,
                   help="Ollama name for --import-gguf (default: automatic based on the file)")
    p.add_argument("--init-config", action="store_true", help="Write a default config file and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="Show full tool output")
    p.add_argument("--no-color", action="store_true", help="Disable colours")
    p.add_argument("--version", action="version", version=f"milelik {__version__}")
    parsed = p.parse_args(argv)
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parsed.raw_argv = tuple(raw_argv)
    parsed.explicit_model = any(
        arg == "--model" or arg.startswith("--model=") for arg in raw_argv)
    parsed.explicit_model_file = any(
        arg == "--model-file" or arg.startswith("--model-file=")
        for arg in raw_argv)
    parsed.explicit_models_dir = any(
        arg == "--models-dir" or arg.startswith("--models-dir=")
        for arg in raw_argv)
    return parsed

def should_open_web_app_by_default(raw_argv, stdin_isatty: bool,
                                   stdout_isatty: bool, cfg: dict | None = None) -> bool:
    """A bare interactive ``milelik`` invocation opens the web app.

    Piping a task on stdin still works for scripts, and an explicitly
    disabled front end leaves terminal chat as the fallback.
    """
    cfg = cfg or {}
    return (not tuple(raw_argv or ()) and bool(stdin_isatty)
            and bool(stdout_isatty) and cfg.get("frontend_enabled", True) is not False)


def launch_frontend(args) -> int:
    """Load the sibling web module and run the local front end.

    The web UI is kept in milelik_web.py so the command-line program stays
    usable without it; dynamic loading also works from a source checkout and
    from ~/.local/share/milelik after installation.
    """
    web_path = Path(__file__).resolve().parent / "milelik_web.py"
    if not web_path.is_file():
        err(f"Milelik's front end is missing: {web_path}")
        print("Re-run python3 setup_milelik.py from the Milelik repository.")
        return 1
    import importlib.util
    spec = importlib.util.spec_from_file_location("milelik_web", web_path)
    if spec is None or spec.loader is None:
        err(f"Could not load Milelik's front end from {web_path}")
        return 1
    web = importlib.util.module_from_spec(spec)
    sys.modules["milelik_web"] = web
    spec.loader.exec_module(web)

    cfg = load_config()
    open_browser = getattr(args, "open_browser", None)
    if open_browser is None:
        open_browser = bool(cfg.get("open_browser", True))
    try:
        return int(web.run_server(
            host=args.web_host,
            port=args.web_port,
            workspace=args.workspace_dir,
            open_browser=open_browser,
            core_module=sys.modules[__name__],
            models_dir=getattr(args, "models_dir", None),
            model_file=getattr(args, "model_file", None),
        ))
    except KeyboardInterrupt:
        print("\nMilelik front end stopped.")
        return 130

def offline_mode(env=None) -> bool:
    """True when Milelik must not download anything (MILELIK_OFFLINE=1)."""
    source = os.environ if env is None else env
    return str(source.get("MILELIK_OFFLINE", "")).strip().lower() in (
        "1", "true", "yes", "y", "on")


def main(argv=None) -> int:
    args = parse_args(argv)

    C.enabled = (not args.no_color and sys.stdout.isatty()
                 and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb")

    if args.init_config:
        print(f"Wrote {write_default_config()}")
        return 0

    if should_open_web_app_by_default(
            getattr(args, "raw_argv", ()), sys.stdin.isatty(),
            sys.stdout.isatty(), load_config()):
        args.gui = True

    # Startup model-file discovery: read Milelik's models/ folders and pick
    # the file the user/config asks for (otherwise the bundled SmolLM2 GGUF,
    # then the optional Qwen target, then the first runnable GGUF).
    # --choose-model opens the terminal chooser.
    startup_cfg = load_config()
    local_model_directories: list[Path] = []
    local_models: list[dict] = []
    selected_local_model = None
    scan_models = args.list_models or (
        not args.no_model_scan
        and (bool(startup_cfg.get("model_discovery", True))
             or bool(args.model_file)
             or args.explicit_models_dir))
    if scan_models:
        local_model_directories = model_directories(args.models_dir, cfg=startup_cfg)
        local_models = discover_model_files(local_model_directories, cfg=startup_cfg)
        if args.list_models:
            print(format_model_inventory(local_models, local_model_directories))
            return 0

        selector = args.model if args.explicit_model else (args.model_file or "")
        selector_is_explicit = bool(
            args.explicit_model_file or os.environ.get("MILELIK_MODEL_FILE"))
        show_chooser = bool(args.choose_model or (
            args.interactive and sys.stdin.isatty()
            and not args.explicit_model and not args.explicit_model_file
            and not startup_cfg.get("model_file")
            and not args.gui and not args.check and not args.pull))
        selected_local_model = choose_model_file(
            local_models,
            configured_model=args.model,
            selected_model_file=selector,
            interactive=show_chooser,
            allow_configured=True,
            strict_selected=bool(
                not show_chooser
                and (args.explicit_model or selector_is_explicit)),
        )
        selector_resolved = bool(
            selector and find_model_file(selector, local_models))
        if selector and not selector_resolved and not show_chooser:
            if selector_is_explicit and not selected_local_model:
                err(f"Model file not found in models/: {selector}")
                print("Run `milelik --list-models` to see the model files Milelik can see.")
                return 1
            if not args.explicit_model and not selector_is_explicit:
                warn(f"Saved model file is no longer available; ignoring it: {selector}")
        if selected_local_model and not selected_local_model.get("supported"):
            err(f"Selected model file is not runnable: {selected_local_model['path']}")
            if selected_local_model.get("reason"):
                print(selected_local_model["reason"])
            return 1
        if selected_local_model:
            inferred = infer_backend_from_host(args.host) if args.host else ""
            if args.backend == "openai" or inferred == "openai":
                args.model = selected_local_model["stem"]
            elif args.backend == "ollama" or inferred == "ollama":
                args.model = selected_local_model["ollama_name"]
            else:
                args.model = selected_local_model["stem"]
            args.model_file = selected_local_model["path"]
            if show_chooser or selector_is_explicit:
                save_model_file_preference(
                    selected_local_model,
                    models_dir=args.models_dir if args.explicit_models_dir else "")
            if not args.check:
                print(f"Using model file: {selected_local_model['name']} "
                      f"({selected_local_model['size_human']})")

    args.local_model_records = local_models
    args.local_model_directories = local_model_directories
    args.selected_model_file = selected_local_model

    if args.gui:
        return launch_frontend(args)

    # --import-gguf, or `--model /path/to/model.gguf`, means: put this exact
    # file into Ollama with the correct Qwen2.5-Coder template and use it.
    import_path = args.import_gguf
    implicit_import = False
    if not import_path and looks_like_gguf_path(args.model) \
            and Path(os.path.expanduser(args.model)).is_file():
        import_path = args.model
        implicit_import = True
    if import_path:
        import_record = selected_local_model
        try:
            import_physical = str(Path(os.path.expanduser(import_path)).resolve())
        except (OSError, RuntimeError):
            import_physical = ""
        if not import_record or import_record.get("path") != import_physical:
            import_record = model_file_record_for_path(import_path)
        if not args.import_name:
            args.import_name = import_record.get("ollama_name") or LOCAL_IMPORTED_MODEL
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
        save_model_file_preference(import_record, model=args.import_name)
        print(f"Saved as Milelik's default model in {config_path()}")
        if not implicit_import:
            print('Now just run:  milelik "your task"')
            return 0
        args.model, args.backend = args.import_name, "ollama"

    client = select_client(args.backend, args.host, args.model,
                           args.ctx, api_key=args.api_key)
    toolbox = Toolbox(auto=args.auto, verbose=args.verbose, cmd_timeout=args.cmd_timeout)

    if args.check:
        return run_check(client, requested_model=args.model,
                         local_models=local_models,
                         local_model_directories=local_model_directories,
                         selected_local_model=selected_local_model)

    if args.pull and selected_local_model:
        pull_host = args.host if args.host and infer_backend_from_host(args.host) == "ollama" \
            else DEFAULT_HOST
        pull_client = make_ollama_client(
            pull_host, selected_local_model["ollama_name"], args.ctx)
        if not pull_client.ensure_running():
            err(f"Ollama isn't running at {pull_client.host}.")
            print("Start it with:  ollama serve")
            return 1
        if import_gguf_to_ollama(selected_local_model["path"],
                                  selected_local_model["ollama_name"]):
            update_config(model=selected_local_model["ollama_name"], backend="ollama")
            save_model_file_preference(
                selected_local_model, model=selected_local_model["ollama_name"])
            return 0
        return 1

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
                  "Or install everything with:  python3 setup_milelik.py")
        else:
            err(f"No local model server is running at {client.host}.")
            print("Pick one:\n"
                  f"  • LM Studio: load {BUNDLED_MODEL_FILENAME}, then start the "
                  "local server (Developer tab, port 1234)\n"
                  f"  • llama.cpp: llama-server -m models/{BUNDLED_MODEL_FILENAME} "
                  "--host 127.0.0.1 --port 8080 -c 8192 --jinja\n"
                  "  • Ollama instead: python3 setup_model.py")
        return 1

    model_ready = client.model_available()
    if selected_local_model and client.backend == "ollama" and not model_ready:
        imported_name = selected_local_model["ollama_name"]
        found = client.resolve_available_model(imported_name, update=False) \
            if hasattr(client, "resolve_available_model") else ""
        if found:
            client.model = found
            model_ready = True
        elif import_gguf_to_ollama(selected_local_model["path"], imported_name):
            client.model = imported_name
            update_config(model=imported_name, backend="ollama")
            save_model_file_preference(selected_local_model, model=imported_name)
            model_ready = True
        else:
            return 1

    if not model_ready:
        if client.backend == "ollama":
            warn(f"Model '{client.model}' is not installed in Ollama yet.")
            if is_bundled_default_model(client.model):
                print("Run:  python3 setup_model.py   # imports Milelik's bundled GGUF")
                return 1
            print("Already have the GGUF file? Press n, then run:  "
                  "python3 setup_model.py /path/to/model.gguf")
            if offline_mode():
                print("Offline mode (MILELIK_OFFLINE=1): not downloading "
                      f"'{client.model}'.")
                return 1
            if sys.stdin.isatty():
                try:
                    ans = input(f"Download it now with `ollama pull {client.model}`? "
                                "[Y/n] ").strip().lower()
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
            print(f"Load {BUNDLED_MODEL_FILENAME} in LM Studio / llama-server, "
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
