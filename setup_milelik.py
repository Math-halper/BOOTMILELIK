#!/usr/bin/env python3
"""Set up and run Milelik from a source checkout.

Run it with:

    python3 setup_milelik.py

The script uses only the Python standard library. On a desktop it opens a
local browser setup page instead of asking setup questions in the terminal.
It checks the local models/ folder, runs Milelik's bundled installer (unless
asked not to), and then starts the browser front end, terminal chat, one
task, --check, or the model list. The heavy system-package and model
provisioning work stays in install_milelik.py, the Python installer shipped
in this repository. Use --terminal-setup if you prefer the old terminal
questions, or --yes for unattended defaults.
"""

from __future__ import annotations

import argparse
import html
import http.server
import importlib.util
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE_PATH = ROOT / "milelik.py"
INSTALLER_PATH = ROOT / "install_milelik.py"
MIN_PYTHON = (3, 10)
LAUNCH_CHOICES = ("gui", "cli", "none")
INSTALLER_CANCELLED = 10
COMPANION_SPRITE_FILENAME = "milelik-companion.svg"
APP_ICON_FILENAME = "milelik-icon.svg"


def expand_path(value) -> Path:
    """Expand a user-supplied path without requiring it to exist yet."""
    return Path(os.path.expanduser(str(value)))


def is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def load_core(root: Path = ROOT):
    """Load milelik.py for its model-folder helpers, if it is available."""
    core_path = root / "milelik.py"
    if not core_path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("milelik_setup_core", core_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def ensure_models_folder(models_dir="", root: Path = ROOT) -> Path:
    """Make sure the repository and any custom models folders exist."""
    primary = expand_path(models_dir) if models_dir else root / "models"
    if not primary.is_absolute():
        primary = Path.cwd() / primary
    primary.mkdir(parents=True, exist_ok=True)

    # The repository/installed models folder is part of the normal Milelik
    # search path even when the user also supplies a custom folder.
    root_models = root / "models"
    if root_models != primary:
        root_models.mkdir(parents=True, exist_ok=True)
    return primary


def model_summary(core, models_dir="", model_file=""):
    """Return (directories, records, selected record, config)."""
    if core is None or not hasattr(core, "discover_model_files"):
        return [], [], None, {}
    try:
        cfg = core.load_config() if hasattr(core, "load_config") else {}
        directories = core.model_directories(models_dir or None, cfg=cfg)
        records = core.discover_model_files(directories, cfg=cfg)
        selector = model_file or cfg.get("model_file") or ""
        selected = core.choose_model_file(
            records,
            configured_model=str(cfg.get("model") or ""),
            selected_model_file=str(selector),
            interactive=False,
        ) if records else None
        return directories, records, selected, cfg
    except Exception:
        return [], [], None, {}


def print_model_summary(core, models_dir="", model_file="") -> None:
    directories, records, selected, _cfg = model_summary(core, models_dir, model_file)
    if not directories:
        print("Model folder check: Milelik's model helpers could not be loaded.")
        return
    runnable = [record for record in records if record.get("supported")]
    print("Model folders checked:")
    for directory in directories:
        print(f"  - {directory}")
    print(f"Model files found: {len(records)} total, {len(runnable)} runnable GGUF file(s).")
    if selected and selected.get("supported"):
        print(f"Model Milelik will choose: {selected.get('name')} ({selected.get('size_human')})")
        print(f"  {selected.get('path')}")
    elif selected:
        print(f"Selected model file is not runnable: {selected.get('name')}")
        if selected.get("reason"):
            print(f"  {selected.get('reason')}")
    elif records:
        print("No runnable GGUF was selected from those files.")
    else:
        print(f"Put a .gguf file in {directories[0]} or let the installer download the default model.")


def resolve_installed_command(home=None, which=shutil.which) -> Path | None:
    """Find the installed Milelik command, if installation has already run."""
    home = Path(home) if home is not None else Path.home()
    candidate = home / ".local" / "bin" / "milelik"
    if is_executable_file(candidate):
        return candidate
    found = which("milelik")
    return Path(found) if found else None


def installed_app_path(home=None) -> Path:
    """Return the per-user installed copy of milelik.py."""
    home = Path(home) if home is not None else Path.home()
    return home / ".local" / "share" / "milelik" / "milelik.py"


def installed_launch_prefix(installed_command=None, home=None,
                            python_executable=None):
    """Return a launch prefix that does not depend on a wrapper's +x bit.

    The normal `~/.local/bin/milelik` wrapper is useful in a terminal, but it
    cannot run if an extractor dropped its executable bit or if the home
    filesystem is mounted `noexec`. In those cases, running the installed
    Python file with the current interpreter is equivalent and reliable.
    """
    app_file = installed_app_path(home)
    if app_file.is_file():
        return [python_executable or sys.executable, str(app_file)]
    if not installed_command:
        return []
    if isinstance(installed_command, (list, tuple)):
        return [str(part) for part in installed_command]
    return [str(installed_command)]


def run_command(command, *, dry_run=False, runner=subprocess.run, cwd=None,
                env=None) -> int:
    """Run one command, or print it in dry-run mode."""
    printable = shlex.join([str(part) for part in command])
    if dry_run:
        print(f"Dry run: {printable}", flush=True)
        return 0
    print(f"Running: {printable}", flush=True)
    sys.stdout.flush()
    try:
        result = runner([str(part) for part in command], cwd=cwd, env=env)
    except KeyboardInterrupt:
        return 130
    except PermissionError as error:
        print(f"Error: could not run {command[0]}: {error}", file=sys.stderr)
        print("Permission denied here usually means a missing executable bit "
              "or a filesystem mounted with noexec. sudo does not bypass "
              "noexec; run Milelik's Python files through python3 or move Milelik "
              "into your home folder.", file=sys.stderr)
        return 126
    except OSError as error:
        print(f"Error: could not run {command[0]}: {error}", file=sys.stderr)
        return 1
    return int(getattr(result, "returncode", 0) or 0)


def ask_yes_no(question: str, default=True, input_fn=input) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = str(input_fn(f"{question} {suffix} ") or "").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return default
        if not answer:
            return default
        if answer in ("y", "yes", "true", "1", "on"):
            return True
        if answer in ("n", "no", "false", "0", "off"):
            return False
        print("Please answer y or n.")


def display_available(environ=None) -> bool:
    """Return True when opening a desktop browser is likely to work."""
    env = os.environ if environ is None else environ
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")
                or sys.platform in ("darwin", "win32"))


def load_sprite_asset(filename: str, root: Path = ROOT) -> str:
    """Load one canonical SVG from svg-images/."""
    asset_path = root / "svg-images" / Path(filename).name
    try:
        return asset_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_sprite_svg(root: Path = ROOT) -> str:
    """Load Milelik's companion from the organized svg-images folder."""
    sprite_svg = load_sprite_asset(COMPANION_SPRITE_FILENAME, root)
    if sprite_svg:
        return sprite_svg
    icon_svg = load_sprite_asset(APP_ICON_FILENAME, root)
    if icon_svg and 'class="milelik-sprite"' not in icon_svg:
        return icon_svg.replace("<svg", '<svg class="milelik-sprite"', 1)
    return icon_svg


def load_app_icon_svg(root: Path = ROOT) -> str:
    """Load Milelik's browser icon from the organized svg-images folder."""
    return load_sprite_asset(APP_ICON_FILENAME, root)


def _setup_pick(*values, default=""):
    for value in values:
        if value is not None and str(value) != "":
            return value
    return default


def browser_setup_defaults(args, cfg=None, selected=None, root: Path = ROOT, core=None):
    """Initial values for the browser setup form."""
    cfg = cfg or {}
    bundled = getattr(core, "BUNDLED_MODEL_FILENAME", "smollm2-135m-instruct-q4_1.gguf")
    model_source = _setup_pick(
        os.environ.get("MODEL_SOURCE"), cfg.get("model_source"), default="auto")
    if getattr(args, "gguf", ""):
        model_source = "gguf"
    def env_bool(name, default):
        if name not in os.environ:
            return default
        return str(os.environ.get(name) or "").lower() in ("1", "true", "yes", "on")

    try:
        web_port = int(_setup_pick(
            getattr(args, "web_port", None), os.environ.get("WEB_PORT"),
            cfg.get("web_port"), default=8765))
    except (TypeError, ValueError):
        web_port = 8765
    open_browser = getattr(args, "open_browser", None)
    if open_browser is None:
        open_browser = env_bool("OPEN_BROWSER", cfg.get("open_browser", True))
    return {
        "dependency_mode": str(_setup_pick(
            getattr(args, "dependency_mode", ""), cfg.get("dependency_mode"),
            default="full")),
        "model_source": str(model_source),
        "gguf": str(_setup_pick(
            getattr(args, "gguf", ""), default=str(root / "models" / bundled))),
        "model": str(_setup_pick(getattr(args, "model", ""), cfg.get("model"))),
        "backend": str(_setup_pick(
            getattr(args, "backend", ""), cfg.get("backend"), default="auto")),
        "host": str(_setup_pick(
            getattr(args, "host", ""), cfg.get("host"),
            default="http://127.0.0.1:1234")),
        "models_dir": str(_setup_pick(
            getattr(args, "models_dir", ""), cfg.get("models_dir"))),
        "model_file": str(_setup_pick(
            (selected or {}).get("path") if getattr(args, "model_file", "") else "",
            getattr(args, "model_file", ""))),
        "model_discovery": bool(
            not getattr(args, "no_model_scan", False)
            and cfg.get("model_discovery", True) is not False),
        "performance_profile": str(_setup_pick(
            os.environ.get("PERFORMANCE_PROFILE"), cfg.get("performance_profile"),
            default="balanced")),
        "auto_approve": env_bool("AUTO_APPROVE", bool(cfg.get("auto", False))),
        "frontend_enabled": env_bool(
            "FRONTEND_ENABLED", cfg.get("frontend_enabled", True) is not False),
        "workspace": str(_setup_pick(
            getattr(args, "workspace", ""), os.environ.get("WORKSPACE_DIR"),
            cfg.get("workspace"), default=str(Path.home() / "MilelikWorkspace"))),
        "web_host": str(_setup_pick(
            getattr(args, "web_host", ""), os.environ.get("WEB_HOST"),
            cfg.get("web_host"), default="127.0.0.1")),
        "web_port": web_port,
        "open_browser": bool(open_browser),
        "ui_theme": str(_setup_pick(
            os.environ.get("UI_THEME"), cfg.get("ui_theme"), default="system")),
        "ui_default_view": str(_setup_pick(
            os.environ.get("UI_DEFAULT_VIEW"), cfg.get("ui_default_view"),
            default="chat")),
        "github_login": env_bool("GITHUB_LOGIN", False),
        "launch": str(_setup_pick(getattr(args, "launch", "gui"), default="gui")),
        "skip_check": bool(getattr(args, "skip_check", False)),
    }


def _form_first(form, name, default=""):
    values = form.get(name) or [default]
    return str(values[0]).strip()


def _form_bool(form, name):
    return _form_first(form, name).lower() in ("1", "true", "yes", "on")


def _choice(value, choices, label):
    if value not in choices:
        raise ValueError(f"Invalid {label}: {value}")
    return value


def apply_browser_setup_form(args, form):
    """Validate browser setup choices, apply them to args, and return env."""
    action = _choice(_form_first(form, "action", "install"),
                       ("install", "run-only", "cancel"), "setup action")
    dependency_mode = _choice(
        _form_first(form, "dependency_mode", "full"),
        ("full", "with-optional", "required", "none"), "dependency mode")
    model_source = _choice(
        _form_first(form, "model_source", "auto"),
        ("auto", "gguf", "server", "custom"), "model source")
    backend = _choice(_form_first(form, "backend", "auto"),
                      ("auto", "ollama", "openai"), "backend")
    performance = _choice(
        _form_first(form, "performance_profile", "balanced"),
        ("fast", "balanced", "quality", "custom"), "performance profile")
    launch = _choice(_form_first(form, "launch", "gui"), LAUNCH_CHOICES, "launch")
    theme = _choice(_form_first(form, "ui_theme", "system"),
                    ("system", "dark", "light"), "theme")
    default_view = _choice(
        _form_first(form, "ui_default_view", "chat"),
        ("chat", "workspace", "apis", "settings"), "opening page")
    try:
        web_port = int(_form_first(form, "web_port", "8765"))
    except ValueError as error:
        raise ValueError("The front-end port must be a number.") from error
    if not 1 <= web_port <= 65535:
        raise ValueError("The front-end port must be between 1 and 65535.")

    gguf = _form_first(form, "gguf")
    model = _form_first(form, "model")
    host = _form_first(form, "host")
    model_file = _form_first(form, "model_file")
    if action == "install" and model_source == "gguf":
        if not gguf:
            raise ValueError("Choose a GGUF file to import.")
        if not expand_path(gguf).is_file():
            raise ValueError(f"GGUF file not found: {gguf}")
        backend = "ollama"
        model_file = gguf
    elif action == "install" and model_source == "server":
        backend = "openai"
        host = host or "http://127.0.0.1:1234"
        model = model or "smollm2-135m-instruct-q4_1"
    elif action == "install" and model_source == "custom":
        backend = "ollama"
        if not model:
            raise ValueError("Enter the Ollama model name to use.")

    args.browser_setup = True
    args.terminal_setup = False
    args.dependency_mode = dependency_mode
    args.gguf = gguf if model_source == "gguf" else ""
    args.model = model
    args.backend = backend
    args.host = host
    args.models_dir = _form_first(form, "models_dir")
    args.model_file = model_file
    args.no_model_scan = not _form_bool(form, "model_discovery")
    args.workspace = _form_first(form, "workspace")
    args.web_host = _form_first(form, "web_host", "127.0.0.1")
    args.web_port = web_port
    args.open_browser = _form_bool(form, "open_browser")
    args.launch = launch
    args.skip_check = _form_bool(form, "skip_check")

    env = {
        "MODEL_SOURCE": model_source,
        "PERFORMANCE_PROFILE": performance,
        "AUTO_APPROVE": "1" if _form_bool(form, "auto_approve") else "0",
        "FRONTEND_ENABLED": "1" if _form_bool(form, "frontend_enabled") else "0",
        "OPEN_BROWSER": "1" if args.open_browser else "0",
        "MODEL_DISCOVERY": "0" if args.no_model_scan else "1",
        "UI_THEME": theme,
        "UI_DEFAULT_VIEW": default_view,
        "GITHUB_LOGIN": "1" if _form_bool(form, "github_login") else "0",
    }
    return {"action": action, "env": env}


def _option_tags(values, current):
    tags = []
    for value, label in values:
        selected = " selected" if str(value) == str(current) else ""
        tags.append(
            f'<option value="{html.escape(str(value), quote=True)}"{selected}>'
            f'{html.escape(str(label))}</option>')
    return "\n".join(tags)


def render_browser_setup_page(token, defaults, records=None, selected=None,
                              sprite_svg=""):
    """Render the one-page browser setup form. Artwork is inline SVG only."""
    records = records or []
    runnable = [r for r in records if r.get("supported")]
    model_options = [("", "Automatic (bundled SmolLM2 first)")]
    for record in runnable:
        model_options.append((
            str(record.get("path") or record.get("name") or ""),
            f"{record.get('name', 'Model')} ({record.get('size_human', '')})",
        ))
    selected_name = selected.get("name") if selected else "the bundled SmolLM2 model"
    checked = lambda name: " checked" if defaults.get(name) else ""
    esc = lambda value: html.escape(str(value), quote=True)
    page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up Milelik</title>
<link rel="icon" type="image/svg+xml" href="/svg-images/milelik-icon.svg">
<link rel="stylesheet" href="/styles.css">
<style>
.setup-shell { width: min(1180px, calc(100vw - 28px)); margin: 0 auto; padding: 26px 0 44px; }
.setup-grid { display: grid; grid-template-columns: 330px minmax(0, 1fr); gap: 20px; align-items: start; }
.setup-intro { position: sticky; top: 18px; }
.setup-form { display: grid; gap: 16px; }
.setup-card { border: 1px solid var(--border); background: var(--panel); padding: 20px; }
.setup-card legend { font: 700 1rem ui-monospace, monospace; padding: 0 7px; }
.setup-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px 16px; }
.setup-wide { grid-column: 1 / -1; }
.check-row { display: flex !important; gap: 10px; align-items: flex-start; font-weight: 600; }
.check-row input { width: auto; margin-top: 3px; }
.setup-actions { display: flex; flex-wrap: wrap; gap: 10px; }
.setup-note { color: var(--muted); line-height: 1.55; }
@media (max-width: 860px) { .setup-grid, .setup-fields { grid-template-columns: 1fr; } .setup-intro { position: static; } }
</style>
</head>
<body>
<main class="setup-shell">
  <p class="eyebrow">Local companion // browser setup // SVG sprite</p>
  <h1>Set up Milelik</h1>
  <div class="setup-grid">
    <aside class="setup-intro">
      <section class="companion-card" aria-label="Milelik companion">
        <div class="companion-topline"><span>MILELIK.SPRITE</span><span id="spriteStateLabel">SETUP</span></div>
        <div id="spriteStage" class="sprite-stage" data-state="idle">__SPRITE__</div>
        <div class="sprite-bubble"><span class="sprite-name">MILELIK</span>
          <p id="spriteSpeech">Choose your setup here. I found __SELECTED__.</p>
        </div>
      </section>
      <p class="setup-note">This page is served only on this computer. Installation still uses Milelik's bundled installer; linux may ask for your password in the terminal that launched setup when system packages are installed.</p>
      <p class="setup-note">No raster sprite images are used: Milelik's companion and icon are SVG files from the organized svg-images folder.</p>
    </aside>
    <form class="setup-form" method="post" action="/setup">
      <input type="hidden" name="token" value="__TOKEN__">
      <fieldset class="setup-card">
        <legend>Model</legend>
        <div class="setup-fields">
          <label>Model source
            <select id="modelSource" name="model_source">__MODEL_SOURCE_OPTIONS__</select>
          </label>
          <label>Backend
            <select name="backend">__BACKEND_OPTIONS__</select>
          </label>
          <label class="setup-wide" data-model-source="gguf">GGUF file to import
            <input name="gguf" value="__GGUF__" autocomplete="off">
          </label>
          <label>Model name or server ID
            <input name="model" value="__MODEL__" placeholder="milelik-smollm2:135m" autocomplete="off">
          </label>
          <label data-model-source="server">LM Studio / llama.cpp URL
            <input name="host" value="__HOST__" autocomplete="off">
          </label>
          <label>Models folder (blank = automatic)
            <input name="models_dir" value="__MODELS_DIR__" autocomplete="off">
          </label>
          <label>Preferred model file
            <select name="model_file">__MODEL_FILE_OPTIONS__</select>
          </label>
          <label class="check-row"><input type="checkbox" name="model_discovery" value="1"__MODEL_DISCOVERY_CHECKED__> Read the models/ folder at startup</label>
        </div>
      </fieldset>
      <fieldset class="setup-card">
        <legend>Installation and safety</legend>
        <div class="setup-fields">
          <label>Dependencies
            <select name="dependency_mode">__DEPENDENCY_OPTIONS__</select>
          </label>
          <label>Performance
            <select name="performance_profile">__PERFORMANCE_OPTIONS__</select>
          </label>
          <label class="check-row"><input type="checkbox" name="auto_approve" value="1"__AUTO_APPROVE_CHECKED__> Auto-approve ordinary actions (dangerous actions still ask)</label>
          <label class="check-row"><input type="checkbox" name="skip_check" value="1"__SKIP_CHECK_CHECKED__> Skip the installer's final check</label>
          <label class="check-row"><input type="checkbox" name="github_login" value="1"__GITHUB_LOGIN_CHECKED__> Run GitHub login after installation</label>
        </div>
      </fieldset>
      <fieldset class="setup-card">
        <legend>Browser front end</legend>
        <div class="setup-fields">
          <label class="check-row"><input type="checkbox" name="frontend_enabled" value="1"__FRONTEND_CHECKED__> Install the browser front end, workspace and APIs</label>
          <label class="check-row"><input type="checkbox" name="open_browser" value="1"__OPEN_BROWSER_CHECKED__> Open the browser when Milelik starts</label>
          <label class="setup-wide">Workspace folder
            <input name="workspace" value="__WORKSPACE__" autocomplete="off">
          </label>
          <label>Front-end address
            <input name="web_host" value="__WEB_HOST__" autocomplete="off">
          </label>
          <label>Front-end port
            <input name="web_port" type="number" min="1" max="65535" value="__WEB_PORT__">
          </label>
          <label>Theme
            <select name="ui_theme">__THEME_OPTIONS__</select>
          </label>
          <label>Opening page
            <select name="ui_default_view">__VIEW_OPTIONS__</select>
          </label>
          <label>After setup
            <select name="launch">__LAUNCH_OPTIONS__</select>
          </label>
        </div>
      </fieldset>
      <div class="setup-actions">
        <button class="button primary" type="submit" name="action" value="install">Install Milelik</button>
        <button class="button secondary" type="submit" name="action" value="run-only" formnovalidate>Run without installing</button>
        <button class="button ghost" type="submit" name="action" value="cancel" formnovalidate>Cancel</button>
      </div>
    </form>
  </div>
</main>
<script>
const source = document.querySelector('#modelSource');
function syncModelFields() {
  document.querySelectorAll('[data-model-source]').forEach((field) => {
    field.hidden = field.dataset.modelSource !== source.value;
  });
}
source.addEventListener('change', syncModelFields);
syncModelFields();
</script>
</body>
</html>
"""
    replacements = {
        "__SPRITE__": sprite_svg,
        "__SELECTED__": html.escape(str(selected_name)),
        "__TOKEN__": esc(token),
        "__MODEL_SOURCE_OPTIONS__": _option_tags([
            ("auto", "Bundled SmolLM2 (recommended)"),
            ("gguf", "Import a GGUF file I choose"),
            ("server", "Use LM Studio / llama.cpp"),
            ("custom", "Download another Ollama model"),
        ], defaults.get("model_source", "auto")),
        "__BACKEND_OPTIONS__": _option_tags([
            ("auto", "Automatic"), ("ollama", "Ollama"),
            ("openai", "LM Studio / llama.cpp"),
        ], defaults.get("backend", "auto")),
        "__GGUF__": esc(defaults.get("gguf", "")),
        "__MODEL__": esc(defaults.get("model", "")),
        "__HOST__": esc(defaults.get("host", "")),
        "__MODELS_DIR__": esc(defaults.get("models_dir", "")),
        "__MODEL_FILE_OPTIONS__": _option_tags(
            model_options, defaults.get("model_file", "")),
        "__MODEL_DISCOVERY_CHECKED__": checked("model_discovery"),
        "__DEPENDENCY_OPTIONS__": _option_tags([
            ("full", "Full (recommended)"), ("with-optional", "Full + optional"),
            ("required", "Required only"), ("none", "Do not install packages"),
        ], defaults.get("dependency_mode", "full")),
        "__PERFORMANCE_OPTIONS__": _option_tags([
            ("balanced", "Balanced"), ("fast", "Fast"),
            ("quality", "Quality"), ("custom", "Custom/current values"),
        ], defaults.get("performance_profile", "balanced")),
        "__AUTO_APPROVE_CHECKED__": checked("auto_approve"),
        "__SKIP_CHECK_CHECKED__": checked("skip_check"),
        "__GITHUB_LOGIN_CHECKED__": checked("github_login"),
        "__FRONTEND_CHECKED__": checked("frontend_enabled"),
        "__OPEN_BROWSER_CHECKED__": checked("open_browser"),
        "__WORKSPACE__": esc(defaults.get("workspace", "")),
        "__WEB_HOST__": esc(defaults.get("web_host", "127.0.0.1")),
        "__WEB_PORT__": esc(defaults.get("web_port", 8765)),
        "__THEME_OPTIONS__": _option_tags([
            ("system", "Match system"), ("dark", "Dark"), ("light", "Light"),
        ], defaults.get("ui_theme", "system")),
        "__VIEW_OPTIONS__": _option_tags([
            ("chat", "Chat"), ("workspace", "Workspace"),
            ("apis", "APIs"), ("settings", "Preferences"),
        ], defaults.get("ui_default_view", "chat")),
        "__LAUNCH_OPTIONS__": _option_tags([
            ("gui", "Open Milelik in the browser"),
            ("cli", "Open terminal chat"), ("none", "Finish without opening"),
        ], defaults.get("launch", "gui")),
    }
    for marker, value in replacements.items():
        page = page.replace(marker, str(value))
    return page


def render_browser_setup_progress(token, sprite_svg=""):
    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Milelik setup</title>
<link rel="icon" type="image/svg+xml" href="/svg-images/milelik-icon.svg">
<link rel="stylesheet" href="/styles.css">
<style>.setup-shell{width:min(720px,calc(100vw - 28px));margin:0 auto;padding:28px 0}.setup-card{border:1px solid var(--border);background:var(--panel);padding:20px;margin-top:16px}</style>
</head><body><main class="setup-shell">
<p class="eyebrow">Milelik browser setup</p><h1 id="setupTitle">Setting up Milelik</h1>
<section class="companion-card"><div class="companion-topline"><span>MILELIK.SPRITE</span><span id="spriteStateLabel">WORKING</span></div>
<div id="spriteStage" class="sprite-stage" data-state="thinking">__SPRITE__</div>
<div class="sprite-bubble"><span class="sprite-name">MILELIK</span><p id="spriteSpeech">Working…</p></div></section>
<section class="setup-card"><p id="setupMessage">Your choices were received.</p>
<p>Keep this tab open. If linux asks for your password, enter it in the terminal that launched setup. Milelik will open in your browser when setup finishes if you selected that option.</p></section>
</main><script>
const token = "__TOKEN__";
async function poll() {
  try {
    const data = await (await fetch('/status?token=' + encodeURIComponent(token))).json();
    document.querySelector('#setupMessage').textContent = data.message || data.phase;
    document.querySelector('#spriteSpeech').textContent = data.message || data.phase;
    const stage = document.querySelector('#spriteStage');
    const label = document.querySelector('#spriteStateLabel');
    if (data.phase === 'complete') { stage.dataset.state='talking'; label.textContent='READY'; document.querySelector('#setupTitle').textContent='Milelik is ready'; return; }
    if (data.phase === 'failed') { stage.dataset.state='glitch'; label.textContent='ERROR'; return; }
    if (data.phase === 'cancelled') { stage.dataset.state='sleep'; label.textContent='CANCELLED'; return; }
  } catch (error) { return; }
  setTimeout(poll, 800);
}
poll();
</script></body></html>"""
    return page.replace("__SPRITE__", sprite_svg).replace(
        "__TOKEN__", html.escape(str(token), quote=True))


class BrowserSetupSession:
    """Small local one-shot browser setup session."""

    def __init__(self, args, token, page_html, progress_html, icon_svg=""):
        self.args = args
        self.token = token
        self.page_html = page_html
        self.progress_html = progress_html
        self.icon_svg = icon_svg
        self.server = None
        self.thread = None
        self.url = ""
        self.result = None
        self.phase = "waiting"
        self.message = "Waiting for your setup choices."
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    def status(self):
        with self._lock:
            return {"phase": self.phase, "message": self.message,
                    "action": (self.result or {}).get("action", "")}

    def set_phase(self, phase, message):
        with self._lock:
            self.phase = phase
            self.message = message

    def complete(self, phase, message, delay=1.0):
        self.set_phase(phase, message)
        if delay:
            time.sleep(delay)
        self.close()

    def wait(self):
        self.event.wait()
        return self.result or {"action": "cancel", "env": {}}

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)


def start_browser_setup(args, core=None, root: Path = ROOT, browser_open=None,
                        host="127.0.0.1", port=0):
    """Start the browser setup page and open it in the user's browser."""
    _directories, records, selected, cfg = model_summary(
        core, getattr(args, "models_dir", ""), getattr(args, "model_file", ""))
    defaults = browser_setup_defaults(args, cfg=cfg, selected=selected,
                                       root=root, core=core)
    sprite_svg = load_sprite_svg(root)
    icon_svg = load_app_icon_svg(root)
    token = secrets.token_urlsafe(24)
    session = BrowserSetupSession(
        args, token,
        render_browser_setup_page(token, defaults, records, selected, sprite_svg),
        render_browser_setup_progress(token, sprite_svg),
        icon_svg=icon_svg,
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, body, content_type="text/html; charset=utf-8",
                  status=200, cookie=False):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header(
                    "Set-Cookie",
                    f"milelik_setup={token}; Path=/; HttpOnly; SameSite=Strict")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, query=None, form=None):
            candidates = []
            if query:
                candidates.extend(query.get("token") or [])
            if form:
                candidates.extend(form.get("token") or [])
            for part in str(self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "milelik_setup":
                    candidates.append(value)
            return any(secrets.compare_digest(str(candidate), token)
                       for candidate in candidates)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = urllib.parse.parse_qs(parsed.query)
            if path in ("/svg-images/milelik-icon.svg",
                        "/sprites/milelik-icon.svg", "/favicon.svg",
                        "/favicon.ico"):
                if session.icon_svg:
                    self._send(session.icon_svg,
                               "image/svg+xml; charset=utf-8")
                else:
                    self._send("Not found.", "text/plain", 404)
                return
            if not self._authorized(query):
                self._send("Milelik setup token required.", "text/plain", 403)
                return
            if path == "/":
                cookie = bool(query.get("token"))
                if session.result:
                    self._send(session.progress_html, cookie=cookie)
                else:
                    self._send(session.page_html, cookie=cookie)
            elif path == "/styles.css":
                try:
                    css = (root / "frontend" / "styles.css").read_bytes()
                except OSError:
                    css = b"body{font-family:monospace;background:#08090b;color:#f7f7f2}"
                self._send(css, "text/css; charset=utf-8")
            elif path == "/status":
                self._send(json.dumps(session.status()), "application/json")
            else:
                self._send("Not found.", "text/plain", 404)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.rstrip("/") != "/setup":
                self._send("Not found.", "text/plain", 404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length < 0 or length > 65536:
                self._send("Setup form is too large.", "text/plain", 400)
                return
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", "replace"),
                keep_blank_values=True)
            if not self._authorized(form=form):
                self._send("Milelik setup token required.", "text/plain", 403)
                return
            if session.result:
                self._send(session.progress_html)
                return
            try:
                result = apply_browser_setup_form(args, form)
            except ValueError as error:
                body = (f"<h1>Check your setup choices</h1><p>{html.escape(str(error))}</p>"
                        f'<p><a href="/?token={html.escape(token, quote=True)}">Back to setup</a></p>')
                self._send(body, status=400, cookie=True)
                return
            session.result = result
            if result["action"] == "install":
                session.set_phase("installing", "Installing Milelik with your browser choices…")
            elif result["action"] == "run-only":
                session.set_phase("installing", "Opening Milelik without installing…")
            else:
                session.set_phase("cancelled", "Setup cancelled. Nothing was installed or started.")
            session.event.set()
            self._send(session.progress_html, cookie=True)

        def log_message(self, *_args):
            pass

    session.server = http.server.ThreadingHTTPServer((host, port), Handler)
    session.server.daemon_threads = True
    actual_port = int(session.server.server_address[1])
    session.url = f"http://{host}:{actual_port}/?token={token}"
    session.thread = threading.Thread(target=session.server.serve_forever,
                                      daemon=True)
    session.thread.start()
    print("\nOpening Milelik setup in your browser…")
    print(f"  {session.url}")
    opener = webbrowser.open if browser_open is None else browser_open
    try:
        opened = bool(opener(session.url))
    except Exception:
        opened = False
    if not opened:
        print("Your browser did not open automatically; open the address above.")
    return session


def build_install_command(args, root: Path = ROOT, interactive_terminal=False):
    """Build the bundled installer command controlled by this script."""
    command = [sys.executable, str(root / "install_milelik.py")]
    if args.yes or getattr(args, "browser_setup", False) or not interactive_terminal:
        command.append("--yes")
    else:
        command.append("--wizard")

    # This Python script controls the final launch so the front end runs in
    # the foreground and Ctrl+C behaves normally.
    command.extend(["--launch", "none"])

    if args.skip_check:
        command.append("--skip-check")
    if args.dependency_mode:
        command.extend(["--dependency-mode", args.dependency_mode])
    if args.model:
        command.extend(["--model", args.model])
    if args.gguf:
        command.extend(["--gguf", args.gguf])
    if args.models_dir:
        command.extend(["--models-dir", args.models_dir])
    if args.model_file:
        command.extend(["--model-file", args.model_file])
    if args.no_model_scan:
        command.append("--no-model-scan")
    if args.backend:
        command.extend(["--backend", args.backend])
    if args.host:
        command.extend(["--host", args.host])
    if args.workspace:
        command.extend(["--workspace", args.workspace])
    if args.web_host:
        command.extend(["--web-host", args.web_host])
    if args.web_port is not None:
        command.extend(["--web-port", str(args.web_port)])
    return command


def build_launch_command(args, root: Path = ROOT, installed_command=None,
                         python_executable=None):
    """Build the Milelik command used after setup (or instead of setup)."""
    if args.launch == "none" and not args.check and not args.list_models and not args.task:
        return []

    if not args.source and installed_command:
        if isinstance(installed_command, (list, tuple)):
            command = [str(part) for part in installed_command]
        else:
            command = [str(installed_command)]
    else:
        command = [python_executable or sys.executable, str(root / "milelik.py")]

    if args.model:
        command.extend(["--model", args.model])
    if args.backend:
        command.extend(["--backend", args.backend])
    if args.host:
        command.extend(["--host", args.host])
    if args.models_dir:
        command.extend(["--models-dir", args.models_dir])
    if args.model_file:
        command.extend(["--model-file", args.model_file])
    if args.choose_model:
        command.append("--choose-model")
    if args.no_model_scan:
        command.append("--no-model-scan")

    if args.list_models:
        command.append("--list-models")
        return command
    if args.check:
        command.append("--check")
        return command
    if args.task:
        command.append(args.task)
        return command
    if args.launch == "cli":
        command.append("--interactive")
        return command
    if args.launch == "gui":
        command.append("--gui")
        if args.workspace:
            command.extend(["--workspace-dir", args.workspace])
        if args.web_host:
            command.extend(["--web-host", args.web_host])
        if args.web_port is not None:
            command.extend(["--web-port", str(args.web_port)])
        if args.open_browser is True:
            command.append("--open-browser")
        elif args.open_browser is False:
            command.append("--no-open-browser")
    return command


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="setup_milelik.py",
        description="Set up Milelik and run it from this repository.",
        epilog=(
            "Examples: python3 setup_milelik.py | "
            "python3 setup_milelik.py --browser-setup | "
            "python3 setup_milelik.py --terminal-setup | "
            "python3 setup_milelik.py --yes | "
            "python3 setup_milelik.py --run-only --cli | "
            "python3 setup_milelik.py --list-models"
        ),
    )
    parser.add_argument("--yes", "-y", action="store_true",
                        help="use installer defaults instead of browser/terminal setup questions")
    parser.add_argument("--browser-setup", action="store_true",
                        help="open Milelik's setup choices in your web browser (desktop default)")
    parser.add_argument("--terminal-setup", "--wizard", dest="terminal_setup",
                        action="store_true",
                        help="use the old terminal setup questions instead of the browser")
    parser.add_argument("--allow-root", action="store_true",
                        help="allow running as root (not recommended for a desktop install)")
    parser.add_argument("--install", action="store_true",
                        help="run the installer even with --check or --list-models")
    parser.add_argument("--install-only", action="store_true",
                        help="run setup/installation and do not start Milelik")
    parser.add_argument("--run-only", "--no-install", dest="no_install", action="store_true",
                        help="skip installation and run Milelik from this folder")
    parser.add_argument("--source", action="store_true",
                        help="run this folder's milelik.py even if Milelik is installed")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the setup/launch commands without running them")
    parser.add_argument("--skip-check", action="store_true",
                        help="tell the installer not to run its final milelik --check")
    parser.add_argument("--check", action="store_true",
                        help="run milelik --check and exit (does not install unless --install is also used)")
    parser.add_argument("--list-models", action="store_true",
                        help="list model files in models/ and exit (does not install unless --install is also used)")
    parser.add_argument("--task", default=None,
                        help="run one Milelik task after setup instead of opening the GUI/chat")

    parser.add_argument("--launch", choices=LAUNCH_CHOICES, default="gui",
                        help="what to start after setup: gui (default), cli, or none")
    parser.add_argument("--gui", dest="launch", action="store_const", const="gui",
                        help="start the browser front end after setup (default)")
    parser.add_argument("--cli", "--interactive", dest="launch", action="store_const", const="cli",
                        help="start terminal chat after setup")
    parser.add_argument("--no-launch", dest="launch", action="store_const", const="none",
                        help="do not start Milelik after setup")

    parser.add_argument("--models-dir", default=os.environ.get("MILELIK_MODELS_DIR", ""),
                        help="folder containing AI model files for Milelik to read")
    parser.add_argument("--model-file", default=os.environ.get("MILELIK_MODEL_FILE", ""),
                        help="preferred model file/path/name/id inside the models folder")
    parser.add_argument("--model", default="",
                        help="Ollama or LM Studio/llama.cpp model name/id")
    parser.add_argument("--gguf", default="",
                        help="existing GGUF file for the installer to import into Ollama")
    parser.add_argument("--backend", choices=("auto", "ollama", "openai"), default="",
                        help="model backend to save/use")
    parser.add_argument("--host", default="",
                        help="model server URL, for example http://127.0.0.1:1234")
    parser.add_argument("--choose-model", action="store_true",
                        help="show Milelik's model-file chooser when it starts")
    parser.add_argument("--no-model-scan", action="store_true",
                        help="do not read the models/ folder at startup")

    parser.add_argument("--dependency-mode", choices=("full", "with-optional", "required", "none"),
                        default="", help="dependency set for the installer")
    parser.add_argument("--workspace", "--workspace-dir", dest="workspace", default="",
                        help="Milelik web workspace folder")
    parser.add_argument("--web-host", default="",
                        help="front-end bind address (default from Milelik config)")
    parser.add_argument("--web-port", type=int, default=None,
                        help="front-end port (default from Milelik config)")
    parser.add_argument("--open-browser", dest="open_browser", action="store_true", default=None,
                        help="open the browser when the front end starts")
    parser.add_argument("--no-open-browser", dest="open_browser", action="store_false", default=None,
                        help="do not open the browser when the front end starts")

    args = parser.parse_args(argv)
    if args.web_port is not None and not 1 <= args.web_port <= 65535:
        parser.error("--web-port must be between 1 and 65535")
    if args.task is not None and not args.task.strip():
        parser.error("--task cannot be empty")
    if args.no_install and (args.install or args.install_only):
        parser.error("--run-only cannot be combined with --install or --install-only")
    if args.browser_setup and args.terminal_setup:
        parser.error("--browser-setup cannot be combined with --terminal-setup")
    if args.browser_setup and args.yes:
        parser.error("--browser-setup cannot be combined with --yes")
    if args.browser_setup and args.no_install:
        parser.error("--browser-setup cannot be combined with --run-only; choose Run without installing on the setup page")
    if args.gguf and not args.model_file:
        args.model_file = args.gguf
    if args.no_install:
        # "Run only" means run the checkout beside this script, matching the
        # model summary printed above, rather than an older installed copy.
        args.source = True
    if args.install_only:
        args.install = True
        args.launch = "none"
        args.check = False
        args.list_models = False
        args.task = None
    return args


def current_euid():
    return os.geteuid() if hasattr(os, "geteuid") else None


def main(argv=None, runner=subprocess.run, input_fn=input, stdin_isatty=None,
         stdout_isatty=None, root: Path = ROOT, installed_command="auto",
         euid=None, browser_open=None, browser_setup_starter=None,
         environ=None) -> int:
    args = parse_args(argv)

    effective_euid = current_euid() if euid is None else euid
    if effective_euid == 0 and not args.allow_root and not args.dry_run:
        print("Error: do not run setup_milelik.py as root/sudo.", file=sys.stderr)
        print("Run it as your normal desktop user:", file=sys.stderr)
        print("  python3 setup_milelik.py", file=sys.stderr)
        print("If ./setup_milelik.py says Permission denied, the python3 form "
              "above bypasses the script's executable bit. sudo does not "
              "bypass a noexec mount, and the installer already requests "
              "sudo only for system packages/services.", file=sys.stderr)
        print("Use --allow-root only in a root-only test environment.",
              file=sys.stderr)
        return 2

    if sys.version_info < MIN_PYTHON:
        print(
            f"Error: Milelik needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; "
            f"this is Python {sys.version.split()[0]}.",
            file=sys.stderr,
        )
        return 2

    core_path = root / "milelik.py"
    if not core_path.is_file():
        print(f"Error: milelik.py was not found beside this setup script: {root}", file=sys.stderr)
        print("Run this script from the Milelik repository, or use the installed milelik command.",
              file=sys.stderr)
        return 2

    try:
        ensure_models_folder(args.models_dir, root=root)
    except OSError as error:
        print(f"Error: could not create the models folder: {error}", file=sys.stderr)
        return 1

    print("Milelik Python setup")
    print(f"Repository: {root}")
    print(f"Python: {sys.version.split()[0]}")

    core = load_core(root)
    if core is None:
        print("Warning: could not load Milelik's model helpers; startup will still scan models/.")
    elif args.no_model_scan and not args.list_models:
        print("Model folder check: skipped because --no-model-scan was used.")
    else:
        print_model_summary(core, args.models_dir, args.model_file)

    operation_only = bool(args.list_models or args.check)
    should_install = not args.no_install and (args.install or not operation_only)
    if operation_only and not should_install:
        args.source = True

    if stdin_isatty is None:
        stdin_isatty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if stdout_isatty is None:
        stdout_isatty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    interactive_terminal = bool(stdin_isatty and stdout_isatty)

    if installed_command == "auto":
        installed = resolve_installed_command()
        installed_prefix = installed_launch_prefix(installed)
    else:
        installed = Path(installed_command) if installed_command else None
        installed_prefix = [str(installed)] if installed else []

    browser_session = None
    browser_env = {}
    if args.browser_setup and not should_install:
        print("Error: --browser-setup needs a setup/install run; do not combine "
              "it with --run-only, --check or --list-models unless --install "
              "is also used.", file=sys.stderr)
        return 2

    auto_browser_setup = bool(
        should_install and not args.terminal_setup and not args.yes
        and display_available(environ))
    use_browser_setup = bool(
        should_install and not args.dry_run
        and (args.browser_setup or auto_browser_setup))
    if args.browser_setup and args.dry_run:
        print("Dry run: would open Milelik browser setup on this computer.")

    if use_browser_setup:
        starter = browser_setup_starter or start_browser_setup
        try:
            browser_session = starter(
                args, core=core, root=root, browser_open=browser_open)
        except OSError as error:
            if args.browser_setup:
                print(f"Error: could not start the browser setup page: {error}",
                      file=sys.stderr)
                return 1
            print(f"Warning: browser setup could not start ({error}); "
                  "using terminal setup instead.")
            browser_session = None
            use_browser_setup = False
        if browser_session is not None:
            args.browser_setup = True
            try:
                browser_result = browser_session.wait()
            except KeyboardInterrupt:
                browser_session.complete(
                    "cancelled", "Browser setup was interrupted.", delay=0)
                return 130
            browser_env = dict(browser_result.get("env") or {})
            if browser_env.get("FRONTEND_ENABLED") == "0" and args.launch == "gui":
                args.launch = "cli"
            action = browser_result.get("action")
            if action == "cancel":
                print("Browser setup cancelled. Nothing was installed or started.")
                browser_session.complete(
                    "cancelled", "Setup cancelled. Nothing was installed or started.")
                return 0
            if action == "run-only":
                should_install = False
                args.source = True
                browser_session.set_phase(
                    "installing", "Opening Milelik from this folder…")

    if should_install and interactive_terminal and not args.yes \
            and not use_browser_setup and not args.browser_setup:
        print()
        print("This setup script will run Milelik's guided installer, then start Milelik.")
        if not ask_yes_no("Run the guided Milelik setup now?", True, input_fn):
            if ask_yes_no("Run Milelik from this folder without installing?", True, input_fn):
                should_install = False
                args.source = True
            else:
                print("Setup cancelled. Nothing was installed or started.")
                return 0

    if should_install:
        installer = root / "install_milelik.py"
        if installer.is_file():
            if not interactive_terminal and not args.yes:
                print("No interactive terminal detected; using installer defaults. Use --yes to make this explicit.")
            install_command = build_install_command(args, root=root,
                                                    interactive_terminal=interactive_terminal)
            install_env = os.environ.copy()
            install_env.update(browser_env)
            install_env["MILELIK_WIZARD_CANCEL_EXIT"] = str(INSTALLER_CANCELLED)
            if browser_session is not None:
                install_env["MILELIK_BROWSER_SETUP_ACTIVE"] = "1"
                browser_session.set_phase(
                    "installing", "Installing Milelik with your browser choices…")
            if args.allow_root:
                install_env["MILELIK_ALLOW_ROOT"] = "1"
            code = run_command(install_command, dry_run=args.dry_run, runner=runner,
                               cwd=os.getcwd(), env=install_env)
            if code == INSTALLER_CANCELLED:
                print("Setup cancelled; Milelik was not started.")
                if browser_session is not None:
                    browser_session.complete(
                        "cancelled", "Setup cancelled; Milelik was not started.")
                return 0
            if code:
                if browser_session is not None:
                    browser_session.complete(
                        "failed", f"Milelik setup stopped with exit code {code}.")
                return code
            if installed_command == "auto":
                installed = resolve_installed_command()
                installed_prefix = installed_launch_prefix(installed)
        elif installed_prefix:
            print("Milelik appears to already be installed; skipping the repository installer.")
        else:
            print(f"Error: bundled installer not found: {installer}", file=sys.stderr)
            print("Run this setup script from a full Milelik repository checkout.", file=sys.stderr)
            if browser_session is not None:
                browser_session.complete(
                    "failed", "Milelik's bundled installer was not found.")
            return 2

    # If the installer saved a preference disabling the front end, respect it
    # instead of trying to start a web module that was not installed.
    if should_install and args.launch == "gui" and core is not None:
        try:
            saved = core.load_config()
        except Exception:
            saved = {}
        if saved.get("frontend_enabled") is False:
            print("The front end is disabled in Milelik preferences; starting terminal chat instead.")
            args.launch = "cli"

    launch_command = build_launch_command(args, root=root, installed_command=installed_prefix)
    if browser_session is not None:
        if launch_command and args.launch == "gui" and not args.task:
            completion_message = "Setup finished. Milelik is opening in your browser."
        elif launch_command:
            completion_message = "Setup finished. Milelik is starting now."
        else:
            completion_message = "Setup finished. You can close this setup tab."
        browser_session.complete("complete", completion_message)
        browser_session = None

    if not launch_command:
        print()
        print("Setup finished. Start Milelik with one of:")
        print("  milelik --gui")
        print("  milelik --interactive")
        print("  python3 setup_milelik.py --run-only")
        return 0

    if args.list_models:
        print("\nListing model files...")
    elif args.check:
        print("\nRunning Milelik check...")
    elif args.launch == "cli" and not args.task:
        print("\nStarting Milelik terminal chat. Press Ctrl+C or type /exit to stop.")
    elif args.task:
        print("\nRunning your Milelik task...")
    else:
        print("\nStarting the Milelik front end. Press Ctrl+C in this terminal to stop it.")

    return run_command(launch_command, dry_run=args.dry_run, runner=runner,
                       cwd=os.getcwd())


if __name__ == "__main__":
    raise SystemExit(main())
