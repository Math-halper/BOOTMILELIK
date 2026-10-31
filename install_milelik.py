#!/usr/bin/env python3
"""Python installer for the Milelik web app on linux Mint.

The normal entry point is ``python3 setup_milelik.py``, which opens the
browser setup page and then calls this installer. This module can also be
used directly:

    python3 install_milelik.py               # browser setup on a desktop
    python3 install_milelik.py --yes         # unattended defaults
    python3 install_milelik.py --wizard      # terminal preference questions

Every system dependency is declared in dependencies/apt-*.txt and installed
by dependencies/install_dependencies.py. No Milelik shell scripts are used.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUNDLED_MODEL_FILE = "smollm2-135m-instruct-q4_1.gguf"
BUNDLED_MODEL_LABEL = "SmolLM2-135M-Instruct-Q4_1"
BUNDLED_OLLAMA_MODEL = "milelik-smollm2:135m"
TARGET_OLLAMA_MODEL = "huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M"
TARGET_MODEL_LABEL = "Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
MODEL_EXTENSIONS = (".gguf", ".ggml", ".bin", ".safetensors", ".onnx", ".pt", ".pth")


class InstallerError(ValueError):
    """An installation choice or environment value is invalid."""


@dataclass
class InstallerOptions:
    model: str = BUNDLED_OLLAMA_MODEL
    gguf: str = ""
    model_source: str = "auto"
    backend: str = "auto"
    host: str = ""
    models_dir: str = ""
    model_file: str = ""
    model_discovery: bool = True
    dependency_mode: str = "full"
    performance_profile: str = "balanced"
    ctx: int = 8192
    max_steps: int = 12
    cmd_timeout: int = 60
    auto_approve: bool = False
    frontend_enabled: bool = True
    open_browser: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    workspace: str = ""
    ui_theme: str = "system"
    ui_default_view: str = "chat"
    github_login: bool = False
    launch: str = "none"
    assume_yes: bool = False
    force_wizard: bool = False
    browser_setup: bool = False
    skip_check: bool = False
    offline: bool = False
    model_was_set: bool = False
    ctx_was_set: bool = False
    max_steps_was_set: bool = False
    cmd_timeout_was_set: bool = False
    launch_was_set: bool = False
    raw_argv: tuple[str, ...] = field(default_factory=tuple)


def bool_value(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value) == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def expand_path(value) -> Path:
    path = Path(os.path.expanduser(str(value)))
    return path if path.is_absolute() else Path.cwd() / path


def _env_int(environ, name: str, default: int) -> int:
    try:
        return int(environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _has_argument(raw_argv, *names: str) -> bool:
    return any(arg == name or arg.startswith(f"{name}=")
               for arg in raw_argv for name in names)


def apply_performance_profile(options: InstallerOptions) -> None:
    profile = options.performance_profile
    values = {
        "fast": (4096, 8, 45),
        "balanced": (8192, 12, 60),
        "quality": (16384, 16, 90),
    }
    if profile == "custom":
        return
    if profile not in values:
        raise InstallerError(
            f"Invalid PERFORMANCE_PROFILE: {profile}\n"
            "Use fast, balanced, quality or custom.")
    ctx, steps, timeout = values[profile]
    if not options.ctx_was_set:
        options.ctx = ctx
    if not options.max_steps_was_set:
        options.max_steps = steps
    if not options.cmd_timeout_was_set:
        options.cmd_timeout = timeout


def normalize_options(options: InstallerOptions) -> InstallerOptions:
    if options.gguf and options.model_source == "auto":
        options.model_source = "gguf"
    elif (
        options.model_source == "auto" and options.model_was_set
        and options.model not in (
            BUNDLED_OLLAMA_MODEL, BUNDLED_MODEL_LABEL, BUNDLED_MODEL_FILE,
            TARGET_OLLAMA_MODEL, TARGET_MODEL_LABEL)
    ):
        options.model_source = "custom"
    if options.backend == "openai" and options.model_source == "auto":
        options.model_source = "server"
    apply_performance_profile(options)
    return options


def parse_args(argv=None, environ=None, home: Path | None = None) -> InstallerOptions:
    env = os.environ if environ is None else environ
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    home = Path(home) if home is not None else Path.home()

    parser = argparse.ArgumentParser(
        prog="install_milelik.py",
        description="Install the Milelik Python web app.")
    parser.add_argument("-y", "--yes", "--non-interactive", "--noninteractive",
                        dest="assume_yes", action="store_true",
                        help="install with defaults/environment values")
    parser.add_argument("--browser-setup", action="store_true",
                        help="force Milelik's browser setup page")
    parser.add_argument("--wizard", "--terminal-setup", dest="force_wizard",
                        action="store_true",
                        help="force the terminal preference questions")
    parser.add_argument("--skip-check", action="store_true",
                        default=bool_value(env.get("SKIP_CHECK", "0")),
                        help="do not run milelik --check at the end")
    parser.add_argument("--offline", action="store_true",
                        default=bool_value(env.get("MILELIK_OFFLINE", "0")),
                        help="never download Ollama or models; fail clearly instead")
    parser.add_argument("--model", default=env.get("MODEL", BUNDLED_OLLAMA_MODEL),
                        help="Ollama model or LM Studio/llama.cpp model id")
    parser.add_argument("--gguf", default=env.get("GGUF", ""),
                        help="import an existing GGUF into Ollama")
    parser.add_argument("--models-dir", default=env.get("MODELS_DIR", ""),
                        help="folder Milelik reads for AI model files")
    parser.add_argument("--model-file", default=env.get("MODEL_FILE", ""),
                        help="preferred model file/path/id inside models/")
    parser.add_argument("--model-scan", dest="model_discovery",
                        action="store_true",
                        default=bool_value(env.get("MODEL_DISCOVERY", "1"), True),
                        help="read models/ at startup (default)")
    parser.add_argument("--no-model-scan", dest="model_discovery",
                        action="store_false",
                        help="do not read models/ at startup")
    parser.add_argument("--backend", default=env.get("BACKEND", "auto"),
                        help="auto, ollama or openai")
    parser.add_argument("--host", default=env.get("MODEL_HOST", env.get("HOST", "")),
                        help="model server URL")
    parser.add_argument("--dependency-mode",
                        default=env.get("DEPENDENCY_MODE", "full"),
                        help="full, with-optional, required or none")
    parser.add_argument("--workspace", "--workspace-dir", dest="workspace",
                        default=env.get("WORKSPACE_DIR", str(home / "MilelikWorkspace")),
                        help="Milelik web workspace folder")
    parser.add_argument("--web-host", default=env.get("WEB_HOST", "127.0.0.1"),
                        help="front-end bind address")
    parser.add_argument("--web-port", type=int,
                        default=_env_int(env, "WEB_PORT", 8765),
                        help="front-end port")
    parser.add_argument("--frontend", dest="frontend_enabled", action="store_true",
                        default=bool_value(env.get("FRONTEND_ENABLED", "1"), True),
                        help="install the browser front end (default)")
    parser.add_argument("--no-frontend", dest="frontend_enabled",
                        action="store_false", help="do not install the front end")
    parser.add_argument("--launch", default=env.get("LAUNCH_AFTER_INSTALL", "none"),
                        help="after install: frontend, cli or none")
    parser.add_argument("--github-login", dest="github_login", action="store_true",
                        default=bool_value(env.get("GITHUB_LOGIN", "0")),
                        help="run gh auth login at the end")
    parser.add_argument("--no-github-login", dest="github_login",
                        action="store_false")
    parser.add_argument("--auto-approve", dest="auto_approve", action="store_true",
                        default=bool_value(env.get("AUTO_APPROVE", "0")),
                        help="auto-approve ordinary actions")
    parser.add_argument("--ask-first", dest="auto_approve", action="store_false",
                        help="ask before actions (default)")

    parsed = parser.parse_args(raw_argv)
    options = InstallerOptions(
        model=str(parsed.model),
        gguf=str(parsed.gguf),
        model_source=str(env.get("MODEL_SOURCE", "auto")),
        backend=str(parsed.backend),
        host=str(parsed.host),
        models_dir=str(parsed.models_dir),
        model_file=str(parsed.model_file),
        model_discovery=bool(parsed.model_discovery),
        dependency_mode=str(parsed.dependency_mode),
        performance_profile=str(env.get("PERFORMANCE_PROFILE", "balanced")),
        ctx=_env_int(env, "CTX", 8192),
        max_steps=_env_int(env, "MAX_STEPS", 12),
        cmd_timeout=_env_int(env, "CMD_TIMEOUT", 60),
        auto_approve=bool(parsed.auto_approve),
        frontend_enabled=bool(parsed.frontend_enabled),
        open_browser=bool_value(env.get("OPEN_BROWSER", "1"), True),
        web_host=str(parsed.web_host),
        web_port=int(parsed.web_port),
        workspace=str(parsed.workspace),
        ui_theme=str(env.get("UI_THEME", "system")),
        ui_default_view=str(env.get("UI_DEFAULT_VIEW", "chat")),
        github_login=bool(parsed.github_login),
        launch=str(parsed.launch),
        assume_yes=bool(parsed.assume_yes),
        force_wizard=bool(parsed.force_wizard),
        browser_setup=bool(parsed.browser_setup),
        skip_check=bool(parsed.skip_check),
        offline=bool(parsed.offline),
        model_was_set=bool(env.get("MODEL")) or _has_argument(raw_argv, "--model"),
        ctx_was_set=bool(env.get("CTX")),
        max_steps_was_set=bool(env.get("MAX_STEPS")),
        cmd_timeout_was_set=bool(env.get("CMD_TIMEOUT")),
        launch_was_set=bool(env.get("LAUNCH_AFTER_INSTALL"))
        or _has_argument(raw_argv, "--launch"),
        raw_argv=raw_argv,
    )
    if options.gguf and _has_argument(raw_argv, "--gguf"):
        options.model_source = "gguf"
    return normalize_options(options)


def _read_answer(input_fn, prompt: str = "") -> str:
    try:
        return str(input_fn(prompt) or "")
    except (EOFError, KeyboardInterrupt):
        return ""


def ask_text(label: str, default: str = "", input_fn=input) -> str:
    print(f"\n{label}")
    print(f"  Default: {default}")
    answer = _read_answer(input_fn, "  Your choice (Enter = default): ").strip()
    return answer or default


def ask_yes_no(label: str, default: bool = False, input_fn=input) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = _read_answer(input_fn, f"\n{label} {hint} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes", "true", "1", "on"):
            return True
        if answer in ("n", "no", "false", "0", "off"):
            return False
        print("  Please answer y or n.")


def ask_choice(label: str, default: str, choices, input_fn=input) -> str:
    values = [str(value) for value, _label in choices]
    labels = [str(text) for _value, text in choices]
    default_index = values.index(str(default)) if str(default) in values else 0
    while True:
        print(f"\n{label}")
        for index, text in enumerate(labels, 1):
            suffix = "  [default]" if index - 1 == default_index else ""
            print(f"  {index}) {text}{suffix}")
        answer = _read_answer(
            input_fn,
            f"  Choose 1-{len(values)} (Enter = {default_index + 1}): ").strip().lower()
        if not answer:
            return values[default_index]
        if answer.isdecimal():
            number = int(answer)
            if 1 <= number <= len(values):
                return values[number - 1]
        for value in values:
            if value.lower() == answer:
                return value
        print("  Please choose one of the numbers above.")


def ask_int(label: str, default: int, minimum: int, maximum: int,
            input_fn=input) -> int:
    while True:
        print(f"\n{label} ({minimum}–{maximum})")
        print(f"  Default: {default}")
        answer = _read_answer(input_fn, "  Your choice: ").strip()
        if not answer:
            return int(default)
        if answer.isdecimal():
            value = int(answer)
            if minimum <= value <= maximum:
                return value
        print(f"  Please enter a whole number from {minimum} to {maximum}.")


def print_summary(options: InstallerOptions) -> None:
    print()
    print("──────────────── Milelik install choices ────────────────")
    print(f"  Dependencies:     {options.dependency_mode}")
    print(f"  Model source:     {options.model_source}")
    print(f"  Model:            {options.model}")
    if options.gguf:
        print(f"  GGUF:             {options.gguf}")
    if options.model_discovery:
        print(f"  Models folder:    {options.models_dir or 'Milelik models/ (auto-detected)'}")
        print(f"  Preferred file:   {options.model_file or 'automatic / ask at startup'}")
    else:
        print("  Models folder:    not scanned at startup")
    print(f"  Backend:          {options.backend}")
    if options.host:
        print(f"  Model server:     {options.host}")
    print(f"  Performance:      {options.performance_profile} "
          f"(ctx {options.ctx}, {options.max_steps} steps, {options.cmd_timeout}s commands)")
    print("  Safety:           " + (
        "auto-approve ordinary actions (dangerous/sudo still ask)"
        if options.auto_approve else "ask before actions"))
    if options.frontend_enabled:
        print(f"  Front end:        yes — http://{options.web_host}:{options.web_port}")
        print(f"  Workspace:        {options.workspace}")
        print(f"  Theme/open page:  {options.ui_theme} / {options.ui_default_view}")
        print(f"  Open browser:     {1 if options.open_browser else 0}")
    else:
        print("  Front end:        no")
    print(f"  GitHub login:     {1 if options.github_login else 0}")
    print(f"  Launch afterwards:{options.launch}")
    print("─────────────────────────────────────────────────────────")


def run_setup_wizard(options: InstallerOptions, root: Path = ROOT,
                     input_fn=input) -> bool:
    print()
    print("Milelik setup wizard")
    print("I'll ask a few preference questions. Press Enter whenever the default is fine.")
    print("You can change almost all of these later from the front end's Preferences page.")

    print("\nStep 1 of 7 — system dependencies")
    options.dependency_mode = ask_choice(
        "Which bundled dependency set should I install?",
        options.dependency_mode,
        [
            ("full", "Full (recommended) — VLC, GitHub, LibreOffice, media/desktop/code tools"),
            ("with-optional", "Full + optional fallback screenshot/wallpaper programs"),
            ("required", "Core only — fastest install, some app tools will be unavailable"),
            ("none", "None — I will manage system packages myself"),
        ], input_fn)

    print("\nStep 2 of 7 — your AI model")
    options.model_source = ask_choice(
        "Which AI model should Milelik use by default?",
        options.model_source,
        [
            ("auto", f"Bundled {BUNDLED_MODEL_LABEL} (recommended) — already in models/, import it into Ollama"),
            ("gguf", "I already have a GGUF file — import that exact file into Ollama"),
            ("server", "A model is already loaded in LM Studio or llama.cpp — do not install another copy"),
            ("custom", "Use a different Ollama model (for example the larger Qwen2.5-Coder)"),
        ], input_fn)

    if options.model_source == "gguf":
        while True:
            options.gguf = ask_text(
                "Path to your GGUF file:",
                options.gguf or str(root / "models" / BUNDLED_MODEL_FILE), input_fn)
            options.gguf = str(expand_path(options.gguf))
            if Path(options.gguf).is_file():
                break
            print(f"  I can't find that file yet: {options.gguf}")
            print("  Check the path, or press Ctrl+C and choose the bundled model instead.")
        options.backend = "ollama"
    elif options.model_source == "server":
        server_kind = "llamacpp" if options.host == "http://127.0.0.1:8080" else "lmstudio"
        server_kind = ask_choice(
            "Which local model server are you using?", server_kind,
            [
                ("lmstudio", "LM Studio on port 1234"),
                ("llamacpp", "llama.cpp / llama-server on port 8080"),
                ("custom", "Another OpenAI-compatible local address"),
            ], input_fn)
        if server_kind == "lmstudio":
            options.host = "http://127.0.0.1:1234"
        elif server_kind == "llamacpp":
            options.host = "http://127.0.0.1:8080"
        else:
            options.host = ask_text(
                "Model server URL:", options.host or "http://127.0.0.1:1234", input_fn)
        if not options.model_was_set:
            options.model = "smollm2-135m-instruct-q4_1"
        options.model = ask_text("Model name/id shown by that server:", options.model, input_fn)
        options.backend = "openai"
    elif options.model_source == "custom":
        options.model = ask_text(
            "Ollama model to download/use:", options.model or "qwen3:4b", input_fn)
        options.model_was_set = True
        options.backend = "ollama"
    else:
        if not options.model_was_set:
            options.model = BUNDLED_OLLAMA_MODEL
            if not options.model_file:
                options.model_file = BUNDLED_MODEL_FILE
        options.backend = "auto"

    options.model_discovery = ask_yes_no(
        "Should Milelik read all AI model files in its models/ folder at startup and choose from them?",
        options.model_discovery, input_fn)
    if options.model_discovery:
        if options.models_dir == str(root / "models"):
            models_location = "repository"
        elif options.models_dir:
            models_location = "custom"
        else:
            models_location = "installed"
        models_location = ask_choice(
            "Which models folder should Milelik read?", models_location,
            [
                ("installed", "Installed Milelik models folder (~/.local/share/milelik/models)"),
                ("repository", f"This repository's models folder ({root / 'models'})"),
                ("custom", "Another folder"),
            ], input_fn)
        if models_location == "installed":
            options.models_dir = ""
        elif models_location == "repository":
            options.models_dir = str(root / "models")
        else:
            options.models_dir = ask_text(
                "Folder containing your AI model files:",
                options.models_dir or str(Path.home() / "models"), input_fn)
        if options.model_source != "gguf":
            options.model_file = ask_text(
                "Preferred model filename/path inside models/ (blank = Milelik chooses automatically):",
                options.model_file, input_fn)

    print("\nStep 3 of 7 — speed versus quality")
    options.performance_profile = ask_choice(
        "Pick a performance profile:", options.performance_profile,
        [
            ("balanced", "Balanced (recommended) — 8192 context, 12 tool steps"),
            ("fast", "Fast / lower RAM — 4096 context, 8 tool steps"),
            ("quality", "Quality / more RAM — 16384 context, 16 tool steps"),
            ("custom", "Custom — choose the numbers myself"),
        ], input_fn)
    options.ctx_was_set = options.max_steps_was_set = options.cmd_timeout_was_set = False
    apply_performance_profile(options)
    if options.performance_profile == "custom":
        options.ctx = ask_int("Model context size (tokens)", options.ctx, 1024, 131072, input_fn)
        options.max_steps = ask_int("Maximum tool steps per task", options.max_steps, 1, 50, input_fn)
        options.cmd_timeout = ask_int(
            "Default shell-command timeout (seconds)", options.cmd_timeout, 5, 900, input_fn)

    print("\nStep 4 of 7 — safety")
    auto_choice = ask_choice(
        "When Milelik wants to run a command or make a change:",
        "1" if options.auto_approve else "0",
        [
            ("0", "Ask me first (recommended)"),
            ("1", "Auto-approve ordinary actions (dangerous and sudo actions still ask)"),
        ], input_fn)
    options.auto_approve = auto_choice == "1"

    print("\nStep 5 of 7 — browser front end and workspace")
    options.frontend_enabled = ask_yes_no(
        "Install Milelik's browser front end, coding workspace and local APIs?",
        options.frontend_enabled, input_fn)
    if options.frontend_enabled:
        options.workspace = ask_text(
            "Where should Milelik keep your editable/runnable workspace?",
            options.workspace, input_fn)
        options.web_host = ask_text(
            "Front-end bind address (127.0.0.1 = only this PC; 0.0.0.0 = trusted LAN):",
            options.web_host, input_fn)
        options.web_port = ask_int("Front-end port", options.web_port, 1, 65535, input_fn)
        options.open_browser = ask_yes_no(
            "Open your browser automatically when the front end starts?",
            options.open_browser, input_fn)
        options.ui_theme = ask_choice(
            "Which front-end theme do you prefer?", options.ui_theme,
            [("system", "Match my system theme"), ("dark", "Dark"), ("light", "Light")],
            input_fn)
        options.ui_default_view = ask_choice(
            "Which page should Milelik open on?", options.ui_default_view,
            [
                ("chat", "Chat"),
                ("workspace", "Workspace / code runner"),
                ("apis", "APIs for coding, maths and utilities"),
                ("settings", "Preferences"),
            ], input_fn)

    print("\nStep 6 of 7 — GitHub (optional)")
    options.github_login = ask_yes_no(
        "Run GitHub login (gh auth login) after installation?",
        options.github_login, input_fn)

    print("\nStep 7 of 7 — what to open when installation finishes")
    if options.launch_was_set:
        print(f"Launch after install: {options.launch} (set by command line or environment)")
    elif options.frontend_enabled:
        default_launch = "frontend" if options.launch == "none" else options.launch
        options.launch = ask_choice(
            "After installing:", default_launch,
            [
                ("frontend", "Open the Milelik front end/workspace"),
                ("cli", "Show me the terminal command (do not open anything)"),
                ("none", "Finish quietly"),
            ], input_fn)
    else:
        options.launch = ask_choice(
            "After installing:", options.launch,
            [("cli", "Show me the terminal command"), ("none", "Finish quietly")],
            input_fn)

    print_summary(options)
    if not ask_yes_no("Proceed with these choices?", True, input_fn):
        print("Installation cancelled. Nothing was installed.")
        return False
    return True


def run_command(runner, command, *, env=None, cwd=None, **kwargs) -> int:
    try:
        result = runner([str(part) for part in command], env=env, cwd=cwd,
                        check=False, **kwargs)
    except OSError as error:
        print(f"Error: could not run {command[0]}: {error}", file=sys.stderr)
        return 1
    return int(getattr(result, "returncode", 0) or 0)


def should_open_browser_setup(options: InstallerOptions, environ,
                              stdin_isatty: bool, stdout_isatty: bool) -> bool:
    result = bool(options.browser_setup)
    if not result:
        result = (
            len(options.raw_argv) == 0 and not options.assume_yes
            and not options.force_wizard
            and bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")))
    if options.assume_yes or options.force_wizard or environ.get("MILELIK_BROWSER_SETUP_ACTIVE"):
        return False
    return result


def delegate_browser_setup(options: InstallerOptions, *, root: Path = ROOT,
                           runner=subprocess.run, environ=None) -> int | None:
    """Delegate to setup_milelik.py's browser setup, or None to fall back."""
    setup_path = root / "setup_milelik.py"
    if not setup_path.is_file():
        if options.browser_setup:
            print("Browser setup needs setup_milelik.py beside install_milelik.py.",
                  file=sys.stderr)
            return 2
        return None

    setup_args = ["--browser-setup"]
    if options.skip_check:
        setup_args.append("--skip-check")
    if options.model_was_set:
        setup_args.extend(["--model", options.model])
    if options.gguf:
        setup_args.extend(["--gguf", options.gguf])
    if options.models_dir:
        setup_args.extend(["--models-dir", options.models_dir])
    if options.model_file:
        setup_args.extend(["--model-file", options.model_file])
    if not options.model_discovery:
        setup_args.append("--no-model-scan")
    if options.backend != "auto":
        setup_args.extend(["--backend", options.backend])
    if options.host:
        setup_args.extend(["--host", options.host])
    setup_args.extend([
        "--dependency-mode", options.dependency_mode,
        "--workspace", options.workspace,
        "--web-host", options.web_host,
        "--web-port", str(options.web_port),
    ])
    if options.launch_was_set:
        setup_args.append({
            "frontend": "--gui", "cli": "--cli", "none": "--no-launch",
        }[options.launch])

    env = dict(os.environ if environ is None else environ)
    env.update({
        "MILELIK_BROWSER_SETUP_ACTIVE": "1",
        "MODEL_SOURCE": options.model_source,
        "PERFORMANCE_PROFILE": options.performance_profile,
        "AUTO_APPROVE": "1" if options.auto_approve else "0",
        "FRONTEND_ENABLED": "1" if options.frontend_enabled else "0",
        "OPEN_BROWSER": "1" if options.open_browser else "0",
        "UI_THEME": options.ui_theme,
        "UI_DEFAULT_VIEW": options.ui_default_view,
        "GITHUB_LOGIN": "1" if options.github_login else "0",
    })
    print("Opening Milelik setup in your browser…")
    return run_command(runner, [sys.executable, setup_path, *setup_args], env=env)


def load_core(root: Path = ROOT):
    core_path = root / "milelik.py"
    if not core_path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("milelik_installer_core", core_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def install_dependencies(options: InstallerOptions, *, root: Path = ROOT,
                         runner=subprocess.run, env=None) -> int:
    mode = options.dependency_mode
    script = root / "dependencies" / "install_dependencies.py"
    mode_args = {
        "full": "--full",
        "required": "--required",
        "minimal": "--required",
        "with-optional": "--with-optional",
        "optional": "--with-optional",
    }
    if mode in ("none", "skip"):
        print(f"Skipping bundled dependency installation (DEPENDENCY_MODE={mode}).")
        return 0
    if mode not in mode_args:
        print(f"Invalid DEPENDENCY_MODE: {mode}", file=sys.stderr)
        print("Use full, with-optional, required or none.", file=sys.stderr)
        return 2
    if not script.is_file():
        print(f"Bundled dependency installer not found: {script}", file=sys.stderr)
        return 1
    return run_command(runner, [sys.executable, script, mode_args[mode]], env=env)


def ollama_api_ready(opener=urllib.request.urlopen) -> bool:
    try:
        with opener(OLLAMA_TAGS_URL, timeout=2) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def find_ollama(which=shutil.which, home: Path | None = None) -> str:
    found = which("ollama")
    if found:
        return str(found)
    home = Path(home) if home is not None else Path.home()
    for candidate in (home / ".local" / "bin" / "ollama",
                      home / "bin" / "ollama",
                      Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"),
                      Path("/opt/ollama/bin/ollama"), Path("/snap/bin/ollama")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def _download_to_file(url: str, destination: Path, opener=urllib.request.urlopen) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Milelik-Installer/1.0"})
    with opener(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ollama_download_base(env=None, root=ROOT) -> tuple[str, str]:
    """Return ``(base_url, source_label)`` for the Ollama release archive.

    Preference order: the ``OLLAMA_DOWNLOAD_BASE`` environment override,
    the version pinned in the repository's ``ollama/ollama.json`` manifest,
    then Ollama's rolling latest-release URL.
    """
    env = os.environ if env is None else env
    override = str(env.get("OLLAMA_DOWNLOAD_BASE", "") or "").strip().rstrip("/")
    if override:
        return override, "OLLAMA_DOWNLOAD_BASE"
    try:
        manifest = json.loads((Path(root) / "ollama" / "ollama.json").read_text(
            encoding="utf-8"))
        base = str(manifest.get("download_base", "") or "").strip().rstrip("/")
        if base:
            pinned = manifest.get("pinned_version", "")
            label = f"ollama/ollama.json ({pinned})" if pinned else "ollama/ollama.json"
            return base, label
    except (OSError, ValueError):
        pass
    return ("https://github.com/ollama/ollama/releases/latest/download",
            "Ollama latest release")


def _load_ollama_setup_module():
    """Import ollama/setup_ollama.py for vendored-runtime support, or None."""
    script = ROOT / "ollama" / "setup_ollama.py"
    if not script.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("milelik_ollama_setup", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (OSError, ValueError, ImportError, AttributeError):
        return None


def _find_matching_vendored_ollama(filename: str):
    """Return ``(setup_module, vendor_dir, vendor)`` for a matching asset.

    Returns None when no usable vendored Ollama chunks for ``filename``
    are committed in ollama/vendor/.
    """
    setup_module = _load_ollama_setup_module()
    if setup_module is None:
        return None
    try:
        found = setup_module.find_vendored()
    except ValueError:
        return None
    if found is None or found[1].get("asset") != filename:
        return None
    return setup_module, found[0], found[1]


def install_ollama_user_local(home: Path | None = None, *, environ=None,
                              opener=urllib.request.urlopen,
                              machine=platform.machine) -> Path | None:
    """Install Ollama under ~/.local without running a shell script.

    This assembles the vendored ollama/vendor/ chunks when they are
    committed (fully offline), and otherwise downloads Ollama's official
    linux release archive, verifies its published SHA-256 when the
    checksum file is available, and extracts it with zstd/tar.
    """
    env = os.environ if environ is None else environ
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        str(machine()).lower())
    if not arch:
        print(f"Unsupported architecture for automatic Ollama installation: {machine()}",
              file=sys.stderr)
        return None
    if not shutil.which("zstd") or not shutil.which("tar"):
        print("Installing Ollama needs the zstd and tar commands.", file=sys.stderr)
        print("Install Milelik's required dependencies first, then try again.",
              file=sys.stderr)
        return None

    home = Path(home) if home is not None else Path.home()
    destination = home / ".local"
    filename = f"ollama-linux-{arch}.tar.zst"
    base, base_source = ollama_download_base(env, ROOT)
    if arch == "amd64":
        print("Using Ollama's linux/amd64 build (correct for Intel and AMD 64-bit PCs).")

    vendored = _find_matching_vendored_ollama(filename)

    try:
        with tempfile.TemporaryDirectory(prefix="milelik-ollama-") as tmp:
            archive = Path(tmp) / filename
            if vendored is not None:
                setup_module, vendor_dir, vendor = vendored
                print(f"Assembling vendored Ollama {vendor.get('pinned_version', '')} "
                      f"for linux/{arch} (offline, no download)…")
                try:
                    setup_module.assemble_archive(vendor_dir, vendor, archive)
                except ValueError as error:
                    print(f"Vendored Ollama chunks are unusable: {error}",
                          file=sys.stderr)
                    return None
                print("Vendored Ollama chunks verified (SHA-256).")
            else:
                print(f"Downloading Ollama for linux/{arch} from {base_source}…")
                _download_to_file(f"{base}/{filename}", archive, opener=opener)

                try:
                    sums_path = Path(tmp) / "sha256sum.txt"
                    _download_to_file(f"{base}/sha256sum.txt", sums_path, opener=opener)
                    expected = ""
                    for line in sums_path.read_text(encoding="utf-8", errors="replace").splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and Path(parts[-1]).name == filename:
                            expected = parts[0].lower()
                            break
                    if expected and _sha256(archive) != expected:
                        print("The downloaded Ollama archive failed its SHA-256 check.",
                              file=sys.stderr)
                        return None
                except (OSError, urllib.error.URLError):
                    print("Warning: could not fetch Ollama's checksum file; continuing.")

            (destination / "bin").mkdir(parents=True, exist_ok=True)
            old_lib = destination / "lib" / "ollama"
            if old_lib.exists():
                shutil.rmtree(old_lib)
            (destination / "lib" / "ollama").mkdir(parents=True, exist_ok=True)

            decompressor = subprocess.Popen(
                ["zstd", "-dc", str(archive)], stdout=subprocess.PIPE)
            if decompressor.stdout is None:
                return None
            try:
                extract = subprocess.run(
                    ["tar", "-xf", "-", "-C", str(destination)],
                    stdin=decompressor.stdout, check=False)
            finally:
                decompressor.stdout.close()
            if decompressor.wait() or extract.returncode:
                print("Could not extract the Ollama release archive.", file=sys.stderr)
                return None
    except (OSError, urllib.error.URLError) as error:
        print(f"Could not download Ollama: {error}", file=sys.stderr)
        return None

    executable = destination / "bin" / "ollama"
    if executable.is_file():
        executable.chmod(0o755)
        return executable
    print(f"Ollama executable was not found after extraction: {executable}",
          file=sys.stderr)
    return None


def find_model_by_pattern(directory, pattern: str, max_depth: int = 6) -> Path | None:
    root = expand_path(directory)
    if not root.is_dir():
        return None
    root_depth = len(root.resolve().parts)
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        try:
            depth = len(current_path.resolve().parts) - root_depth
        except OSError:
            depth = 0
        if depth >= max_depth:
            dirnames[:] = []
        for filename in sorted(filenames):
            if fnmatch.fnmatch(filename.lower(), pattern.lower()):
                candidate = current_path / filename
                if candidate.is_file():
                    return candidate
    return None


def ensure_model(options: InstallerOptions, *, root: Path = ROOT,
                 home: Path | None = None, core=None,
                 runner=subprocess.run, which=shutil.which, env=None,
                 process_starter=subprocess.Popen, ready=ollama_api_ready,
                 sleep=time.sleep, ollama_installer=None) -> int:
    home = Path(home) if home is not None else Path.home()
    if options.model_source == "server" or options.backend == "openai":
        print("Using your existing local model server; not installing Ollama or downloading a model copy.")
        options.backend = "openai"
        if not options.host:
            options.host = "http://127.0.0.1:1234"
        return 0

    ollama = find_ollama(which, home)
    if not ollama:
        if options.offline:
            print("Offline mode: Ollama was not found in PATH, "
                  "~/.local/bin, ~/bin, /usr/local/bin, /usr/bin, "
                  "/opt/ollama/bin or /snap/bin, so nothing was downloaded.")
            print("Install Ollama, re-run without --offline, or serve a local GGUF")
            print("with the packaged engine:  python3 engine/serve.py --model /path/to/model.gguf")
            return 1
        print("Installing Ollama…")
        installer = ollama_installer or install_ollama_user_local
        installed = installer(home=home, environ=env)
        if not installed:
            return 1
        ollama = str(installed)

    # Make a user-local Ollama discoverable by Milelik subprocesses even when
    # ~/.local/bin has not yet joined this shell's PATH.
    if env is not None:
        bin_dir = str(Path(ollama).resolve().parent)
        env["PATH"] = bin_dir + (os.pathsep + env["PATH"] if env.get("PATH") else "")

    if which("systemctl") and home not in Path(ollama).resolve().parents:
        run_command(runner, ["sudo", "systemctl", "enable", "--now", "ollama"],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not ready():
        print("Starting Ollama in the background…")
        try:
            process_starter([ollama, "serve"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            env=env)
        except OSError as error:
            print(f"Could not start Ollama: {error}", file=sys.stderr)
            return 1
    print("Waiting for Ollama", end="", flush=True)
    for _ in range(50):
        if ready():
            break
        print(".", end="", flush=True)
        sleep(0.2)
    print()

    if core is not None and hasattr(core, "resolve_ollama_model_name"):
        options.model = str(core.resolve_ollama_model_name(options.model))

    candidate = expand_path(options.gguf) if options.gguf else None
    if candidate is None and (
        options.model_source == "gguf"
        or (options.model_source == "auto" and (
            not options.model_was_set
            or options.model in (BUNDLED_OLLAMA_MODEL, TARGET_OLLAMA_MODEL)))
    ):
        pattern = ("*qwen2.5-coder*7b*abliterat*q4_k_m*.gguf"
                   if options.model == TARGET_OLLAMA_MODEL
                   else "*smollm2*135m*q4_1*.gguf")
        app_models = home / ".local" / "share" / "milelik" / "models"
        directories = [
            options.models_dir, root / "models", app_models, Path.cwd() / "models",
            root, Path.cwd(), home / "Downloads", home / "models", home / "Models",
            home / ".lmstudio" / "models", home / ".cache" / "lm-studio" / "models",
        ]
        for directory in directories:
            if not directory:
                continue
            candidate = find_model_by_pattern(directory, pattern)
            if candidate is not None:
                break

    if candidate is not None and candidate.is_file():
        if not options.model_file:
            options.model_file = str(candidate)
        print(f"Found your GGUF: {candidate}")
        print("Importing it (no download)…")
        if core is not None and hasattr(core, "ollama_name_for_model_file"):
            options.model = str(core.ollama_name_for_model_file(str(candidate)))
        model_env = dict(env or os.environ)
        model_env["OLLAMA_NAME"] = options.model
        code = run_command(
            runner,
            [sys.executable, root / "setup_model.py", candidate],
            env=model_env)
        if code:
            return code
        options.backend = "ollama"
        return 0

    if options.model_source == "gguf":
        print("No GGUF file was found to import.", file=sys.stderr)
        print("Run the installer again and choose the bundled model, or use:",
              file=sys.stderr)
        print("  GGUF=/path/to/model.gguf python3 install_milelik.py --yes",
              file=sys.stderr)
        return 1

    if options.offline:
        print(f"Offline mode: model '{options.model}' is not installed and "
              "no local GGUF was found, so nothing was downloaded.")
        print("Import it with:  python3 setup_model.py /path/to/model.gguf")
        return 1
    if options.model == BUNDLED_OLLAMA_MODEL:
        print("Bundled GGUF was not found; downloading SmolLM2 135M from Ollama…")
        options.model = "smollm2:135m"
    else:
        print(f"Downloading model '{options.model}' (first time only)…")
    return run_command(runner, [ollama, "pull", options.model], env=env)


def _copy_file(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(mode)


def _replace_tree(source: Path, destination: Path) -> None:
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True)


def _link_or_copy(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
        return
    except OSError:
        pass
    try:
        destination.symlink_to(source)
        return
    except OSError:
        pass
    shutil.copy2(source, destination)


def _write_python_launcher(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"os.execv(sys.executable, [sys.executable, {str(target)!r}, *sys.argv[1:]])\n",
        encoding="utf-8")
    path.chmod(0o755)


def install_files(options: InstallerOptions, *, root: Path = ROOT,
                  home: Path | None = None) -> tuple[Path, Path, Path]:
    home = Path(home) if home is not None else Path.home()
    app_dir = home / ".local" / "share" / "milelik"
    bin_dir = home / ".local" / "bin"
    desktop_dir = home / ".local" / "share" / "applications"
    workspace_dir = expand_path(options.workspace)
    for directory in (app_dir, bin_dir, desktop_dir, workspace_dir):
        directory.mkdir(parents=True, exist_ok=True)

    _copy_file(root / "milelik.py", app_dir / "milelik.py", 0o755)
    if options.frontend_enabled:
        _copy_file(root / "milelik_web.py", app_dir / "milelik_web.py", 0o755)
    elif (app_dir / "milelik_web.py").exists():
        (app_dir / "milelik_web.py").unlink()

    for extra in ("app.py", "install_milelik.py", "setup_model.py",
                  "setup_milelik.py", "uninstall_milelik.py"):
        source = root / extra
        if source.is_file():
            _copy_file(source, app_dir / extra, 0o755)
    modelfile = root / "Modelfile.qwen2.5-coder-abliterated"
    if modelfile.is_file():
        _copy_file(modelfile, app_dir / modelfile.name, 0o644)

    if (root / "dependencies").is_dir():
        _replace_tree(root / "dependencies", app_dir / "dependencies")
        dependency_script = app_dir / "dependencies" / "install_dependencies.py"
        if dependency_script.is_file():
            dependency_script.chmod(0o755)

    frontend_dest = app_dir / "frontend"
    if frontend_dest.is_symlink() or frontend_dest.is_file():
        frontend_dest.unlink()
    elif frontend_dest.exists():
        shutil.rmtree(frontend_dest)
    if options.frontend_enabled and (root / "frontend").is_dir():
        shutil.copytree(root / "frontend", frontend_dest, symlinks=True)

    if (root / "svg-images").is_dir():
        _replace_tree(root / "svg-images", app_dir / "svg-images")

    if (root / "engine").is_dir():
        _replace_tree(root / "engine", app_dir / "engine")
        for mode_name, mode in (("llama-server", 0o755), ("serve.py", 0o755),
                                ("ollama_compat.py", 0o644)):
            candidate = app_dir / "engine" / mode_name
            if candidate.is_file():
                candidate.chmod(mode)

    models_dest = app_dir / "models"
    models_dest.mkdir(parents=True, exist_ok=True)
    for extra in ("README.md", ".gitkeep", "LICENSE-SmolLM2-Apache-2.0.txt"):
        source = root / "models" / extra
        if source.is_file():
            _copy_file(source, models_dest / extra, 0o644)
    source_models = root / "models"
    if source_models.is_dir():
        for current, _dirnames, filenames in os.walk(source_models, followlinks=False):
            current_path = Path(current)
            for filename in filenames:
                source = current_path / filename
                if source.suffix.lower() not in MODEL_EXTENSIONS:
                    continue
                _link_or_copy(source, models_dest / source.relative_to(source_models))

    for extra in ("requirements.txt", "pyproject.toml"):
        source = root / extra
        if source.is_file():
            _copy_file(source, app_dir / extra, 0o644)

    _write_python_launcher(bin_dir / "milelik", app_dir / "milelik.py")
    if options.frontend_enabled:
        _write_python_launcher(bin_dir / "milelik-web", app_dir / "milelik_web.py")
        if (app_dir / "app.py").is_file():
            _write_python_launcher(bin_dir / "milelik-app", app_dir / "app.py")
    else:
        for name in ("milelik-web", "milelik-app"):
            if (bin_dir / name).exists():
                (bin_dir / name).unlink()

    launcher_targets = {
        "milelik-setup": app_dir / "setup_milelik.py",
        "milelik-install": app_dir / "install_milelik.py",
        "milelik-model-setup": app_dir / "setup_model.py",
        "milelik-engine": app_dir / "engine" / "serve.py",
        "milelik-uninstall": app_dir / "uninstall_milelik.py",
    }
    for name, target in launcher_targets.items():
        launcher = bin_dir / name
        if target.is_file():
            _write_python_launcher(launcher, target)
        elif launcher.exists():
            launcher.unlink()

    desktop = root / "milelik.desktop"
    if desktop.is_file():
        _copy_file(desktop, desktop_dir / desktop.name, 0o644)
    workspace_desktop = root / "milelik-workspace.desktop"
    installed_workspace_desktop = desktop_dir / "milelik-workspace.desktop"
    if options.frontend_enabled and workspace_desktop.is_file():
        _copy_file(workspace_desktop, installed_workspace_desktop, 0o644)
    elif installed_workspace_desktop.exists():
        installed_workspace_desktop.unlink()

    return app_dir, bin_dir, workspace_dir


def save_config(options: InstallerOptions, *, home: Path | None = None,
                environ=None) -> Path:
    home = Path(home) if home is not None else Path.home()
    env = os.environ if environ is None else environ
    config_home = expand_path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") \
        else home / ".config"
    path = config_home / "milelik" / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        config = {}
    config.update({
        "model": options.model,
        "backend": options.backend,
        "host": options.host,
        "models_dir": options.models_dir,
        "model_file": options.model_file,
        "model_discovery": bool(options.model_discovery),
        "model_source": options.model_source,
        "dependency_mode": options.dependency_mode,
        "performance_profile": options.performance_profile,
        "ctx": int(options.ctx),
        "max_steps": int(options.max_steps),
        "cmd_timeout": int(options.cmd_timeout),
        "auto": bool(options.auto_approve),
        "workspace": str(expand_path(options.workspace)),
        "web_host": options.web_host,
        "web_port": int(options.web_port),
        "open_browser": bool(options.open_browser),
        "frontend_enabled": bool(options.frontend_enabled),
        "ui_theme": options.ui_theme,
        "ui_default_view": options.ui_default_view,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Saved your Milelik preferences in {path}")
    return path


def validate_options(options: InstallerOptions) -> None:
    if options.model_source not in ("auto", "gguf", "server", "custom"):
        raise InstallerError(f"Invalid MODEL_SOURCE: {options.model_source}")
    if options.backend not in ("auto", "ollama", "openai"):
        raise InstallerError(
            f"Invalid BACKEND: {options.backend} (use auto, ollama or openai)")
    if options.launch not in ("frontend", "cli", "none"):
        raise InstallerError(f"Invalid LAUNCH_AFTER_INSTALL: {options.launch}")
    if not 1 <= int(options.web_port) <= 65535:
        raise InstallerError(f"Invalid WEB_PORT: {options.web_port}")
    apply_performance_profile(options)


def normalize_paths(options: InstallerOptions) -> None:
    if options.gguf:
        options.gguf = str(expand_path(options.gguf))
    if options.models_dir:
        options.models_dir = str(expand_path(options.models_dir))
    if options.model_file and (
        "/" in options.model_file or options.model_file.startswith("~")):
        options.model_file = str(expand_path(options.model_file))
    if not options.model_file and options.gguf:
        options.model_file = options.gguf
    options.workspace = str(expand_path(options.workspace))


def main(argv=None, *, runner=subprocess.run, input_fn=input,
         stdin_isatty=None, stdout_isatty=None, environ=None,
         root: Path = ROOT, home: Path | None = None, euid=None,
         which=shutil.which, process_starter=subprocess.Popen,
         ready=ollama_api_ready, sleep=time.sleep, core="auto",
         ollama_installer=None) -> int:
    env = dict(os.environ if environ is None else environ)
    home = Path(home) if home is not None else Path.home()
    try:
        options = parse_args(argv, environ=env, home=home)
    except InstallerError as error:
        print(str(error), file=sys.stderr)
        return 2

    effective_euid = (os.geteuid() if hasattr(os, "geteuid") else 0) \
        if euid is None else euid
    if effective_euid == 0 and env.get("MILELIK_ALLOW_ROOT") != "1":
        print("Error: do not run the Milelik installer as root/sudo.", file=sys.stderr)
        print("Run it as your normal desktop user:", file=sys.stderr)
        print("  python3 setup_milelik.py", file=sys.stderr)
        print("The installer requests sudo only for system packages/services. Running",
              file=sys.stderr)
        print("the whole installer as root would install Milelik under /root instead",
              file=sys.stderr)
        print("of your own account. Set MILELIK_ALLOW_ROOT=1 only for a root-only test",
              file=sys.stderr)
        print("environment.", file=sys.stderr)
        return 2

    if stdin_isatty is None:
        stdin_isatty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if stdout_isatty is None:
        stdout_isatty = bool(getattr(sys.stdout, "isatty", lambda: False)())

    if should_open_browser_setup(options, env, stdin_isatty, stdout_isatty):
        delegated = delegate_browser_setup(
            options, root=root, runner=runner, environ=env)
        if delegated is not None:
            return delegated

    interactive = bool(options.force_wizard or (
        not options.assume_yes and stdin_isatty and stdout_isatty))
    print("Installing Milelik")
    try:
        if interactive:
            if not run_setup_wizard(options, root=root, input_fn=input_fn):
                try:
                    return int(env.get("MILELIK_WIZARD_CANCEL_EXIT", "0"))
                except ValueError:
                    return 0
        else:
            print("(non-interactive/default mode — run python3 setup_milelik.py for browser setup)")
            print_summary(options)
        validate_options(options)
    except InstallerError as error:
        print(str(error), file=sys.stderr)
        return 2
    normalize_paths(options)

    subprocess_env = dict(os.environ)
    subprocess_env.update(env)

    code = install_dependencies(
        options, root=root, runner=runner, env=subprocess_env)
    if code:
        return code

    if sys.version_info < (3, 10):
        print("Milelik needs Python 3.10 or newer.", file=sys.stderr)
        print("Install the repository's dependencies with:", file=sys.stderr)
        print(f"  python3 {root / 'dependencies' / 'install_dependencies.py'} --required",
              file=sys.stderr)
        return 1
    if not which("curl"):
        print("Milelik's installer needs curl.", file=sys.stderr)
        print("Install the repository's dependencies with:", file=sys.stderr)
        print(f"  python3 {root / 'dependencies' / 'install_dependencies.py'} --required",
              file=sys.stderr)
        return 1

    if core == "auto":
        core = load_core(root)
    code = ensure_model(
        options, root=root, home=home, core=core, runner=runner, which=which,
        env=subprocess_env, process_starter=process_starter, ready=ready,
        sleep=sleep, ollama_installer=ollama_installer)
    if code:
        return code

    try:
        app_dir, bin_dir, workspace_dir = install_files(
            options, root=root, home=home)
        save_config(options, home=home, environ=env)
    except (OSError, shutil.Error) as error:
        print(f"Error: could not install Milelik's files: {error}", file=sys.stderr)
        return 1

    path_parts = [part for part in subprocess_env.get("PATH", "").split(os.pathsep) if part]
    if str(bin_dir) not in path_parts:
        print()
        print(f"Warning: {bin_dir} is not on your PATH in this shell.")
        print("   linux Mint adds it automatically at login - open a NEW terminal,")
        print('   or run:  export PATH="$HOME/.local/bin:$PATH"')

    print()
    if options.skip_check:
        print("Skipping final check (--skip-check).")
    else:
        run_command(runner, [sys.executable, app_dir / "milelik.py", "--check"],
                    env=subprocess_env)

    if options.github_login:
        print()
        if which("gh"):
            print("Starting GitHub login…")
            code = run_command(runner, ["gh", "auth", "login"], env=subprocess_env)
            if code:
                print("GitHub login was not completed; run 'gh auth login' whenever you're ready.")
        else:
            print("GitHub CLI (gh) is not available, so login was skipped.")

    if options.launch == "frontend":
        if options.frontend_enabled:
            print()
            print("Opening the Milelik web app in the background…")
            command = [
                sys.executable, app_dir / "milelik.py", "--gui",
                "--workspace-dir", workspace_dir, "--web-host", options.web_host,
                "--web-port", str(options.web_port),
                "--open-browser" if options.open_browser else "--no-open-browser",
            ]
            try:
                log_path = app_dir / "frontend.log"
                with log_path.open("ab") as log:
                    process_starter(
                        [str(part) for part in command], stdout=log,
                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        start_new_session=True, env=subprocess_env)
                print(f"Front-end log/API token: {log_path}")
            except OSError as error:
                print(f"Could not start the Milelik web app: {error}", file=sys.stderr)
        else:
            print("Front-end launch requested, but the front end was disabled in preferences.")

    print()
    print("Done. Open Milelik your way:")
    if options.frontend_enabled:
        print("   Web app + workspace + APIs:  milelik --gui")
        print("   (also available as the Milelik app and: milelik-web)")
    print("   Chat in a terminal:            milelik --interactive")
    print('   One task:                      milelik "what is my local IP, public IP, and disk space?"')
    print("   Check my setup:                milelik --check")
    print(f"   Your workspace:                {workspace_dir}")
    print("   GitHub account (once):         gh auth login")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
