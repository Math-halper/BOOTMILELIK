#!/usr/bin/env python3
"""Set up Milelik's bundled SmolLM2-135M-Instruct-Q4_1 default model.

Examples:
    python3 setup_model.py
    python3 setup_model.py /path/to/model.gguf
    python3 setup_model.py --download
    python3 setup_model.py --validate /path/to/model.gguf

With a GGUF (given, or the bundled file found in Milelik's models/ folders),
this imports the file into Ollama with an appropriate template. The bundled
SmolLM2 file becomes "milelik-smollm2:135m"; a Qwen2.5-Coder GGUF still
becomes "milelik-coder:7b" with its ChatML/tools template.

If the GGUF is already loaded in LM Studio, this is not needed: start LM
Studio's local server and run Milelik; it is detected automatically.
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET_FILE = "smollm2-135m-instruct-q4_1.gguf"
OLLAMA_LIBRARY_MODEL = "smollm2:135m"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
MODEL_PATTERN = "*smollm2*135m*q4_1*.gguf"


def expand_path(value) -> Path:
    return Path(os.path.expanduser(str(value)))


def find_bundled_gguf(directory, max_depth: int = 6) -> Path | None:
    """Find the bundled GGUF below a directory, mirroring find -maxdepth."""
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
            if fnmatch.fnmatch(filename.lower(), MODEL_PATTERN.lower()):
                candidate = current_path / filename
                if candidate.is_file():
                    return candidate
    return None


def candidate_directories(root: Path = ROOT, models_dir: str = "",
                          home: Path | None = None,
                          cwd: Path | None = None) -> list[Path]:
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    values = [
        expand_path(models_dir) if models_dir else None,
        root / "models",
        home / ".local" / "share" / "milelik" / "models",
        cwd / "models",
        root,
        cwd,
        home / "Downloads",
        home / "models",
        home / "Models",
        home / ".lmstudio" / "models",
        home / ".cache" / "lm-studio" / "models",
    ]
    return [value for value in values if value is not None]


def find_gguf(root: Path = ROOT, models_dir: str = "",
              home: Path | None = None, cwd: Path | None = None) -> Path | None:
    for directory in candidate_directories(root, models_dir, home, cwd):
        found = find_bundled_gguf(directory, max_depth=6)
        if found is not None:
            return found
    return None


def load_core(root: Path = ROOT):
    core_path = root / "milelik.py"
    if not core_path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("milelik_model_setup_core", core_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def ollama_api_ready(url: str = OLLAMA_TAGS_URL, opener=urllib.request.urlopen) -> bool:
    try:
        with opener(url, timeout=2) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def ensure_ollama_running(ollama: str, *, process_starter=subprocess.Popen,
                          ready=ollama_api_ready, sleep=time.sleep) -> bool:
    if ready():
        return True
    try:
        process_starter(
            [ollama, "serve"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    for _ in range(50):
        if ready():
            return True
        sleep(0.2)
    return ready()


def save_downloaded_model(core, model: str = OLLAMA_LIBRARY_MODEL) -> Path | None:
    if core is not None and hasattr(core, "update_config"):
        path = core.update_config(model=model, backend="auto", model_file="")
        print(f"Default model saved in {path}")
        return Path(path)
    return None


def parse_args(argv=None, environ=None):
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        prog="setup_model.py",
        description="Import Milelik's bundled GGUF into Ollama or download it.")
    parser.add_argument("gguf", nargs="?", default=env.get("GGUF", ""),
                        help="GGUF file (or a folder containing it)")
    parser.add_argument("--download", action="store_true",
                        help="download smollm2:135m from Ollama instead")
    parser.add_argument("--validate", action="store_true",
                        help="only check the GGUF header/metadata, do not import")
    parser.add_argument("--import-name", default=env.get("OLLAMA_NAME", ""),
                        help="Ollama name for an imported GGUF")
    parser.add_argument("--models-dir",
                        default=env.get("MODELS_DIR", env.get("MILELIK_MODELS_DIR", "")),
                        help="extra folder to search first for the bundled GGUF")
    return parser.parse_args(argv)


def main(argv=None, *, runner=subprocess.run, which=shutil.which,
         environ=None, root: Path = ROOT, home: Path | None = None,
         cwd: Path | None = None, core="auto",
         process_starter=subprocess.Popen, ready=ollama_api_ready,
         sleep=time.sleep) -> int:
    args = parse_args(argv, environ=environ)
    if core == "auto":
        core = load_core(root)

    gguf = expand_path(args.gguf) if args.gguf else None
    if gguf is not None and gguf.is_dir():
        gguf = find_bundled_gguf(gguf, max_depth=4) or gguf
    if gguf is None:
        gguf = find_gguf(root=root, models_dir=args.models_dir,
                         home=home, cwd=cwd)

    if args.validate:
        if gguf is None or not gguf.is_file():
            print("No GGUF file to validate. Pass one explicitly:")
            print("  python3 setup_model.py --validate /path/to/model.gguf")
            return 1
        if core is None or not hasattr(core, "validate_gguf"):
            print("Cannot validate without milelik.py beside setup_model.py.")
            return 1
        valid, detail = core.validate_gguf(gguf)
        print(f"{'VALID' if valid else 'INVALID'}: {gguf}")
        print(detail)
        return 0 if valid else 1

    milelik_py = root / "milelik.py"
    if gguf is not None and gguf.is_file():
        print(f"Found: {gguf}")
        ollama = which("ollama")
        if not ollama:
            print("Ollama isn't installed, so the GGUF can't be imported into it.")
            print("Run python3 setup_milelik.py first - or skip Ollama: load the file in")
            print("LM Studio, start its server (port 1234), and just run Milelik.")
            return 1

        import_name = args.import_name
        if not import_name:
            if core is not None and hasattr(core, "ollama_name_for_model_file"):
                import_name = core.ollama_name_for_model_file(str(gguf))
            else:
                import_name = "milelik-smollm2:135m"

        result = runner(
            [sys.executable, str(milelik_py), "--import-gguf", str(gguf),
             "--import-name", str(import_name)], check=False)
        code = int(getattr(result, "returncode", 0) or 0)
        if code:
            return code
        print()
        runner([sys.executable, str(milelik_py), "--check"], check=False)
        return 0

    if args.download:
        ollama = which("ollama")
        if not ollama:
            print("Ollama isn't installed. Run python3 setup_milelik.py first.",
                  file=sys.stderr)
            return 1
        ensure_ollama_running(
            ollama, process_starter=process_starter, ready=ready, sleep=sleep)
        print(f"Downloading {OLLAMA_LIBRARY_MODEL} (~94 MB)…")
        result = runner([ollama, "pull", OLLAMA_LIBRARY_MODEL], check=False)
        code = int(getattr(result, "returncode", 0) or 0)
        if code:
            return code
        save_downloaded_model(core, OLLAMA_LIBRARY_MODEL)
        print()
        runner([sys.executable, str(milelik_py), "--check"], check=False)
        return 0

    print(f"Could not find {TARGET_FILE}")
    print("in Milelik's models/ folders, this folder, ~/Downloads, ~/models, or LM Studio's model folders.")
    print()
    print("Options:")
    print(f"  1. Restore Milelik's bundled file in models/{TARGET_FILE}, or point at a GGUF:")
    print("                           python3 setup_model.py /path/to/model.gguf")
    print("  2. Download it instead:  python3 setup_model.py --download")
    print(f"                           (ollama pull {OLLAMA_LIBRARY_MODEL})")
    print("  3. Already loaded in LM Studio? Just start its server and run: milelik --check")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
