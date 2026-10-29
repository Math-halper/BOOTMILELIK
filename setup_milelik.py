#!/usr/bin/env python3
"""Set up and run Milelik from a source checkout.

Run it with:

    python3 setup_milelik.py

The script uses only the Python standard library. It checks the local
models/ folder, runs Milelik's bundled installer (unless asked not to), and
then starts the browser front end, terminal chat, one task, --check, or the
model list. The heavy system-package and model provisioning work stays in
install.sh, which is the installer already shipped in this repository.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE_PATH = ROOT / "milelik.py"
INSTALLER_PATH = ROOT / "install.sh"
MIN_PYTHON = (3, 10)
LAUNCH_CHOICES = ("gui", "cli", "none")
INSTALLER_CANCELLED = 10


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
              "noexec; run scripts through python3/bash or move Milelik "
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


def build_install_command(args, root: Path = ROOT, interactive_terminal=False):
    """Build the bundled installer command controlled by this script."""
    command = ["bash", str(root / "install.sh")]
    if args.yes or not interactive_terminal:
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
            "python3 setup_milelik.py --yes | "
            "python3 setup_milelik.py --run-only --cli | "
            "python3 setup_milelik.py --list-models"
        ),
    )
    parser.add_argument("--yes", "-y", action="store_true",
                        help="use installer defaults instead of the guided questions")
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
         euid=None) -> int:
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

    if should_install and interactive_terminal and not args.yes:
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
        installer = root / "install.sh"
        if installer.is_file():
            if not interactive_terminal and not args.yes:
                print("No interactive terminal detected; using installer defaults. Use --yes to make this explicit.")
            install_command = build_install_command(args, root=root,
                                                    interactive_terminal=interactive_terminal)
            install_env = os.environ.copy()
            install_env["MILELIK_WIZARD_CANCEL_EXIT"] = str(INSTALLER_CANCELLED)
            if args.allow_root:
                install_env["MILELIK_ALLOW_ROOT"] = "1"
            code = run_command(install_command, dry_run=args.dry_run, runner=runner,
                               cwd=os.getcwd(), env=install_env)
            if code == INSTALLER_CANCELLED:
                print("Setup cancelled; Milelik was not started.")
                return 0
            if code:
                return code
            if installed_command == "auto":
                installed = resolve_installed_command()
                installed_prefix = installed_launch_prefix(installed)
        elif installed_prefix:
            print("Milelik appears to already be installed; skipping the repository installer.")
        else:
            print(f"Error: bundled installer not found: {installer}", file=sys.stderr)
            print("Run this setup script from a full Milelik repository checkout.", file=sys.stderr)
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
