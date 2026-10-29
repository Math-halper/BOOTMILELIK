#!/usr/bin/env bash
# Milelik installer for linux Mint (and other Ubuntu/Debian-based distros).
#
#   ./install.sh                 # default model: qwen3:8b
#   MODEL=qwen3:4b ./install.sh  # smaller/faster model for weaker hardware
#   MODEL=qwen3:14b ./install.sh # smarter, slower, needs more RAM/VRAM
set -euo pipefail

MODEL="${MODEL:-qwen3:8b}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/milelik"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "🤖 Installing Milelik (model: $MODEL)"
echo

# --- 1. Python 3.10+ (ships with linux Mint 21/22) ---------------------------
if ! command -v python3 >/dev/null 2>&1 || \
   ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Installing Python 3…"
    sudo apt-get update && sudo apt-get install -y python3 curl
fi
command -v curl >/dev/null 2>&1 || sudo apt-get install -y curl

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
echo "Downloading model '$MODEL' (first time only, a few GB)…"
ollama pull "$MODEL"

# --- 4. Milelik itself ------------------------------------------------------------
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
install -m 0755 "$SCRIPT_DIR/milelik.py" "$APP_DIR/milelik.py"
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
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, indent=2))
PY

echo
"$BIN_DIR/milelik" --check || true
echo
echo "✅ Done. Try it:"
echo "   milelik \"what is my local IP, public IP, and how much disk space is free?\""
echo "   milelik --interactive"
