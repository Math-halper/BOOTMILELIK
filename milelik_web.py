#!/usr/bin/env python3
"""Milelik's local browser front end, workspace and HTTP APIs.

This module deliberately uses only the Python standard library.  It serves
frontend/ on a local HTTP server and exposes token-protected JSON APIs for:

  * chatting with Milelik's local model (with browser approval prompts)
  * browsing, editing and running files in a local workspace
  * cloning GitHub repositories into that workspace
  * coding helpers (run/check/regex/encoding)
  * mathematics (safe evaluation, statistics, conversion, quadratics)
  * small text/hash/time utilities
  * all 26 existing Milelik tools

Run it with ``milelik --gui``.  The default bind address is 127.0.0.1.
"""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import contextlib
import datetime as _dt
import hashlib
import html
import io
import json
import math
import mimetypes
import os
import platform
import re
import secrets
import shutil
import statistics
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http import HTTPStatus, cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # Normal use: milelik.py and milelik_web.py sit beside one another.
    import milelik as core
except Exception:  # pragma: no cover - run_server can be given a core module.
    core = None  # type: ignore[assignment]

APP_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = APP_DIR / "frontend"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 3_000_000
MAX_FILE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 20_000
TOKEN_COOKIE = "milelik_token"
TOKEN_HEADER = "X-Milelik-Token"
SENSITIVE_ENV = {
    "GITHUB_TOKEN", "GH_TOKEN", "MILELIK_API_KEY", "OPENAI_API_KEY",
    "MILELIK_WEB_TOKEN",
}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ApiError(Exception):
    """An error that can be returned directly as a JSON API response."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = str(message)
        self.status = int(status)


def _clean_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = ANSI_RE.sub("", str(text or ""))
    text = text.replace("\r", "\n")
    if len(text) > limit:
        return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"
    return text


def _finite_number(value, name: str = "value") -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ApiError(f"{name} must be a number.")
    if not math.isfinite(number):
        raise ApiError(f"{name} must be finite.")
    return number


def _json_number(value):
    if isinstance(value, int):
        return value
    number = float(value)
    if not math.isfinite(number):
        raise ApiError("The result is not a finite number.")
    if number.is_integer() and abs(number) < 10**15:
        return int(number)
    return number


# ---------------------------------------------------------------------------
# Mathematics API (safe AST evaluation, never Python eval)
# ---------------------------------------------------------------------------

_MATH_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau,
                   "phi": (1 + math.sqrt(5)) / 2}
_MATH_FUNCTIONS = {
    "abs": abs,
    "acos": math.acos,
    "asin": math.asin,
    "atan": math.atan,
    "atan2": math.atan2,
    "ceil": math.ceil,
    "cos": math.cos,
    "cosh": math.cosh,
    "degrees": math.degrees,
    "exp": math.exp,
    "factorial": math.factorial,
    "floor": math.floor,
    "gcd": math.gcd,
    "hypot": math.hypot,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "max": max,
    "min": min,
    "pow": math.pow,
    "radians": math.radians,
    "round": round,
    "sin": math.sin,
    "sinh": math.sinh,
    "sqrt": math.sqrt,
    "tan": math.tan,
    "tanh": math.tanh,
}


def evaluate_math_expression(expression: str, variables: dict | None = None):
    """Evaluate a small arithmetic expression without executing Python code."""
    expression = str(expression or "").strip()
    if not expression:
        raise ApiError("An expression is required.")
    if len(expression) > 500:
        raise ApiError("The expression is too long (maximum 500 characters).")

    values = dict(_MATH_CONSTANTS)
    for name, value in (variables or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
            raise ApiError(f"Invalid variable name: {name!r}")
        values[str(name)] = _finite_number(value, f"variable {name}")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise ApiError(f"Syntax error at line {e.lineno}, column {e.offset}: {e.msg}")

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ApiError("Only numbers are allowed in math expressions.")
            return _finite_number(node.value, "constant")
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ApiError(f"Unknown variable or constant: {node.id}")
            return values[node.id]
        if isinstance(node, ast.UnaryOp):
            value = evaluate(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +value
            if isinstance(node.op, ast.USub):
                return -value
            raise ApiError("That unary operator is not supported.")
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            try:
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    return left / right
                if isinstance(node.op, ast.FloorDiv):
                    return left // right
                if isinstance(node.op, ast.Mod):
                    return left % right
                if isinstance(node.op, ast.Pow):
                    if abs(right) > 1000:
                        raise ApiError("Exponents are limited to ±1000.")
                    return left ** right
            except ZeroDivisionError:
                raise ApiError("Division by zero.")
            except OverflowError:
                raise ApiError("The calculation overflowed.")
            raise ApiError("That operator is not supported.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise ApiError("Only simple named math functions are supported.")
            name = node.func.id
            if name not in _MATH_FUNCTIONS:
                raise ApiError(f"Unknown math function: {name}")
            args = [evaluate(arg) for arg in node.args]
            if name == "factorial":
                if len(args) != 1 or int(args[0]) != args[0] or not 0 <= args[0] <= 1000:
                    raise ApiError("factorial(x) needs an integer from 0 to 1000.")
                args = [int(args[0])]
            if name == "gcd":
                args = [int(arg) for arg in args]
            try:
                return _MATH_FUNCTIONS[name](*args)
            except (ValueError, OverflowError, ZeroDivisionError) as e:
                raise ApiError(f"Math error in {name}(): {e}")
        raise ApiError(f"Unsupported expression element: {node.__class__.__name__}")

    return _json_number(evaluate(tree))


def statistics_payload(numbers) -> dict:
    if isinstance(numbers, str):
        raw = [part for part in re.split(r"[,\s]+", numbers.strip()) if part]
    elif isinstance(numbers, list):
        raw = numbers
    else:
        raise ApiError("numbers must be a JSON array or a comma/space separated string.")
    if not raw or len(raw) > 10_000:
        raise ApiError("Provide between 1 and 10,000 numbers.")
    values = sorted(_finite_number(value, "number") for value in raw)
    n = len(values)
    result = {
        "count": n,
        "sum": _json_number(sum(values)),
        "mean": _json_number(statistics.fmean(values)),
        "median": _json_number(statistics.median(values)),
        "minimum": _json_number(values[0]),
        "maximum": _json_number(values[-1]),
        "range": _json_number(values[-1] - values[0]),
    }
    try:
        result["mode"] = _json_number(statistics.mode(values))
    except statistics.StatisticsError:
        result["mode"] = None
    result["population_variance"] = _json_number(statistics.pvariance(values))
    result["population_stdev"] = _json_number(statistics.pstdev(values))
    if n > 1:
        result["sample_variance"] = _json_number(statistics.variance(values))
        result["sample_stdev"] = _json_number(statistics.stdev(values))
    if n >= 4:
        q1, _median, q3 = statistics.quantiles(values, n=4, method="inclusive")
        result.update({"q1": _json_number(q1), "q3": _json_number(q3),
                       "iqr": _json_number(q3 - q1)})
    return result


_UNIT_CATEGORIES: dict[str, dict[str, float]] = {
    "length": {"mm": 0.001, "cm": 0.01, "m": 1, "km": 1000,
               "in": 0.0254, "ft": 0.3048, "yd": 0.9144,
               "mi": 1609.344, "nmi": 1852},
    "mass": {"g": 0.001, "kg": 1, "t": 1000,
             "oz": 0.028349523125, "lb": 0.45359237},
    "time": {"ms": 0.001, "s": 1, "min": 60, "h": 3600,
             "day": 86400, "week": 604800},
    "data": {"bit": 0.125, "B": 1, "KB": 1000, "MB": 1000**2,
             "GB": 1000**3, "TB": 1000**4, "KiB": 1024,
             "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4},
    "speed": {"m/s": 1, "km/h": 1 / 3.6, "mph": 0.44704,
              "knot": 0.5144444444},
    "area": {"cm2": 0.0001, "m2": 1, "km2": 1_000_000,
             "ha": 10_000, "acre": 4046.8564224, "ft2": 0.09290304},
    "volume": {"ml": 0.001, "L": 1, "m3": 1000,
               "gallon_us": 3.785411784, "cup": 0.2365882365},
    "pressure": {"Pa": 1, "kPa": 1000, "bar": 100_000,
                 "atm": 101_325, "psi": 6894.757293168},
    "energy": {"J": 1, "kJ": 1000, "cal": 4.184, "kcal": 4184,
               "kWh": 3_600_000},
}
_UNIT_ALIASES = {
    "metre": "m", "meter": "m", "metres": "m", "meters": "m",
    "kilometre": "km", "kilometer": "km", "kilometres": "km",
    "kilometers": "km", "centimetre": "cm", "centimeter": "cm",
    "millimetre": "mm", "millimeter": "mm", "inch": "in", "inches": "in",
    "foot": "ft", "feet": "ft", "yard": "yd", "yards": "yd",
    "mile": "mi", "miles": "mi", "gram": "g", "grams": "g",
    "kilogram": "kg", "kilograms": "kg", "pound": "lb", "pounds": "lb",
    "ounce": "oz", "ounces": "oz", "second": "s", "seconds": "s",
    "minute": "min", "minutes": "min", "hour": "h", "hours": "h",
    "byte": "B", "bytes": "B", "litre": "L", "liter": "L",
    "litres": "L", "liters": "L",
    "c": "C", "celsius": "C", "°c": "C", "f": "F", "fahrenheit": "F",
    "°f": "F", "k": "K", "kelvin": "K",
}


def _normalise_unit(unit: str) -> str:
    raw = str(unit or "").strip()
    lower = raw.lower().replace(" ", "_")
    return _UNIT_ALIASES.get(lower, _UNIT_ALIASES.get(raw, raw))


def convert_units(value, from_unit: str, to_unit: str) -> dict:
    number = _finite_number(value)
    source, target = _normalise_unit(from_unit), _normalise_unit(to_unit)
    if source in ("C", "F", "K") or target in ("C", "F", "K"):
        if source not in ("C", "F", "K") or target not in ("C", "F", "K"):
            raise ApiError("Temperature units can only convert to C, F or K.")
        celsius = number if source == "C" else (
            (number - 32) * 5 / 9 if source == "F" else number - 273.15)
        result = celsius if target == "C" else (
            celsius * 9 / 5 + 32 if target == "F" else celsius + 273.15)
        return {"value": _json_number(result), "from": source, "to": target,
                "category": "temperature"}
    for category, units in _UNIT_CATEGORIES.items():
        if source in units:
            if target not in units:
                raise ApiError(f"{target!r} is not a {category} unit.")
            result = number * units[source] / units[target]
            return {"value": _json_number(result), "from": source,
                    "to": target, "category": category}
    raise ApiError(f"Unknown unit: {from_unit!r}")


def solve_quadratic(a, b, c) -> dict:
    a, b, c = (_finite_number(a, "a"), _finite_number(b, "b"),
               _finite_number(c, "c"))
    if a == 0:
        if b == 0:
            raise ApiError("That is not an equation (a and b are both zero).")
        return {"type": "linear", "roots": [_json_number(-c / b)]}
    discriminant = b * b - 4 * a * c
    payload = {"type": "quadratic", "a": _json_number(a), "b": _json_number(b),
               "c": _json_number(c), "discriminant": _json_number(discriminant),
               "vertex": {"x": _json_number(-b / (2 * a)),
                          "y": _json_number(c - (b * b) / (4 * a))}}
    if discriminant > 0:
        root = math.sqrt(discriminant)
        payload["roots"] = [_json_number((-b - root) / (2 * a)),
                            _json_number((-b + root) / (2 * a))]
    elif discriminant == 0:
        payload["roots"] = [_json_number(-b / (2 * a))]
    else:
        payload["roots"] = []
        payload["complex_roots"] = [
            {"real": _json_number(-b / (2 * a)),
             "imaginary": _json_number(-math.sqrt(-discriminant) / (2 * a))},
            {"real": _json_number(-b / (2 * a)),
             "imaginary": _json_number(math.sqrt(-discriminant) / (2 * a))},
        ]
    return payload


# ---------------------------------------------------------------------------
# Coding and small utility helpers
# ---------------------------------------------------------------------------

def _safe_env() -> dict:
    env = {key: value for key, value in os.environ.items()
           if key not in SENSITIVE_ENV}
    env.setdefault("TERM", "dumb")
    return env


def run_process(args, cwd: Path, stdin: str = "", timeout: int = 30) -> dict:
    try:
        timeout = max(1, min(int(timeout or 30), 300))
    except (TypeError, ValueError):
        timeout = 30
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(arg) for arg in args], cwd=str(cwd), input=str(stdin or ""),
            capture_output=True, text=True, errors="replace", timeout=timeout,
            env=_safe_env())
        code, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        code = 124
        stdout = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        stderr += f"\nTimed out after {timeout} seconds."
    except FileNotFoundError as e:
        raise ApiError(f"Program not found: {e.filename or args[0]}")
    except OSError as e:
        raise ApiError(f"Could not run {args[0]}: {e}")
    return {
        "exit_code": code,
        "stdout": _clean_output(stdout),
        "stderr": _clean_output(stderr),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "cwd": str(cwd),
    }


def _version_of(binary: str, args=("--version",)) -> str | None:
    path = shutil.which(binary)
    if not path:
        return None
    try:
        proc = subprocess.run([path] + list(args), capture_output=True,
                              text=True, errors="replace", timeout=4,
                              env=_safe_env())
        text = (proc.stdout or proc.stderr or "").strip().splitlines()
        return text[0].strip() if text else path
    except (OSError, subprocess.SubprocessError):
        return path


def regex_search(pattern: str, text: str, flags: str = "") -> dict:
    if len(pattern or "") > 500 or len(text or "") > 100_000:
        raise ApiError("Pattern or text is too large.")
    bits = 0
    for flag in str(flags or ""):
        bits |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X}.get(flag, 0)
        if flag not in "imsx":
            raise ApiError(f"Unknown regex flag: {flag!r} (use i, m, s or x)")
    try:
        compiled = re.compile(str(pattern), bits)
    except re.error as e:
        raise ApiError(f"Invalid regular expression: {e}")
    matches = []
    for match in compiled.finditer(str(text)):
        matches.append({"match": match.group(0), "start": match.start(),
                        "end": match.end(), "groups": list(match.groups())})
        if len(matches) >= 100:
            break
    return {"count": len(matches), "matches": matches,
            "truncated": len(matches) >= 100}


def encode_decode(operation: str, text: str) -> dict:
    data = str(text or "")
    op = str(operation or "").lower().replace("-", "_")
    try:
        if op == "base64_encode":
            result = base64.b64encode(data.encode()).decode()
        elif op == "base64_decode":
            result = base64.b64decode(data.encode(), validate=True).decode()
        elif op == "url_encode":
            result = urllib.parse.quote(data, safe="")
        elif op == "url_decode":
            result = urllib.parse.unquote(data)
        elif op == "html_escape":
            result = html.escape(data, quote=True)
        elif op == "html_unescape":
            result = html.unescape(data)
        elif op == "hex_encode":
            result = data.encode().hex()
        elif op == "hex_decode":
            result = bytes.fromhex(data).decode()
        elif op == "json_format":
            result = json.dumps(json.loads(data), indent=2, ensure_ascii=False)
        else:
            raise ApiError("operation must be base64_encode, base64_decode, "
                           "url_encode, url_decode, html_escape, html_unescape, "
                           "hex_encode, hex_decode or json_format.")
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as e:
        raise ApiError(f"Could not perform {op}: {e}")
    return {"operation": op, "result": result}


def text_utility(operation: str, text: str) -> dict:
    data = str(text or "")
    op = str(operation or "stats").lower().replace("-", "_")
    if op == "stats":
        words = re.findall(r"\b[\w'-]+\b", data, flags=re.UNICODE)
        sentences = [part for part in re.split(r"[.!?]+", data) if part.strip()]
        return {"operation": op, "characters": len(data),
                "characters_no_spaces": len(re.sub(r"\s", "", data)),
                "words": len(words), "lines": len(data.splitlines()),
                "sentences": len(sentences),
                "reading_time_minutes": round(len(words) / 200, 2) if words else 0}
    transforms = {
        "uppercase": data.upper,
        "lowercase": data.lower,
        "title": data.title,
        "trim": data.strip,
        "reverse": lambda: data[::-1],
        "slugify": lambda: re.sub(r"[^a-z0-9]+", "-", data.lower()).strip("-"),
    }
    if op == "word_frequencies":
        from collections import Counter
        return {"operation": op,
                "result": Counter(re.findall(r"\b\w+\b", data.lower())).most_common(50)}
    if op not in transforms:
        raise ApiError("operation must be stats, uppercase, lowercase, title, trim, "
                       "reverse, slugify or word_frequencies.")
    return {"operation": op, "result": transforms[op]()}


def generate_password(length=20, symbols: bool = True) -> dict:
    try:
        length = int(length)
    except (TypeError, ValueError):
        raise ApiError("length must be an integer from 8 to 128.")
    if not 8 <= length <= 128:
        raise ApiError("length must be from 8 to 128.")
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    if symbols:
        pools.append("!@#$%^&*()-_=+[]{};:,.?")
    chars = [secrets.choice(pool) for pool in pools]
    alphabet = "".join(pools)
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    return {"password": "".join(chars), "length": length, "symbols": bool(symbols)}


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

IGNORED_DIRS = {".git", ".milelik-runs", "__pycache__", "node_modules", ".venv"}
STARTER_FILES = {
    "README.md": """# Milelik Workspace\n\nThis is your Milelik workspace. Open a file on the left, edit it, then use **Run**.\n\nYou can also clone a GitHub repository into this folder from the Workspace page.\n""",
    "examples/hello.py": """print('Hello from your Milelik workspace!')\nname = input('What is your name? ')\nprint(f'Nice to meet you, {name or \"friend\"}.')\n""",
    "examples/maths.py": """import math\n\nfor n in range(1, 8):\n    print(n, 'squared is', n * n, 'and sqrt is', round(math.sqrt(n), 3))\n""",
    "examples/hello.js": """console.log('Hello from JavaScript in Milelik Workspace!');\nconsole.log('2 + 3 =', 2 + 3);\n""",
    "notes.md": """# Notes\n\nIdeas, commands and things to try:\n\n- Ask Milelik to explain a file in this workspace.\n- Ask it to create a Python script here, then run it.\n- Clone a GitHub project and inspect its README.\n""",
}


class Workspace:
    def __init__(self, root):
        self._lock = threading.RLock()
        self.root = Path()
        self.set_root(root or "~/MilelikWorkspace")

    def set_root(self, root) -> Path:
        with self._lock:
            candidate = Path(os.path.expanduser(str(root or "~/MilelikWorkspace"))).resolve()
            existed = candidate.exists()
            if existed and not candidate.is_dir():
                raise ApiError(f"Workspace is not a directory: {candidate}")
            candidate.mkdir(parents=True, exist_ok=True)
            self.root = candidate
            entries = [p for p in candidate.iterdir() if p.name != ".DS_Store"]
            if not existed or not entries:
                for relative, content in STARTER_FILES.items():
                    target = candidate / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        target.write_text(content, encoding="utf-8")
            return self.root

    def resolve(self, relative: str = ".", *, must_exist: bool = False) -> Path:
        raw = str(relative or ".").replace("\\", "/")
        if "\x00" in raw:
            raise ApiError("Invalid workspace path.")
        requested = Path(raw)
        if requested.is_absolute():
            raise ApiError("Use a path inside the workspace, not an absolute path.")
        candidate = (self.root / requested).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise ApiError("That path is outside the workspace.")
        if must_exist and not candidate.exists():
            raise ApiError(f"Not found in workspace: {relative}", 404)
        return candidate

    def relative(self, path: Path) -> str:
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return str(path)
        return "." if str(rel) == "." else rel.as_posix()

    def tree(self) -> dict:
        count = 0
        truncated = False

        def build(directory: Path, depth: int = 0) -> list:
            nonlocal count, truncated
            nodes = []
            try:
                children = sorted(directory.iterdir(),
                                  key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return nodes
            for child in children:
                if child.is_dir() and child.name in IGNORED_DIRS:
                    continue
                if count >= 1000:
                    truncated = True
                    break
                count += 1
                try:
                    stat = child.stat()
                    modified = _dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
                    size = stat.st_size if child.is_file() else None
                except OSError:
                    modified, size = None, None
                node = {"name": child.name, "path": self.relative(child),
                        "type": "folder" if child.is_dir() else "file",
                        "size": size, "modified": modified}
                if child.is_dir() and depth < 12:
                    node["children"] = build(child, depth + 1)
                nodes.append(node)
            return nodes

        return {"root": str(self.root), "tree": build(self.root),
                "truncated": truncated, "project": self.project_suggestions()}

    def read_file(self, relative: str) -> dict:
        path = self.resolve(relative, must_exist=True)
        if not path.is_file():
            raise ApiError("That workspace path is not a file.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ApiError("That file is larger than the 2 MB editor limit.")
        data = path.read_bytes()
        if b"\x00" in data:
            raise ApiError("That looks like a binary file; the editor only opens text.")
        return {"path": self.relative(path), "content": data.decode("utf-8", errors="replace"),
                "size": len(data), "language": language_for_path(path)}

    def write_file(self, relative: str, content: str) -> dict:
        path = self.resolve(relative)
        if path == self.root or path.is_dir():
            raise ApiError("Choose a file path inside the workspace.")
        encoded = str(content or "").encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ApiError("Files are limited to 2 MB in the web editor.")
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded)
        return {"path": self.relative(path), "size": len(encoded),
                "language": language_for_path(path), "saved": True}

    def make_folder(self, relative: str) -> dict:
        path = self.resolve(relative)
        with self._lock:
            path.mkdir(parents=True, exist_ok=True)
        return {"path": self.relative(path), "created": True}

    def delete_file(self, relative: str) -> dict:
        path = self.resolve(relative, must_exist=True)
        if path == self.root:
            raise ApiError("The workspace folder itself cannot be deleted.")
        with self._lock:
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    raise ApiError("Only empty folders can be deleted in the web workspace.")
            else:
                path.unlink()
        return {"path": self.relative(path), "deleted": True}

    def rename(self, source: str, destination: str) -> dict:
        src = self.resolve(source, must_exist=True)
        dst = self.resolve(destination)
        if src == self.root:
            raise ApiError("The workspace folder itself cannot be renamed.")
        if dst.exists():
            raise ApiError("The destination already exists.")
        with self._lock:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        return {"from": self.relative(src), "to": self.relative(dst)}

    def run_dir(self) -> Path:
        directory = self.root / ".milelik-runs"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def project_suggestions(self) -> dict:
        suggestions = []
        files = {p.name for p in self.root.iterdir() if p.is_file()} if self.root.exists() else set()

        def add(label, command):
            suggestions.append({"label": label, "command": command, "cwd": "."})

        if "package.json" in files:
            try:
                package = json.loads((self.root / "package.json").read_text(errors="replace"))
                scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            except (OSError, ValueError):
                scripts = {}
            add("Install Node dependencies", "npm install")
            for name in ("dev", "start", "test", "build"):
                if name in scripts:
                    add(f"npm run {name}", f"npm run {name}")
        if "requirements.txt" in files:
            add("Create Python venv and install requirements",
                "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
        for candidate in ("main.py", "app.py", "run.py"):
            if candidate in files:
                add(f"Run {candidate}", f"python3 {candidate}")
        if "Cargo.toml" in files:
            add("Run Rust project", "cargo run")
        if "go.mod" in files:
            add("Run Go project", "go run .")
        if "Makefile" in files:
            add("Run default Make target", "make")
        readme = next((name for name in files if name.lower() == "readme.md"), None)
        return {"readme": readme, "suggestions": suggestions[:10]}


def language_for_path(path: Path) -> str:
    return {".py": "python", ".sh": "bash", ".bash": "bash",
            ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
            ".json": "json", ".md": "markdown", ".html": "html", ".htm": "html",
            ".css": "css", ".txt": "text", ".toml": "toml"}.get(
                path.suffix.lower(), "text")


# ---------------------------------------------------------------------------
# API catalogue / OpenAPI
# ---------------------------------------------------------------------------

API_CATALOG = [
    {"method": "GET", "path": "/api/health", "category": "System", "description": "Check that the local API is running.", "example": {}},
    {"method": "GET", "path": "/api/status", "category": "System", "description": "Model, backend, workspace and helper status.", "example": {}},
    {"method": "POST", "path": "/api/chat", "category": "AI", "description": "Send a task to Milelik's local model and its tools.", "example": {"message": "What is my disk space and local IP?"}},
    {"method": "POST", "path": "/api/tools/run", "category": "AI tools", "description": "Call one of Milelik's 26 tools directly.", "example": {"tool": "system_info", "arguments": {}}},
    {"method": "GET", "path": "/api/workspace", "category": "Workspace", "description": "Get the workspace tree and project run suggestions.", "example": {}},
    {"method": "GET", "path": "/api/workspace/file?path=examples/hello.py", "category": "Workspace", "description": "Read a text file from the workspace.", "example": {}},
    {"method": "PUT", "path": "/api/workspace/file", "category": "Workspace", "description": "Create or replace a workspace text file.", "example": {"path": "hello.py", "content": "print('Hello')\n"}},
    {"method": "POST", "path": "/api/workspace/run-file", "category": "Workspace", "description": "Run a Python, Bash or JavaScript file in the workspace.", "example": {"path": "examples/hello.py", "stdin": "Ada\n"}},
    {"method": "POST", "path": "/api/workspace/clone", "category": "Workspace", "description": "Clone a public GitHub repository into the workspace.", "example": {"url": "https://github.com/Math-halper/BOOTMILELIK.git"}},
    {"method": "POST", "path": "/api/code/run", "category": "Coding", "description": "Run a Python, Bash or JavaScript snippet in the workspace.", "example": {"language": "python", "code": "print(2 + 3)"}},
    {"method": "POST", "path": "/api/code/check", "category": "Coding", "description": "Syntax-check Python, Bash or JavaScript without running it.", "example": {"language": "python", "code": "print('ok')"}},
    {"method": "POST", "path": "/api/code/regex", "category": "Coding", "description": "Test a regular expression and list matches/groups.", "example": {"pattern": "\\d+", "text": "Room 101 and 42"}},
    {"method": "POST", "path": "/api/code/encode", "category": "Coding", "description": "Base64, URL, HTML and hex encode/decode, or format JSON.", "example": {"operation": "base64_encode", "text": "Milelik"}},
    {"method": "POST", "path": "/api/math/evaluate", "category": "Mathematics", "description": "Safely evaluate an arithmetic expression with functions and variables.", "example": {"expression": "sqrt(x^2 + 9)", "variables": {"x": 4}}},
    {"method": "POST", "path": "/api/math/statistics", "category": "Mathematics", "description": "Mean, median, spread and quartiles for a list of numbers.", "example": {"numbers": [4, 8, 15, 16, 23, 42]}},
    {"method": "POST", "path": "/api/math/convert", "category": "Mathematics", "description": "Convert length, mass, time, data, speed, temperature and more.", "example": {"value": 72, "from": "F", "to": "C"}},
    {"method": "POST", "path": "/api/math/quadratic", "category": "Mathematics", "description": "Solve ax² + bx + c = 0, including vertex and complex roots.", "example": {"a": 1, "b": -3, "c": 2}},
    {"method": "POST", "path": "/api/utilities/text", "category": "Utilities", "description": "Text statistics and transformations.", "example": {"operation": "stats", "text": "Hello Milelik world."}},
    {"method": "POST", "path": "/api/utilities/hash", "category": "Utilities", "description": "Hash text with MD5, SHA-1, SHA-256, SHA-512 or BLAKE2b.", "example": {"algorithm": "sha256", "text": "Milelik"}},
    {"method": "POST", "path": "/api/utilities/password", "category": "Utilities", "description": "Generate a cryptographically random password locally.", "example": {"length": 20, "symbols": True}},
]


def openapi_spec(host="127.0.0.1", port=DEFAULT_PORT, version="dev") -> dict:
    paths: dict[str, dict] = {}
    for item in API_CATALOG:
        clean_path = item["path"].split("?", 1)[0]
        operation = {
            "tags": [item["category"]],
            "summary": item["description"],
            "responses": {"200": {"description": "Successful JSON response"}},
            "security": [{"MilelikToken": []}],
        }
        if item["method"] in ("POST", "PUT"):
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {
                    "schema": {"type": "object"}, "example": item["example"]}},
            }
        paths.setdefault(clean_path, {})[item["method"].lower()] = operation
    return {
        "openapi": "3.0.3",
        "info": {"title": "Milelik Local API", "version": str(version),
                 "description": "Local APIs served by Milelik on your own device."},
        "servers": [{"url": f"http://{host}:{port}"}],
        "components": {"securitySchemes": {"MilelikToken": {
            "type": "apiKey", "in": "header", "name": TOKEN_HEADER}}},
        "paths": paths,
    }


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

class WebState:
    def __init__(self, core_module, workspace, token: str | None = None):
        if core_module is None:
            raise RuntimeError("milelik.py could not be imported")
        self.core = core_module
        self.token = token or os.environ.get("MILELIK_WEB_TOKEN") or secrets.token_urlsafe(24)
        self.workspace = Workspace(workspace)
        self.frontend_dir = FRONTEND_DIR
        self.bound_host = DEFAULT_HOST
        self.bound_port = DEFAULT_PORT
        self.verbose = False
        self._agent = None
        self._client = None
        self._agent_lock = threading.Lock()
        self._task_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._transcript: list[dict] = []
        self._approvals: dict[str, dict] = {}

    # -- settings ---------------------------------------------------------
    def effective_config(self) -> dict:
        defaults = self.core.default_config() if hasattr(self.core, "default_config") else {}
        cfg = dict(defaults)
        cfg.update(self.core.load_config())
        cfg["workspace"] = str(self.workspace.root)
        return cfg

    def update_settings(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise ApiError("Settings must be a JSON object.")
        clean: dict[str, object] = {}

        def text(key, maximum=500):
            if key in data:
                value = str(data[key] or "").strip()
                if len(value) > maximum:
                    raise ApiError(f"{key} is too long.")
                clean[key] = value

        def integer(key, minimum, maximum):
            if key in data:
                try:
                    value = int(data[key])
                except (TypeError, ValueError):
                    raise ApiError(f"{key} must be an integer.")
                if not minimum <= value <= maximum:
                    raise ApiError(f"{key} must be between {minimum} and {maximum}.")
                clean[key] = value

        def boolean(key):
            if key in data:
                clean[key] = bool(data[key])

        def choice(key, options):
            if key in data:
                value = str(data[key])
                if value not in options:
                    raise ApiError(f"{key} must be one of: {', '.join(options)}")
                clean[key] = value

        text("model", 300)
        text("host", 300)
        text("workspace", 1000)
        text("web_host", 200)
        choice("backend", ("auto", "ollama", "openai"))
        choice("ui_theme", ("system", "dark", "light"))
        choice("ui_default_view", ("chat", "workspace", "apis", "settings"))
        integer("ctx", 1024, 131072)
        integer("max_steps", 1, 50)
        integer("cmd_timeout", 5, 900)
        integer("web_port", 1, 65535)
        for key in ("auto", "open_browser", "frontend_enabled"):
            boolean(key)
        if not clean:
            raise ApiError("No recognised settings were supplied.")

        if "workspace" in clean:
            self.workspace.set_root(str(clean["workspace"]))
            clean["workspace"] = str(self.workspace.root)
        self.core.update_config(**clean)
        if {"model", "backend", "host", "ctx", "max_steps", "auto",
                "cmd_timeout", "workspace"} & set(clean):
            with self._agent_lock:
                self._agent = None
                self._client = None
        restart = (("web_host" in clean and clean["web_host"] != self.bound_host)
                   or ("web_port" in clean and clean["web_port"] != self.bound_port))
        return {"settings": self.effective_config(),
                "restart_required_for_web_address": bool(restart)}

    # -- approvals ---------------------------------------------------------
    @staticmethod
    def _public_approval(record: dict) -> dict:
        return {key: record.get(key) for key in (
            "id", "action", "detail", "dangerous_reason", "sudo",
            "created_at", "status")}

    def confirm_from_tool(self, action="", detail="", dangerous_reason=None,
                          sudo=False, from_user=False, **_extra) -> bool:
        record = {
            "id": uuid.uuid4().hex,
            "action": str(action or "perform an action"),
            "detail": str(detail or ""),
            "dangerous_reason": dangerous_reason,
            "sudo": bool(sudo),
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "status": "pending",
            "decision": False,
            "event": threading.Event(),
        }
        with self._state_lock:
            self._approvals[record["id"]] = record
        approved = record["event"].wait(timeout=300) and bool(record["decision"])
        with self._state_lock:
            if record["status"] == "pending":
                record["status"] = "expired"
            self._approvals.pop(record["id"], None)
        return approved

    def list_approvals(self) -> list[dict]:
        with self._state_lock:
            return [self._public_approval(r) for r in self._approvals.values()
                    if r["status"] == "pending"]

    def decide_approval(self, approval_id: str, approved: bool,
                        typed_yes: str = "") -> dict:
        with self._state_lock:
            record = self._approvals.get(approval_id)
            if not record or record["status"] != "pending":
                raise ApiError("That approval is no longer pending.", 404)
            if approved and record.get("dangerous_reason") and typed_yes != "YES":
                raise ApiError("Type YES in the approval dialog to allow a dangerous action.")
            record["decision"] = bool(approved)
            record["status"] = "approved" if approved else "denied"
            public = self._public_approval(record)
            record["event"].set()
            return public

    # -- model / agent ------------------------------------------------------
    def _select_client(self):
        cfg = self.effective_config()
        return self.core.select_client(
            str(cfg.get("backend") or "auto"), str(cfg.get("host") or "") or None,
            str(cfg.get("model") or self.core.DEFAULT_MODEL),
            int(cfg.get("ctx") or self.core.DEFAULT_CTX))

    def status(self) -> dict:
        cfg = self.effective_config()
        model_status = {"configured_model": cfg.get("model"),
                        "configured_backend": cfg.get("backend"),
                        "server_up": False, "model_available": False,
                        "available_models": []}
        try:
            client = self._select_client()
            up = bool(client.is_up())
            models = []
            available = False
            if up:
                try:
                    models = list(client.tags(timeout=3))
                except Exception:
                    models = []
                try:
                    available = bool(client.resolve_available_model(
                        str(cfg.get("model") or ""), update=False))
                except Exception:
                    available = False
            model_status.update({
                "selected_backend": getattr(client, "backend", "unknown"),
                "selected_model": getattr(client, "model", ""),
                "host": getattr(client, "host", ""),
                "server_up": up,
                "model_available": available,
                "available_models": models,
            })
        except Exception as e:
            model_status["error"] = str(e)
        helpers = {name: bool(shutil.which(name)) for name in (
            "git", "gh", "node", "npm", "vlc", "playerctl", "ffprobe",
            "libreoffice", "ollama")}
        return {
            "ok": True,
            "version": getattr(self.core, "__version__", "dev"),
            "time": _dt.datetime.now().isoformat(timespec="seconds"),
            "workspace": str(self.workspace.root),
            "web": {"host": self.bound_host, "port": self.bound_port,
                    "token_required": True},
            "model": model_status,
            "settings": cfg,
            "tools": list(getattr(self.core, "TOOL_NAMES", [])),
            "helpers": helpers,
            "python": platform.python_version(),
            "platform": platform.platform(),
        }

    def models(self) -> dict:
        try:
            client = self._select_client()
            models = list(client.tags(timeout=5)) if client.is_up() else []
            return {"backend": getattr(client, "backend", ""),
                    "host": getattr(client, "host", ""), "models": models}
        except Exception as e:
            return {"backend": "", "host": "", "models": [], "error": str(e)}

    def system_payload(self) -> dict:
        toolbox = self.core.Toolbox(auto=True, cwd=str(self.workspace.root))
        lines = toolbox.system_info().splitlines()
        details = {}
        for line in lines:
            if ":" in line:
                key, value = line.split(":", 1)
                details[key.strip().lower().replace(" ", "_")] = value.strip()
        details.update({
            "hostname": platform.node(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "workspace": str(self.workspace.root),
        })
        return {"system": details, "text": "\n".join(lines)}

    def _get_agent(self):
        with self._agent_lock:
            if self._agent is not None:
                return self._agent
            cfg = self.effective_config()
            client = self._select_client()
            if not client.ensure_running():
                raise ApiError(
                    "No local model server is running. Start Ollama with "
                    "`ollama serve`, or load the GGUF in LM Studio/llama.cpp "
                    "and start its local server.", 503)
            if not client.model_available():
                loaded = []
                try:
                    loaded = list(client.tags(timeout=4))
                except Exception:
                    pass
                detail = f" Loaded there: {', '.join(loaded)}." if loaded else ""
                raise ApiError(
                    f"Model {cfg.get('model')!r} is not available at "
                    f"{getattr(client, 'host', '')}.{detail} "
                    "Run ./setup-model.sh or choose another model in Settings.", 503)
            toolbox = self.core.Toolbox(
                auto=bool(cfg.get("auto", False)), cwd=str(self.workspace.root),
                cmd_timeout=int(cfg.get("cmd_timeout") or 60),
                confirm_callback=self.confirm_from_tool)
            self._client = client
            self._agent = self.core.Agent(
                client, toolbox, max_steps=int(cfg.get("max_steps") or 12))
            return self._agent

    def chat(self, message: str) -> dict:
        message = str(message or "").strip()
        if not message:
            raise ApiError("A message is required.")
        if len(message) > 20_000:
            raise ApiError("That message is too long.")
        if not self._task_lock.acquire(blocking=False):
            raise ApiError("Milelik is already working on another task.", 409)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            agent = self._get_agent()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answer = agent.run(message)
        except ApiError:
            raise
        except Exception as e:
            if isinstance(e, getattr(self.core, "OllamaError", ())):
                raise ApiError(str(e), 503)
            raise
        finally:
            self._task_lock.release()
        entry_time = _dt.datetime.now().isoformat(timespec="seconds")
        with self._state_lock:
            self._transcript.append({"role": "user", "content": message,
                                      "created_at": entry_time})
            self._transcript.append({"role": "assistant", "content": str(answer),
                                      "created_at": entry_time})
            self._transcript = self._transcript[-200:]
        logs = "\n".join(line for line in _clean_output(
            stdout.getvalue() + "\n" + stderr.getvalue(), 8000).splitlines()
            if line.strip())
        return {"answer": str(answer), "model": getattr(self._client, "model", ""),
                "backend": getattr(self._client, "backend", ""),
                "workspace": str(self.workspace.root), "logs": logs}

    def transcript(self) -> list[dict]:
        with self._state_lock:
            return list(self._transcript)

    def reset_chat(self) -> dict:
        with self._state_lock:
            self._transcript.clear()
        with self._agent_lock:
            if self._agent is not None:
                self._agent.reset()
        return {"reset": True}

    def run_tool(self, name: str, arguments) -> dict:
        tool = self.core.canonical_tool_name(str(name or ""))
        if tool not in self.core.TOOL_NAMES:
            raise ApiError(f"Unknown Milelik tool: {name!r}")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ApiError("Tool arguments must be a JSON object.")
        if not self._task_lock.acquire(blocking=False):
            raise ApiError("Milelik is already working on another task.", 409)
        try:
            cfg = self.effective_config()
            toolbox = self.core.Toolbox(
                auto=bool(cfg.get("auto", False)), cwd=str(self.workspace.root),
                cmd_timeout=int(cfg.get("cmd_timeout") or 60),
                confirm_callback=self.confirm_from_tool)
            result = toolbox.dispatch(tool, arguments)
        finally:
            self._task_lock.release()
        return {"tool": tool, "result": str(result)}

    # -- code execution -----------------------------------------------------
    def code_runtimes(self) -> dict:
        return {
            "python": {"available": True, "version": platform.python_version(),
                       "executable": sys.executable},
            "bash": {"available": bool(shutil.which("bash")),
                     "version": _version_of("bash")},
            "node": {"available": bool(shutil.which("node")),
                     "version": _version_of("node")},
            "npm": {"available": bool(shutil.which("npm")),
                    "version": _version_of("npm")},
            "git": {"available": bool(shutil.which("git")),
                    "version": _version_of("git")},
            "gh": {"available": bool(shutil.which("gh")),
                   "version": _version_of("gh")},
        }

    @staticmethod
    def _language(value: str) -> str:
        language = str(value or "").lower().strip()
        aliases = {"py": "python", "python3": "python", "sh": "bash",
                   "shell": "bash", "js": "javascript", "node": "javascript",
                   "nodejs": "javascript"}
        language = aliases.get(language, language)
        if language not in ("python", "bash", "javascript"):
            raise ApiError("language must be python, bash or javascript.")
        return language

    def _code_temp(self, language: str, code: str) -> Path:
        extension = {"python": ".py", "bash": ".sh", "javascript": ".js"}[language]
        path = self.workspace.run_dir() / f"run-{uuid.uuid4().hex}{extension}"
        path.write_text(str(code or ""), encoding="utf-8")
        return path

    def run_code(self, language: str, code: str, stdin: str = "",
                 timeout=30, cwd: str = ".") -> dict:
        language = self._language(language)
        if len(str(code or "")) > 500_000:
            raise ApiError("Code snippets are limited to 500 KB.")
        working_dir = self.workspace.resolve(cwd or ".", must_exist=True)
        if not working_dir.is_dir():
            raise ApiError("The code working directory is not a folder.")
        temp = self._code_temp(language, code)
        try:
            if language == "python":
                args = [sys.executable, str(temp)]
            elif language == "bash":
                bash = shutil.which("bash")
                if not bash:
                    raise ApiError("Bash is not available.")
                args = [bash, str(temp)]
            else:
                node = shutil.which("node")
                if not node:
                    raise ApiError("Node.js is not installed. It is included in Milelik's full dependency set.")
                args = [node, str(temp)]
            result = run_process(args, working_dir, stdin, timeout)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
        result.update({"language": language,
                       "cwd": self.workspace.relative(working_dir)})
        return result

    def check_code(self, language: str, code: str) -> dict:
        language = self._language(language)
        code = str(code or "")
        if language == "python":
            try:
                compile(code, "<milelik-workspace>", "exec")
            except SyntaxError as e:
                return {"ok": False, "language": language,
                        "errors": [{"line": e.lineno, "column": e.offset,
                                    "message": e.msg, "text": (e.text or "").strip()}]}
            return {"ok": True, "language": language, "errors": []}
        temp = self._code_temp(language, code)
        try:
            if language == "bash":
                binary = shutil.which("bash")
                if not binary:
                    raise ApiError("Bash is not available.")
                args = [binary, "-n", str(temp)]
            else:
                binary = shutil.which("node")
                if not binary:
                    raise ApiError("Node.js is not installed.")
                args = [binary, "--check", str(temp)]
            result = run_process(args, self.workspace.root, "", 20)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
        message = (result["stderr"] or result["stdout"]).strip()
        return {"ok": result["exit_code"] == 0, "language": language,
                "errors": [] if result["exit_code"] == 0 else [{"message": message}]}

    def run_workspace_file(self, relative: str, stdin: str = "", timeout=30) -> dict:
        path = self.workspace.resolve(relative, must_exist=True)
        if not path.is_file():
            raise ApiError("That workspace path is not a file.")
        language = language_for_path(path)
        if language not in ("python", "bash", "javascript"):
            raise ApiError("Only .py, .sh/.bash and .js files can be run directly.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ApiError("That file is larger than the 2 MB runner limit.")
        result = run_process(
            ([sys.executable, str(path)] if language == "python" else
             [shutil.which("bash") or "bash", str(path)] if language == "bash" else
             [shutil.which("node") or "node", str(path)]),
            path.parent, stdin, timeout)
        result.update({"language": language, "path": self.workspace.relative(path),
                       "cwd": self.workspace.relative(path.parent)})
        return result

    def clone_repo(self, url: str, directory: str = "") -> dict:
        raw = str(url or "").strip()
        repo_name = ""
        if raw.startswith("git@github.com:"):
            tail = raw.split(":", 1)[1]
            if len([part for part in tail.split("/") if part]) < 2:
                raise ApiError("Use a full GitHub repository URL.")
            repo_name = tail.rstrip("/").rsplit("/", 1)[-1]
        else:
            parsed = urllib.parse.urlparse(raw)
            if parsed.scheme != "https" or parsed.hostname != "github.com":
                raise ApiError("For safety, the workspace cloner accepts GitHub URLs only.")
            if len([part for part in parsed.path.split("/") if part]) < 2:
                raise ApiError("Use a full owner/repository GitHub URL.")
            repo_name = [part for part in parsed.path.split("/") if part][-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        folder = str(directory or repo_name).strip()
        folder = re.sub(r"[^A-Za-z0-9._-]+", "-", folder).strip("-.")
        if not folder:
            raise ApiError("Choose a destination folder name.")
        target = self.workspace.resolve(folder)
        if target.exists():
            raise ApiError(f"That workspace folder already exists: {folder}")
        git = shutil.which("git")
        if not git:
            raise ApiError("Git is not installed.")
        env_args = [git, "clone", "--depth", "1", "--", raw, str(target)]
        result = run_process(env_args, self.workspace.root, "", 300)
        if result["exit_code"] != 0:
            return {"cloned": False, **result}
        return {"cloned": True, "path": self.workspace.relative(target), **result,
                "workspace": self.workspace.tree()}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class MilelikHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, state: WebState):
        self.state = state
        super().__init__(address, handler)


class MilelikHandler(BaseHTTPRequestHandler):
    server: MilelikHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "MilelikWeb/1.0"

    @property
    def state(self) -> WebState:
        return self.server.state

    def log_message(self, fmt, *args):
        if getattr(self.state, "verbose", False):
            super().log_message(fmt, *args)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")

    def _send_bytes(self, data: bytes, content_type: str,
                    status=HTTPStatus.OK, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_json(self, payload, status=HTTPStatus.OK, extra_headers=None):
        self._send_bytes(json.dumps(payload, ensure_ascii=False).encode(),
                         "application/json; charset=utf-8", status, extra_headers)

    def _send_error(self, message: str, status=HTTPStatus.BAD_REQUEST):
        self._send_json({"error": message}, status)

    def _cookie_token(self) -> str:
        try:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            morsel = jar.get(TOKEN_COOKIE)
            return morsel.value if morsel else ""
        except cookies.CookieError:
            return ""

    def _authenticated(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER, "") or self._cookie_token()
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        return bool(supplied) and secrets.compare_digest(supplied, self.state.token)

    def _require_auth(self):
        if not self._authenticated():
            raise ApiError(
                "Missing or invalid Milelik API token. Send it in the "
                f"{TOKEN_HEADER} header.", HTTPStatus.UNAUTHORIZED)
        origin = self.headers.get("Origin")
        if origin and self.command in ("POST", "PUT", "PATCH", "DELETE"):
            origin_host = urllib.parse.urlparse(origin).netloc
            if origin_host and origin_host != self.headers.get("Host", ""):
                raise ApiError("Cross-origin requests are not allowed.", HTTPStatus.FORBIDDEN)

    def _read_json(self, required: bool = True) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("Invalid Content-Length header.")
        if length > MAX_BODY_BYTES:
            raise ApiError("Request body is too large.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        if length <= 0:
            if required:
                raise ApiError("A JSON request body is required.")
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ApiError(f"Invalid JSON: {e}")
        if not isinstance(data, dict):
            raise ApiError("The JSON request body must be an object.")
        return data

    def _serve_static(self, filename: str):
        path = self.state.frontend_dir / filename
        if not path.is_file():
            raise ApiError("Milelik's front-end files are missing. Re-run ./install.sh.", 500)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "text/javascript"):
            content_type += "; charset=utf-8"
        self._send_bytes(path.read_bytes(), content_type,
                         extra_headers={"Cache-Control": "no-cache"})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                token = (query.get("token") or [""])[0]
                if token:
                    if not secrets.compare_digest(token, self.state.token):
                        raise ApiError("Invalid Milelik front-end token.", HTTPStatus.UNAUTHORIZED)
                    cookie = (f"{TOKEN_COOKIE}={self.state.token}; Path=/; HttpOnly; "
                              "SameSite=Strict")
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", cookie)
                    self._security_headers()
                    self.end_headers()
                    return
                self._serve_static("index.html")
                return
            if path in ("/styles.css", "/app.js"):
                self._serve_static(path.lstrip("/"))
                return
            if path == "/favicon.ico":
                self._send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
                return
            if path == "/api/health":
                self._send_json({"ok": True, "service": "milelik",
                                 "version": getattr(self.state.core, "__version__", "dev"),
                                 "time": _dt.datetime.now().isoformat(timespec="seconds")})
                return
            if not path.startswith("/api/"):
                raise ApiError("Not found.", HTTPStatus.NOT_FOUND)
            self._require_auth()

            if path == "/api/status":
                self._send_json(self.state.status())
            elif path == "/api/settings":
                self._send_json({"settings": self.state.effective_config()})
            elif path == "/api/models":
                self._send_json(self.state.models())
            elif path == "/api/system":
                self._send_json(self.state.system_payload())
            elif path == "/api/tools":
                self._send_json({"tools": getattr(self.state.core, "TOOLS", [])})
            elif path == "/api/docs":
                self._send_json({"title": "Milelik Local API", "endpoints": API_CATALOG,
                                 "auth": {"header": TOKEN_HEADER,
                                          "note": "The startup URL sets a browser cookie automatically."},
                                 "openapi": "/api/openapi.json"})
            elif path == "/api/openapi.json":
                self._send_json(openapi_spec(self.state.bound_host, self.state.bound_port,
                                              getattr(self.state.core, "__version__", "dev")))
            elif path == "/api/approvals":
                self._send_json({"approvals": self.state.list_approvals()})
            elif path == "/api/chat/history":
                self._send_json({"messages": self.state.transcript()})
            elif path == "/api/workspace":
                self._send_json(self.state.workspace.tree())
            elif path == "/api/workspace/file":
                self._send_json(self.state.workspace.read_file((query.get("path") or [""])[0]))
            elif path == "/api/workspace/download":
                file_path = self.state.workspace.resolve((query.get("path") or [""])[0], must_exist=True)
                if not file_path.is_file() or file_path.stat().st_size > 10_000_000:
                    raise ApiError("That file cannot be downloaded here.")
                self._send_bytes(
                    file_path.read_bytes(),
                    mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
                    extra_headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'})
            elif path == "/api/code/runtimes":
                self._send_json({"runtimes": self.state.code_runtimes()})
            elif path == "/api/math/constants":
                self._send_json({"constants": _MATH_CONSTANTS,
                                 "functions": sorted(_MATH_FUNCTIONS),
                                 "units": {"temperature": ["C", "F", "K"],
                                           **{k: list(v) for k, v in _UNIT_CATEGORIES.items()}}})
            elif path == "/api/utilities/time":
                now = _dt.datetime.now().astimezone()
                self._send_json({"local": now.isoformat(timespec="seconds"),
                                 "utc": now.astimezone(_dt.timezone.utc).isoformat(timespec="seconds"),
                                 "timestamp": int(now.timestamp()),
                                 "timezone": str(now.tzinfo)})
            else:
                raise ApiError("API endpoint not found.", HTTPStatus.NOT_FOUND)
        except ApiError as e:
            self._send_error(e.message, e.status)
        except BrokenPipeError:
            pass
        except Exception as e:  # keep the local server alive
            self._send_error(f"Internal Milelik web error: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        self._handle_write("POST")

    def do_PUT(self):
        self._handle_write("PUT")

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._require_auth()
            if path == "/api/workspace/file":
                self._send_json(self.state.workspace.delete_file((query.get("path") or [""])[0]))
                return
            raise ApiError("API endpoint not found.", HTTPStatus.NOT_FOUND)
        except ApiError as e:
            self._send_error(e.message, e.status)
        except Exception as e:
            self._send_error(f"Internal Milelik web error: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_write(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            self._require_auth()
            approval_match = re.fullmatch(r"/api/approvals/([^/]+)/decide", path)
            if approval_match and method == "POST":
                data = self._read_json()
                self._send_json(self.state.decide_approval(
                    approval_match.group(1), bool(data.get("approved")),
                    str(data.get("typed_yes") or "")))
                return

            data = self._read_json(required=path not in ("/api/chat/reset",))
            if path == "/api/settings" and method in ("POST", "PUT"):
                self._send_json(self.state.update_settings(data))
            elif path == "/api/chat" and method == "POST":
                self._send_json(self.state.chat(str(data.get("message") or "")))
            elif path == "/api/chat/reset" and method == "POST":
                self._send_json(self.state.reset_chat())
            elif path == "/api/tools/run" and method == "POST":
                self._send_json(self.state.run_tool(str(data.get("tool") or ""), data.get("arguments", {})))
            elif path == "/api/workspace/file" and method in ("POST", "PUT"):
                self._send_json(self.state.workspace.write_file(
                    str(data.get("path") or ""), str(data.get("content") or "")))
            elif path == "/api/workspace/folder" and method == "POST":
                self._send_json(self.state.workspace.make_folder(str(data.get("path") or "")))
            elif path == "/api/workspace/rename" and method == "POST":
                self._send_json(self.state.workspace.rename(
                    str(data.get("path") or data.get("from") or ""),
                    str(data.get("destination") or data.get("to") or "")))
            elif path == "/api/workspace/delete" and method == "POST":
                self._send_json(self.state.workspace.delete_file(str(data.get("path") or "")))
            elif path == "/api/workspace/run-file" and method == "POST":
                self._send_json(self.state.run_workspace_file(
                    str(data.get("path") or ""), str(data.get("stdin") or ""),
                    data.get("timeout", 30)))
            elif path == "/api/workspace/clone" and method == "POST":
                self._send_json(self.state.clone_repo(
                    str(data.get("url") or ""), str(data.get("directory") or "")))
            elif path == "/api/code/run" and method == "POST":
                self._send_json(self.state.run_code(
                    str(data.get("language") or "python"), str(data.get("code") or ""),
                    str(data.get("stdin") or ""), data.get("timeout", 30),
                    str(data.get("cwd") or ".")))
            elif path == "/api/code/check" and method == "POST":
                self._send_json(self.state.check_code(
                    str(data.get("language") or "python"), str(data.get("code") or "")))
            elif path == "/api/code/regex" and method == "POST":
                self._send_json(regex_search(str(data.get("pattern") or ""),
                                             str(data.get("text") or ""),
                                             str(data.get("flags") or "")))
            elif path == "/api/code/encode" and method == "POST":
                self._send_json(encode_decode(str(data.get("operation") or ""),
                                              str(data.get("text") or "")))
            elif path == "/api/math/evaluate" and method == "POST":
                variables = data.get("variables") or {}
                if not isinstance(variables, dict):
                    raise ApiError("variables must be a JSON object.")
                expression = str(data.get("expression") or "").replace("^", "**")
                self._send_json({"expression": data.get("expression"),
                                 "result": evaluate_math_expression(expression, variables)})
            elif path == "/api/math/statistics" and method == "POST":
                self._send_json(statistics_payload(data.get("numbers")))
            elif path == "/api/math/convert" and method == "POST":
                self._send_json(convert_units(data.get("value"),
                                              str(data.get("from") or ""),
                                              str(data.get("to") or "")))
            elif path == "/api/math/quadratic" and method == "POST":
                self._send_json(solve_quadratic(data.get("a"), data.get("b"), data.get("c")))
            elif path == "/api/utilities/text" and method == "POST":
                self._send_json(text_utility(str(data.get("operation") or "stats"),
                                             str(data.get("text") or "")))
            elif path == "/api/utilities/hash" and method == "POST":
                algorithm = str(data.get("algorithm") or "sha256").lower()
                if algorithm not in ("md5", "sha1", "sha256", "sha512", "blake2b"):
                    raise ApiError("algorithm must be md5, sha1, sha256, sha512 or blake2b.")
                digest = hashlib.new(algorithm, str(data.get("text") or "").encode()).hexdigest()
                self._send_json({"algorithm": algorithm, "digest": digest})
            elif path == "/api/utilities/password" and method == "POST":
                self._send_json(generate_password(data.get("length", 20), bool(data.get("symbols", True))))
            else:
                raise ApiError("API endpoint not found.", HTTPStatus.NOT_FOUND)
        except ApiError as e:
            self._send_error(e.message, e.status)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send_error(f"Internal Milelik web error: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  workspace="~/MilelikWorkspace", core_module=None,
                  token: str | None = None) -> MilelikHTTPServer:
    state = WebState(core_module or core, workspace, token)
    server = MilelikHTTPServer((str(host), int(port)), MilelikHandler, state)
    state.bound_host, state.bound_port = server.server_address[:2]
    return server


def _display_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else str(host)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
               workspace="~/MilelikWorkspace", open_browser: bool = True,
               core_module=None, token: str | None = None) -> int:
    try:
        server = create_server(host, port, workspace, core_module, token)
    except OSError as e:
        print(f"Could not start Milelik's front end on {host}:{port}: {e}", file=sys.stderr)
        print("Try another port, for example: milelik --gui --web-port 8766", file=sys.stderr)
        return 1
    state = server.state
    url = (f"http://{_display_host(host)}:{state.bound_port}/"
           f"?token={urllib.parse.quote(state.token)}")
    print("\n🤖 Milelik front end is running", flush=True)
    print(f"   Open:      {url}", flush=True)
    print(f"   Workspace: {state.workspace.root}", flush=True)
    print(f"   API docs:  http://{_display_host(host)}:{state.bound_port}/api/docs", flush=True)
    print(f"   API token header: {TOKEN_HEADER}: {state.token}", flush=True)
    print("   Press Ctrl+C to stop. Keep the token private: it can run local code.\n", flush=True)
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nMilelik front end stopped.")
    finally:
        server.server_close()
    return 0


def parse_args(argv=None):
    cfg = {}
    if core is not None:
        try:
            cfg = core.load_config()
        except Exception:
            cfg = {}
    parser = argparse.ArgumentParser(prog="milelik-web", description="Milelik browser front end, workspace and local APIs")
    parser.add_argument("--host", default=cfg.get("web_host", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(cfg.get("web_port", DEFAULT_PORT)))
    parser.add_argument("--workspace", default=cfg.get("workspace", "~/MilelikWorkspace"))
    parser.add_argument("--no-open-browser", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return run_server(args.host, args.port, args.workspace,
                      open_browser=not args.no_open_browser, core_module=core)


if __name__ == "__main__":
    raise SystemExit(main())
