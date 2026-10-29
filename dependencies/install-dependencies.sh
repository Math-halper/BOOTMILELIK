#!/usr/bin/env bash
# Install or check every system dependency declared in this repository.
#
#   ./dependencies/install-dependencies.sh                 # full set (default)
#   ./dependencies/install-dependencies.sh --required      # core runtime only
#   ./dependencies/install-dependencies.sh --with-optional # full + fallbacks
#   ./dependencies/install-dependencies.sh --check         # verify, don't install
#   ./dependencies/install-dependencies.sh --list          # print package names
#
# The package lists beside this script are the canonical dependency
# manifests. Apt resolves and installs their transitive library
# dependencies from the configured linux Mint/Ubuntu/Debian repositories.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="full"
ACTION="install"
UPDATE=1
DRY_RUN=0

APT_GET="${APT_GET:-apt-get}"
DPKG_QUERY="${DPKG_QUERY:-dpkg-query}"

usage() {
    # Print the leading comment block, stopping at the first non-comment.
    awk 'NR == 1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --full) MODE="full" ;;
        --required|--minimal) MODE="required" ;;
        --with-optional|--optional) MODE="with-optional" ;;
        --check) ACTION="check" ;;
        --list) ACTION="list" ;;
        --no-update) UPDATE=0 ;;
        --dry-run) DRY_RUN=1; UPDATE=0 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Try: $0 --help" >&2
            exit 2
            ;;
    esac
    shift
done

PACKAGE_FILES=("$SCRIPT_DIR/apt-required.txt")
case "$MODE" in
    full)
        PACKAGE_FILES+=("$SCRIPT_DIR/apt-tools.txt")
        ;;
    with-optional)
        PACKAGE_FILES+=("$SCRIPT_DIR/apt-tools.txt" "$SCRIPT_DIR/apt-optional.txt")
        ;;
    required)
        ;;
esac

# Read and de-duplicate the manifests while preserving their order.
declare -A _SEEN=()
PACKAGES=()
for file in "${PACKAGE_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo "Dependency manifest not found: $file" >&2
        exit 1
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"
        # Trim leading/trailing whitespace without relying on xargs.
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [ -n "$line" ] || continue
        if [[ ! "$line" =~ ^[a-z0-9][a-z0-9.+-]*$ ]]; then
            echo "Invalid apt package name in $file: $line" >&2
            exit 1
        fi
        if [ -z "${_SEEN[$line]:-}" ]; then
            _SEEN[$line]=1
            PACKAGES+=("$line")
        fi
    done < "$file"
done

if [ "$ACTION" = "list" ]; then
    printf '%s\n' "${PACKAGES[@]}"
    exit 0
fi

check_packages() {
    local missing=() pkg status
    if ! command -v "$DPKG_QUERY" >/dev/null 2>&1; then
        echo "$DPKG_QUERY not found; cannot verify apt packages." >&2
        return 1
    fi
    for pkg in "${PACKAGES[@]}"; do
        status="$($DPKG_QUERY -W -f='${Status}' "$pkg" 2>/dev/null || true)"
        case "$status" in
            *"install ok installed"*) ;;
            *) missing+=("$pkg") ;;
        esac
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        echo "Missing apt packages (${#missing[@]}): ${missing[*]}" >&2
        return 1
    fi
    echo "All ${#PACKAGES[@]} declared apt packages are installed."
}

check_commands() {
    local commands=(bash python3 curl file find ip ss xdg-open pgrep pkill free df lscpu)
    local missing=() cmd
    if [ "$MODE" != "required" ]; then
        commands+=(vlc playerctl gh git ffprobe mediainfo libreoffice
                   notify-send xclip xsel wl-copy wl-paste brightnessctl
                   pactl gnome-screenshot gsettings gtk-launch)
    fi
    if [ "$MODE" = "with-optional" ]; then
        commands+=(feh import maim scrot xfce4-screenshooter)
    fi
    for cmd in "${commands[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        echo "Missing commands (${#missing[@]}): ${missing[*]}" >&2
        return 1
    fi
    echo "All expected dependency commands are available."
}

if [ "$ACTION" = "check" ]; then
    rc=0
    check_packages || rc=1
    check_commands || rc=1
    exit "$rc"
fi

if ! command -v "$APT_GET" >/dev/null 2>&1; then
    echo "$APT_GET not found. Milelik's bundled system-dependency installer" >&2
    echo "supports linux Mint and other apt-based distributions." >&2
    exit 1
fi

SUDO=()
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "sudo is required to install system packages (or run as root)." >&2
        exit 1
    fi
    SUDO=(sudo)
fi

echo "Installing Milelik's $MODE dependency set (${#PACKAGES[@]} packages)…"
if [ "$UPDATE" -eq 1 ]; then
    "${SUDO[@]}" "$APT_GET" update
fi

APT_ARGS=(install -y)
if [ "$DRY_RUN" -eq 1 ]; then
    APT_ARGS+=(--simulate)
fi
"${SUDO[@]}" "$APT_GET" "${APT_ARGS[@]}" "${PACKAGES[@]}"

if [ "$DRY_RUN" -eq 0 ]; then
    check_packages
fi

echo "Milelik system dependencies are ready."
