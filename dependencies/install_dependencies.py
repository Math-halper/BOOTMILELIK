#!/usr/bin/env python3
"""Install or check every system dependency declared in this repository.

Examples:
    python3 dependencies/install_dependencies.py
    python3 dependencies/install_dependencies.py --required
    python3 dependencies/install_dependencies.py --with-optional
    python3 dependencies/install_dependencies.py --check
    python3 dependencies/install_dependencies.py --list

The package lists beside this module are the canonical dependency manifests.
Apt resolves and installs their transitive library dependencies from the
configured linux Mint/Ubuntu/Debian repositories.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")

REQUIRED_COMMANDS = (
    "bash", "python3", "curl", "file", "find", "ip", "ss", "xdg-open",
    "pgrep", "pkill", "free", "df", "lscpu", "zstd",
)
FULL_COMMANDS = (
    "vlc", "playerctl", "gh", "git", "ffprobe", "mediainfo", "libreoffice",
    "notify-send", "xclip", "xsel", "wl-copy", "wl-paste", "brightnessctl",
    "pactl", "gnome-screenshot", "gsettings", "gtk-launch", "node", "npm",
    "pip3",
)
OPTIONAL_COMMANDS = ("feh", "import", "maim", "scrot", "xfce4-screenshooter")


def manifest_files(mode: str, root: Path = ROOT) -> list[Path]:
    files = [root / "apt-required.txt"]
    if mode in ("full", "with-optional"):
        files.append(root / "apt-tools.txt")
    if mode == "with-optional":
        files.append(root / "apt-optional.txt")
    return files


def read_packages(mode: str, root: Path = ROOT) -> list[str]:
    """Read and de-duplicate apt manifests while preserving their order."""
    packages: list[str] = []
    seen: set[str] = set()
    for path in manifest_files(mode, root):
        if not path.is_file():
            raise FileNotFoundError(f"Dependency manifest not found: {path}")
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            package = raw_line.split("#", 1)[0].strip()
            if not package:
                continue
            if not PACKAGE_RE.fullmatch(package):
                raise ValueError(f"Invalid apt package name in {path}: {package}")
            if package not in seen:
                seen.add(package)
                packages.append(package)
    return packages


def expected_commands(mode: str) -> list[str]:
    commands = list(REQUIRED_COMMANDS)
    if mode != "required":
        commands.extend(FULL_COMMANDS)
    if mode == "with-optional":
        commands.extend(OPTIONAL_COMMANDS)
    return commands


def _command_exists(command: str, *, which=shutil.which) -> bool:
    if not command:
        return False
    if os.sep in command:
        return os.access(os.path.expanduser(command), os.X_OK)
    return which(command) is not None


def check_packages(packages, dpkg_query: str, *, runner=subprocess.run,
                   which=shutil.which) -> bool:
    if not _command_exists(dpkg_query, which=which):
        print(f"{dpkg_query} not found; cannot verify apt packages.",
              file=sys.stderr)
        return False
    missing = []
    for package in packages:
        result = runner(
            [dpkg_query, "-W", "-f=${Status}", package],
            text=True, capture_output=True, check=False)
        status = getattr(result, "stdout", "") or ""
        if "install ok installed" not in status:
            missing.append(package)
    if missing:
        print(f"Missing apt packages ({len(missing)}): {' '.join(missing)}",
              file=sys.stderr)
        return False
    print(f"All {len(packages)} declared apt packages are installed.")
    return True


def check_commands(mode: str, *, which=shutil.which) -> bool:
    missing = [command for command in expected_commands(mode)
               if not _command_exists(command, which=which)]
    if missing:
        print(f"Missing commands ({len(missing)}): {' '.join(missing)}",
              file=sys.stderr)
        return False
    print("All expected dependency commands are available.")
    return True


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="install_dependencies.py",
        description="Install or check Milelik's declared system dependencies.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", dest="mode", action="store_const", const="full",
                      help="install the full set (default)")
    mode.add_argument("--required", "--minimal", dest="mode",
                      action="store_const", const="required",
                      help="install the core runtime only")
    mode.add_argument("--with-optional", "--optional", dest="mode",
                      action="store_const", const="with-optional",
                      help="install the full set plus fallback programs")
    parser.set_defaults(mode="full")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", dest="action", action="store_const",
                        const="check", help="verify packages; do not install")
    action.add_argument("--list", dest="action", action="store_const",
                        const="list", help="print package names and exit")
    parser.set_defaults(action="install")
    parser.add_argument("--no-update", action="store_true",
                        help="do not run apt-get update first")
    parser.add_argument("--dry-run", action="store_true",
                        help="ask apt to simulate the installation")
    return parser.parse_args(argv)


def main(argv=None, *, runner=subprocess.run, which=shutil.which,
         environ=None, euid=None, root: Path = ROOT) -> int:
    args = parse_args(argv)
    env = os.environ if environ is None else environ
    apt_get = env.get("APT_GET", "apt-get")
    dpkg_query = env.get("DPKG_QUERY", "dpkg-query")

    try:
        packages = read_packages(args.mode, root)
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    if args.action == "list":
        print("\n".join(packages))
        return 0

    if args.action == "check":
        ok = check_packages(packages, dpkg_query, runner=runner, which=which)
        ok = check_commands(args.mode, which=which) and ok
        return 0 if ok else 1

    if not _command_exists(apt_get, which=which):
        print(f"{apt_get} not found. Milelik's bundled system-dependency "
              "installer supports linux Mint and other apt-based "
              "distributions.", file=sys.stderr)
        return 1

    effective_euid = (os.geteuid() if hasattr(os, "geteuid") else 0) \
        if euid is None else euid
    sudo: list[str] = []
    if effective_euid != 0:
        if not _command_exists("sudo", which=which):
            print("sudo is required to install system packages (or run as root).",
                  file=sys.stderr)
            return 1
        sudo = ["sudo"]

    print(f"Installing Milelik's {args.mode} dependency set "
          f"({len(packages)} packages)…")
    if not args.no_update and not args.dry_run:
        result = runner([*sudo, apt_get, "update"], check=False)
        code = int(getattr(result, "returncode", 0) or 0)
        if code:
            return code

    apt_args = ["install", "-y"]
    if args.dry_run:
        apt_args.append("--simulate")
    result = runner([*sudo, apt_get, *apt_args, *packages], check=False)
    code = int(getattr(result, "returncode", 0) or 0)
    if code:
        return code

    if not args.dry_run and not check_packages(
            packages, dpkg_query, runner=runner, which=which):
        return 1

    print("Milelik system dependencies are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
