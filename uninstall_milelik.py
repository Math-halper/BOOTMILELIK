#!/usr/bin/env python3
"""Remove Milelik's per-user installation.

Ollama, downloaded/imported models and the user's Milelik workspace are left
alone on purpose. Model files under the installed app directory are also
preserved because they can be many gigabytes.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        shutil.rmtree(path)


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        _remove_file(path)
    elif path.exists():
        shutil.rmtree(path)


def uninstall(home: Path | None = None) -> Path | None:
    """Remove Milelik below *home*; return the preserved models path if any."""
    home = Path(home) if home is not None else Path.home()
    app_dir = home / ".local" / "share" / "milelik"

    for name in ("milelik", "milelik-web", "milelik-app", "milelik-setup",
                 "milelik-install", "milelik-model-setup", "milelik-engine",
                 "milelik-uninstall"):
        _remove_file(home / ".local" / "bin" / name)

    preserved_models = None
    models_dir = app_dir / "models"
    if models_dir.is_dir():
        preserved_models = models_dir
        for child in app_dir.iterdir():
            if child.name != "models":
                _remove_tree(child)
    else:
        _remove_tree(app_dir)

    applications = home / ".local" / "share" / "applications"
    _remove_file(applications / "milelik.desktop")
    _remove_file(applications / "milelik-workspace.desktop")
    _remove_tree(home / ".config" / "milelik")
    return preserved_models


def main(argv=None, *, home: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uninstall_milelik.py",
        description="Remove Milelik while preserving models and workspace files.")
    parser.parse_args(argv)
    preserved = uninstall(home=home)
    if preserved is not None:
        print(f"Your model files were preserved in: {preserved}")
    print("Milelik removed.")
    print("Ollama/models and your Milelik workspace files were NOT touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
