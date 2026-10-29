#!/usr/bin/env bash
# Set up Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M for Milelik.
#
#   ./setup-model.sh [/path/to/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf]
#   ./setup-model.sh --download     # no GGUF handy? pull it into Ollama instead
#
# With a GGUF (given, or found in the usual places - this folder, ~/Downloads,
# ~/models, LM Studio's model folders) it imports the file into Ollama with
# the correct Qwen2.5-Coder ChatML/tools template as "milelik-coder:7b" and
# makes it Milelik's default model.
#
# Note: if the GGUF is already loaded in LM Studio, you don't need this at
# all - just start LM Studio's local server and run `milelik`; it is detected
# automatically and nothing is duplicated.
set -euo pipefail

TARGET_FILE="Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf"
OLLAMA_LIBRARY_MODEL="huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M"
IMPORT_NAME="${OLLAMA_NAME:-milelik-coder:7b}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MILELIK_PY="$SCRIPT_DIR/milelik.py"
DOWNLOAD=0
GGUF="${GGUF:-}"

for arg in "$@"; do
    case "$arg" in
        --download) DOWNLOAD=1 ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) GGUF="$arg" ;;
    esac
done

find_gguf() {
    local dir found
    for dir in "$SCRIPT_DIR" "$PWD" "$HOME/Downloads" "$HOME/models" \
               "$HOME/Models" "$HOME/.lmstudio/models" \
               "$HOME/.cache/lm-studio/models"; do
        [ -d "$dir" ] || continue
        found="$(find "$dir" -maxdepth 6 -type f \
                    -iname '*qwen2.5-coder*7b*abliterat*q4_k_m*.gguf' \
                    -print -quit 2>/dev/null || true)"
        if [ -n "$found" ]; then
            echo "$found"
            return 0
        fi
    done
    return 1
}

if [ -n "$GGUF" ] && [ -d "$GGUF" ]; then
    FOUND="$(find "$GGUF" -maxdepth 4 -type f \
                -iname '*qwen2.5-coder*7b*abliterat*q4_k_m*.gguf' \
                -print -quit 2>/dev/null || true)"
    [ -n "$FOUND" ] && GGUF="$FOUND"
fi
if [ -z "$GGUF" ]; then
    GGUF="$(find_gguf || true)"
fi

if [ -n "$GGUF" ] && [ -f "$GGUF" ]; then
    echo "Found: $GGUF"
    if ! command -v ollama >/dev/null 2>&1; then
        echo "Ollama isn't installed, so the GGUF can't be imported into it."
        echo "Run ./install.sh first - or skip Ollama: load the file in"
        echo "LM Studio, start its server (port 1234), and just run: milelik"
        exit 1
    fi
    python3 "$MILELIK_PY" --import-gguf "$GGUF" --import-name "$IMPORT_NAME"
    echo
    python3 "$MILELIK_PY" --check || true
    exit 0
fi

if [ "$DOWNLOAD" = "1" ]; then
    command -v ollama >/dev/null 2>&1 || {
        echo "Ollama isn't installed. Run ./install.sh first."; exit 1; }
    curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
        || nohup ollama serve >/dev/null 2>&1 &
    for _ in $(seq 1 50); do
        curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
        sleep 0.2
    done
    echo "Downloading $OLLAMA_LIBRARY_MODEL (~4.7 GB)…"
    ollama pull "$OLLAMA_LIBRARY_MODEL"
    python3 - "$OLLAMA_LIBRARY_MODEL" <<'PY'
import json, os, pathlib, sys
p = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "milelik" / "config.json"
cfg = {}
if p.exists():
    try: cfg = json.loads(p.read_text())
    except ValueError: cfg = {}
cfg.update({"model": sys.argv[1], "backend": "auto"})
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, indent=2))
print(f"Default model saved in {p}")
PY
    echo
    python3 "$MILELIK_PY" --check || true
    exit 0
fi

echo "Could not find $TARGET_FILE"
echo "in this folder, ~/Downloads, ~/models, or LM Studio's model folders."
echo
echo "Options:"
echo "  1. Point at your file:   ./setup-model.sh /path/to/$TARGET_FILE"
echo "  2. Download it instead:  ./setup-model.sh --download"
echo "                           (ollama pull $OLLAMA_LIBRARY_MODEL)"
echo "  3. Already loaded in LM Studio? Just start its server and run: milelik --check"
exit 1
