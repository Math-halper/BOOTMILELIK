#!/usr/bin/env bash
# Milelik installer for linux Mint (and other Ubuntu/Debian-based distros).
#
#   ./install.sh                 # default: Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M
#   GGUF=/path/to/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf ./install.sh
#                                # import a GGUF you already have (no download)
#   MODEL=qwen3:4b ./install.sh  # or pick any other Ollama model instead
#
# Every system dependency is declared in dependencies/apt-*.txt and is
# installed automatically by dependencies/install-dependencies.sh:
#   DEPENDENCY_MODE=full ./install.sh            # default: all tool helpers
#   DEPENDENCY_MODE=with-optional ./install.sh   # also optional fallbacks
#   DEPENDENCY_MODE=required ./install.sh        # core runtime packages only
#   DEPENDENCY_MODE=none ./install.sh            # skip apt dependency install
set -euo pipefail

MODEL_WAS_SET=0
[ -n "${MODEL:-}" ] && MODEL_WAS_SET=1
MODEL="${MODEL:-huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M}"
DEPENDENCY_MODE="${DEPENDENCY_MODE:-full}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/milelik"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "🤖 Installing Milelik (model: $MODEL)"
echo

# --- 1. System dependencies declared in this repository ----------------------
# The manifests in dependencies/apt-*.txt are canonical: apt-required.txt is
# the core runtime, apt-tools.txt adds VLC/media, desktop, LibreOffice, Git
# and GitHub helpers, and apt-optional.txt holds extra fallback programs.
case "$DEPENDENCY_MODE" in
    full)
        "$SCRIPT_DIR/dependencies/install-dependencies.sh" --full
        ;;
    required|minimal)
        "$SCRIPT_DIR/dependencies/install-dependencies.sh" --required
        ;;
    with-optional|optional)
        "$SCRIPT_DIR/dependencies/install-dependencies.sh" --with-optional
        ;;
    none|skip)
        echo "Skipping bundled dependency installation (DEPENDENCY_MODE=$DEPENDENCY_MODE)."
        ;;
    *)
        echo "Invalid DEPENDENCY_MODE: $DEPENDENCY_MODE" >&2
        echo "Use full, with-optional, required or none." >&2
        exit 2
        ;;
esac

if ! command -v python3 >/dev/null 2>&1 || \
   ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Milelik needs Python 3.10 or newer." >&2
    echo "Install the repository's dependencies with:" >&2
    echo "  $SCRIPT_DIR/dependencies/install-dependencies.sh --required" >&2
    exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
    echo "Milelik's installer needs curl." >&2
    echo "Install the repository's dependencies with:" >&2
    echo "  $SCRIPT_DIR/dependencies/install-dependencies.sh --required" >&2
    exit 1
fi

# --- 2. Ollama (local model runtime) ------------------------------------------
if ! command -v ollama >/dev/null 2>&1; then
    echo "Installing Ollama…"
    curl -fsSL https://ollama.com/install.sh | sh
fi

# Start Ollama now and on boot, if systemd is available.
if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl enable --now ollama 2>/dev/null || true
fi
if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "Starting Ollama in the background…"
    nohup ollama serve >/dev/null 2>&1 &
fi
echo -n "Waiting for Ollama"
for _ in $(seq 1 50); do
    if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then break; fi
    echo -n "."; sleep 0.2
done
echo

# --- 3. The model --------------------------------------------------------------
# Normalise aliases of the target model (GGUF filename, HF name, ...) to the
# Ollama-library tag, using Milelik's own resolver.
MODEL="$(python3 - "$SCRIPT_DIR/milelik.py" "$MODEL" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("milelik", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(m.resolve_ollama_model_name(sys.argv[2]))
PY
)"

# Already have the GGUF? Import it instead of downloading another copy.
GGUF_CANDIDATE="${GGUF:-}"
if [ -z "$GGUF_CANDIDATE" ] && [ "$MODEL_WAS_SET" = "0" ]; then
    for d in "$SCRIPT_DIR" "$PWD" "$HOME/Downloads" "$HOME/models" \
             "$HOME/Models" "$HOME/.lmstudio/models" \
             "$HOME/.cache/lm-studio/models"; do
        [ -d "$d" ] || continue
        GGUF_CANDIDATE="$(find "$d" -maxdepth 6 -type f \
            -iname '*qwen2.5-coder*7b*abliterat*q4_k_m*.gguf' -print -quit \
            2>/dev/null || true)"
        [ -n "$GGUF_CANDIDATE" ] && break
    done
fi

if [ -n "$GGUF_CANDIDATE" ] && [ -f "$GGUF_CANDIDATE" ]; then
    echo "Found your GGUF: $GGUF_CANDIDATE"
    echo "Importing it (no download)…"
    "$SCRIPT_DIR/setup-model.sh" "$GGUF_CANDIDATE"
    MODEL="milelik-coder:7b"
else
    echo "Downloading model '$MODEL' (first time only, ~4.7 GB)…"
    ollama pull "$MODEL"
fi

# --- 4. Milelik itself ------------------------------------------------------------
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
install -m 0755 "$SCRIPT_DIR/milelik.py" "$APP_DIR/milelik.py"
for extra in setup-model.sh Modelfile.qwen2.5-coder-abliterated; do
    if [ -f "$SCRIPT_DIR/$extra" ]; then
        install -m 0755 "$SCRIPT_DIR/$extra" "$APP_DIR/$extra"
    fi
done
# Keep the dependency manifests/installer with the installed app, so a
# missing helper can be fixed without returning to the source checkout.
rm -rf "$APP_DIR/dependencies"
cp -a "$SCRIPT_DIR/dependencies" "$APP_DIR/dependencies"
chmod +x "$APP_DIR/dependencies/install-dependencies.sh"
for extra in requirements.txt pyproject.toml; do
    if [ -f "$SCRIPT_DIR/$extra" ]; then
        install -m 0644 "$SCRIPT_DIR/$extra" "$APP_DIR/$extra"
    fi
done
cat > "$BIN_DIR/milelik" <<EOF
#!/usr/bin/env bash
exec python3 "$APP_DIR/milelik.py" "\$@"
EOF
chmod +x "$BIN_DIR/milelik"
if [ -f "$SCRIPT_DIR/milelik.desktop" ]; then
    install -m 0644 "$SCRIPT_DIR/milelik.desktop" "$DESKTOP_DIR/milelik.desktop"
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo
       echo "⚠  $BIN_DIR is not on your PATH in this shell."
       echo "   linux Mint adds it automatically at login - open a NEW terminal,"
       echo "   or run:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# Remember the chosen model so plain `milelik` uses it.
MILELIK_MODEL="$MODEL" python3 "$APP_DIR/milelik.py" --init-config >/dev/null
python3 - "$MODEL" <<'PY'
import json, os, pathlib, sys
p = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "milelik" / "config.json"
cfg = json.loads(p.read_text()) if p.exists() else {}
cfg["model"] = sys.argv[1]
cfg.setdefault("backend", "auto")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, indent=2))
PY

echo
"$BIN_DIR/milelik" --check || true
echo
echo "✅ Done. Try it:"
echo "   milelik \"what is my local IP, public IP, and how much disk space is free?\""
echo "   milelik --interactive"
