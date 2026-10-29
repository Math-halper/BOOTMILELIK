#!/usr/bin/env bash
# Milelik installer for linux Mint (and other Ubuntu/Debian-based distros).
#
# Easy interactive install (asks about your preferences):
#   ./install.sh
#
# Automated/default install:
#   ./install.sh --yes
#
# Useful alternatives:
#   GGUF=/path/to/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf ./install.sh
#   MODEL=qwen3:4b ./install.sh --yes
#   MODEL_SOURCE=server BACKEND=openai HOST=http://127.0.0.1:1234 ./install.sh --yes
#   DEPENDENCY_MODE=with-optional ./install.sh
#
# Every system dependency is declared in dependencies/apt-*.txt and installed
# automatically by dependencies/install-dependencies.sh.
set -euo pipefail

BUNDLED_MODEL_FILE="smollm2-135m-instruct-q4_1.gguf"
BUNDLED_MODEL_LABEL="SmolLM2-135M-Instruct-Q4_1"
BUNDLED_OLLAMA_MODEL="milelik-smollm2:135m"
# Optional larger coder upgrade (still fully supported when selected).
TARGET_OLLAMA_MODEL="huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M"
TARGET_MODEL_LABEL="Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M"

MODEL_WAS_SET=0
[ -n "${MODEL:-}" ] && MODEL_WAS_SET=1
MODEL="${MODEL:-$BUNDLED_OLLAMA_MODEL}"
GGUF="${GGUF:-}"
MODEL_SOURCE="${MODEL_SOURCE:-auto}"
BACKEND="${BACKEND:-auto}"
MODEL_HOST="${MODEL_HOST:-${HOST:-}}"
MODELS_DIR="${MODELS_DIR:-}"
MODEL_FILE="${MODEL_FILE:-}"
MODEL_DISCOVERY="${MODEL_DISCOVERY:-1}"
DEPENDENCY_MODE="${DEPENDENCY_MODE:-full}"

PERFORMANCE_PROFILE="${PERFORMANCE_PROFILE:-balanced}"
CTX_WAS_SET=0; [ -n "${CTX:-}" ] && CTX_WAS_SET=1
MAX_STEPS_WAS_SET=0; [ -n "${MAX_STEPS:-}" ] && MAX_STEPS_WAS_SET=1
CMD_TIMEOUT_WAS_SET=0; [ -n "${CMD_TIMEOUT:-}" ] && CMD_TIMEOUT_WAS_SET=1
CTX="${CTX:-8192}"
MAX_STEPS="${MAX_STEPS:-12}"
CMD_TIMEOUT="${CMD_TIMEOUT:-60}"
AUTO_APPROVE="${AUTO_APPROVE:-0}"

FRONTEND_ENABLED="${FRONTEND_ENABLED:-1}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"
WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-8765}"
WORKSPACE_DIR="${WORKSPACE_DIR:-$HOME/MilelikWorkspace}"
UI_THEME="${UI_THEME:-system}"
UI_DEFAULT_VIEW="${UI_DEFAULT_VIEW:-chat}"
GITHUB_LOGIN="${GITHUB_LOGIN:-0}"
LAUNCH_WAS_SET=0; [ -n "${LAUNCH_AFTER_INSTALL:-}" ] && LAUNCH_WAS_SET=1
LAUNCH_AFTER_INSTALL="${LAUNCH_AFTER_INSTALL:-none}"

ASSUME_YES=0
FORCE_WIZARD=0
SKIP_CHECK="${SKIP_CHECK:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/milelik"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

usage() {
    cat <<'EOF'
Milelik installer

Usage:
  ./install.sh [--yes] [options]

Interactive mode:
  (default in a terminal)  Ask about model, dependencies, safety, front end,
                           workspace, theme and GitHub preferences
  --wizard                 Force the preference questions
  --yes, -y                Install with defaults / environment values
  --non-interactive        Same as --yes

Model options:
  --model MODEL            Ollama model or LM Studio/llama.cpp model id
  --gguf PATH              Import an existing GGUF into Ollama
  --models-dir DIR         Folder Milelik reads for AI model files at startup
  --model-file SELECTOR    Preferred model file/path/id inside models/
  --model-scan/--no-model-scan
                           Read models/ at startup (default: yes)
  --backend BACKEND        auto, ollama or openai
  --host URL               Model server URL (for LM Studio/llama.cpp)

Other options:
  --dependency-mode MODE   full, with-optional, required or none
  --workspace DIR          Milelik web workspace folder
  --web-host HOST          Front-end bind address (default 127.0.0.1)
  --web-port PORT          Front-end port (default 8765)
  --frontend/--no-frontend Install the browser front end (default: yes)
  --launch MODE            After install: frontend, cli or none
  --github-login           Run `gh auth login` at the end
  --skip-check             Do not run `milelik --check` at the end
  -h, --help               Show this help

Environment variables still work too: MODEL, GGUF, MODEL_SOURCE, BACKEND,
HOST, DEPENDENCY_MODE, CTX, MAX_STEPS, CMD_TIMEOUT, WORKSPACE_DIR and others.
EOF
}

bool_value() {
    case "$(printf '%s' "${1:-0}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|y|on) printf '1\n' ;;
        *) printf '0\n' ;;
    esac
}

expand_path() {
    local path="${1:-}"
    case "$path" in
        "~") printf '%s\n' "$HOME" ;;
        "~/"*) printf '%s/%s\n' "$HOME" "${path#~/}" ;;
        /*) printf '%s\n' "$path" ;;
        *) printf '%s/%s\n' "$PWD" "$path" ;;
    esac
}

need_value() {
    if [ "$#" -lt 2 ]; then
        echo "Option $1 needs a value." >&2
        exit 2
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        -y|--yes|--non-interactive|--noninteractive) ASSUME_YES=1 ;;
        --wizard) FORCE_WIZARD=1 ;;
        --skip-check) SKIP_CHECK=1 ;;
        --model) need_value "$@"; MODEL="$2"; MODEL_WAS_SET=1; shift ;;
        --gguf) need_value "$@"; GGUF="$2"; MODEL_SOURCE="gguf"; shift ;;
        --models-dir) need_value "$@"; MODELS_DIR="$2"; shift ;;
        --model-file) need_value "$@"; MODEL_FILE="$2"; shift ;;
        --model-scan) MODEL_DISCOVERY=1 ;;
        --no-model-scan) MODEL_DISCOVERY=0 ;;
        --backend) need_value "$@"; BACKEND="$2"; shift ;;
        --host) need_value "$@"; MODEL_HOST="$2"; shift ;;
        --dependency-mode) need_value "$@"; DEPENDENCY_MODE="$2"; shift ;;
        --workspace|--workspace-dir) need_value "$@"; WORKSPACE_DIR="$2"; shift ;;
        --web-host) need_value "$@"; WEB_HOST="$2"; shift ;;
        --web-port) need_value "$@"; WEB_PORT="$2"; shift ;;
        --frontend) FRONTEND_ENABLED=1 ;;
        --no-frontend) FRONTEND_ENABLED=0 ;;
        --launch) need_value "$@"; LAUNCH_AFTER_INSTALL="$2"; LAUNCH_WAS_SET=1; shift ;;
        --github-login) GITHUB_LOGIN=1 ;;
        --no-github-login) GITHUB_LOGIN=0 ;;
        --auto-approve) AUTO_APPROVE=1 ;;
        --ask-first) AUTO_APPROVE=0 ;;
        *)
            echo "Unknown installer option: $1" >&2
            echo "Try: ./install.sh --help" >&2
            exit 2
            ;;
    esac
    shift
done

FRONTEND_ENABLED="$(bool_value "$FRONTEND_ENABLED")"
OPEN_BROWSER="$(bool_value "$OPEN_BROWSER")"
AUTO_APPROVE="$(bool_value "$AUTO_APPROVE")"
GITHUB_LOGIN="$(bool_value "$GITHUB_LOGIN")"
SKIP_CHECK="$(bool_value "$SKIP_CHECK")"
MODEL_DISCOVERY="$(bool_value "$MODEL_DISCOVERY")"

if [ -n "$GGUF" ] && [ "$MODEL_SOURCE" = "auto" ]; then
    MODEL_SOURCE="gguf"
elif [ "$MODEL_SOURCE" = "auto" ] && [ "$MODEL_WAS_SET" = "1" ] && \
     [ "$MODEL" != "$BUNDLED_OLLAMA_MODEL" ] && [ "$MODEL" != "$BUNDLED_MODEL_LABEL" ] && \
     [ "$MODEL" != "$BUNDLED_MODEL_FILE" ] && \
     [ "$MODEL" != "$TARGET_OLLAMA_MODEL" ] && [ "$MODEL" != "$TARGET_MODEL_LABEL" ]; then
    MODEL_SOURCE="custom"
fi
if [ "$BACKEND" = "openai" ] && [ "$MODEL_SOURCE" = "auto" ]; then
    MODEL_SOURCE="server"
fi

apply_performance_profile() {
    case "$PERFORMANCE_PROFILE" in
        fast)
            [ "$CTX_WAS_SET" = "0" ] && CTX=4096
            [ "$MAX_STEPS_WAS_SET" = "0" ] && MAX_STEPS=8
            [ "$CMD_TIMEOUT_WAS_SET" = "0" ] && CMD_TIMEOUT=45
            ;;
        balanced)
            [ "$CTX_WAS_SET" = "0" ] && CTX=8192
            [ "$MAX_STEPS_WAS_SET" = "0" ] && MAX_STEPS=12
            [ "$CMD_TIMEOUT_WAS_SET" = "0" ] && CMD_TIMEOUT=60
            ;;
        quality)
            [ "$CTX_WAS_SET" = "0" ] && CTX=16384
            [ "$MAX_STEPS_WAS_SET" = "0" ] && MAX_STEPS=16
            [ "$CMD_TIMEOUT_WAS_SET" = "0" ] && CMD_TIMEOUT=90
            ;;
        custom) ;;
        *)
            echo "Invalid PERFORMANCE_PROFILE: $PERFORMANCE_PROFILE" >&2
            echo "Use fast, balanced, quality or custom." >&2
            exit 2
            ;;
    esac
}
apply_performance_profile

ask_text() {
    local var_name="$1" label="$2" default_value="${3:-}" answer=""
    printf '\n%s\n' "$label"
    printf '  Default: %s\n' "$default_value"
    IFS= read -r -p '  Your choice (Enter = default): ' answer || true
    [ -n "$answer" ] || answer="$default_value"
    printf -v "$var_name" '%s' "$answer"
}

ask_yes_no() {
    local var_name="$1" label="$2" default_value="${3:-0}" answer=""
    local hint="[y/N]"
    [ "$default_value" = "1" ] && hint="[Y/n]"
    while true; do
        printf '\n%s %s ' "$label" "$hint"
        IFS= read -r answer || true
        answer="${answer,,}"
        case "$answer" in
            "") printf -v "$var_name" '%s' "$default_value"; return ;;
            y|yes|true|1|on) printf -v "$var_name" '1'; return ;;
            n|no|false|0|off) printf -v "$var_name" '0'; return ;;
            *) echo '  Please answer y or n.' ;;
        esac
    done
}

# ask_choice VARIABLE "Question" default-value "value|Friendly label" ...
ask_choice() {
    local var_name="$1" label="$2" default_value="$3"
    shift 3
    local -a values=() labels=()
    local spec answer="" index=1 default_index=1 i
    for spec in "$@"; do
        values+=("${spec%%|*}")
        labels+=("${spec#*|}")
    done
    for i in "${!values[@]}"; do
        if [ "${values[$i]}" = "$default_value" ]; then
            default_index=$((i + 1))
        fi
    done
    while true; do
        printf '\n%s\n' "$label"
        for i in "${!values[@]}"; do
            if [ "$((i + 1))" -eq "$default_index" ]; then
                printf '  %s) %s  [default]\n' "$((i + 1))" "${labels[$i]}"
            else
                printf '  %s) %s\n' "$((i + 1))" "${labels[$i]}"
            fi
        done
        printf '  Choose 1-%s (Enter = %s): ' "${#values[@]}" "$default_index"
        IFS= read -r answer || true
        answer="${answer,,}"
        if [ -z "$answer" ]; then
            printf -v "$var_name" '%s' "${values[$((default_index - 1))]}"
            return
        fi
        if [[ "$answer" =~ ^[0-9]+$ ]] && [ "$answer" -ge 1 ] && [ "$answer" -le "${#values[@]}" ]; then
            printf -v "$var_name" '%s' "${values[$((answer - 1))]}"
            return
        fi
        for i in "${!values[@]}"; do
            if [ "${values[$i],,}" = "$answer" ]; then
                printf -v "$var_name" '%s' "${values[$i]}"
                return
            fi
        done
        echo '  Please choose one of the numbers above.'
    done
}

ask_int() {
    local var_name="$1" label="$2" default_value="$3" minimum="$4" maximum="$5" answer=""
    while true; do
        printf '\n%s (%s–%s)\n' "$label" "$minimum" "$maximum"
        printf '  Default: %s\n' "$default_value"
        printf '  Your choice: '
        IFS= read -r answer || true
        [ -n "$answer" ] || answer="$default_value"
        if [[ "$answer" =~ ^[0-9]+$ ]] && [ "$answer" -ge "$minimum" ] && [ "$answer" -le "$maximum" ]; then
            printf -v "$var_name" '%s' "$answer"
            return
        fi
        echo "  Please enter a whole number from $minimum to $maximum."
    done
}

print_summary() {
    echo
    echo "──────────────── Milelik install choices ────────────────"
    echo "  Dependencies:     $DEPENDENCY_MODE"
    echo "  Model source:     $MODEL_SOURCE"
    echo "  Model:            $MODEL"
    [ -n "$GGUF" ] && echo "  GGUF:             $GGUF"
    if [ "$MODEL_DISCOVERY" = "1" ]; then
        echo "  Models folder:    ${MODELS_DIR:-Milelik models/ (auto-detected)}"
        echo "  Preferred file:   ${MODEL_FILE:-automatic / ask at startup}"
    else
        echo "  Models folder:    not scanned at startup"
    fi
    echo "  Backend:          $BACKEND"
    [ -n "$MODEL_HOST" ] && echo "  Model server:     $MODEL_HOST"
    echo "  Performance:      $PERFORMANCE_PROFILE (ctx $CTX, $MAX_STEPS steps, ${CMD_TIMEOUT}s commands)"
    if [ "$AUTO_APPROVE" = "1" ]; then
        echo "  Safety:           auto-approve ordinary actions (dangerous/sudo still ask)"
    else
        echo "  Safety:           ask before actions"
    fi
    if [ "$FRONTEND_ENABLED" = "1" ]; then
        echo "  Front end:        yes — http://$WEB_HOST:$WEB_PORT"
        echo "  Workspace:        $WORKSPACE_DIR"
        echo "  Theme/open page:  $UI_THEME / $UI_DEFAULT_VIEW"
        echo "  Open browser:     $OPEN_BROWSER"
    else
        echo "  Front end:        no"
    fi
    echo "  GitHub login:     $GITHUB_LOGIN"
    echo "  Launch afterwards:$LAUNCH_AFTER_INSTALL"
    echo "─────────────────────────────────────────────────────────"
}

run_setup_wizard() {
    echo
    echo "Milelik setup wizard"
    echo "I'll ask a few preference questions. Press Enter whenever the default is fine."
    echo "You can change almost all of these later from the front end's Preferences page."

    echo
    echo "Step 1 of 7 — system dependencies"
    ask_choice DEPENDENCY_MODE "Which bundled dependency set should I install?" "$DEPENDENCY_MODE" \
        "full|Full (recommended) — VLC, GitHub, LibreOffice, media/desktop/code tools" \
        "with-optional|Full + optional fallback screenshot/wallpaper programs" \
        "required|Core only — fastest install, some app tools will be unavailable" \
        "none|None — I will manage system packages myself"

    echo
    echo "Step 2 of 7 — your AI model"
    ask_choice MODEL_SOURCE "Which AI model should Milelik use by default?" "$MODEL_SOURCE" \
        "auto|Bundled $BUNDLED_MODEL_LABEL (recommended) — already in models/, import it into Ollama" \
        "gguf|I already have a GGUF file — import that exact file into Ollama" \
        "server|A model is already loaded in LM Studio or llama.cpp — do not install another copy" \
        "custom|Use a different Ollama model (for example the larger Qwen2.5-Coder)"

    case "$MODEL_SOURCE" in
        gguf)
            while true; do
                ask_text GGUF "Path to your GGUF file:" "${GGUF:-$SCRIPT_DIR/models/$BUNDLED_MODEL_FILE}"
                GGUF="$(expand_path "$GGUF")"
                [ -f "$GGUF" ] && break
                echo "  I can't find that file yet: $GGUF"
                echo "  Check the path, or press Ctrl+C and choose the bundled model instead."
            done
            BACKEND="ollama"
            # The exact Ollama name is derived from the chosen file during import.
            ;;
        server)
            local server_kind="lmstudio"
            [ "$MODEL_HOST" = "http://127.0.0.1:8080" ] && server_kind="llamacpp"
            ask_choice server_kind "Which local model server are you using?" "$server_kind" \
                "lmstudio|LM Studio on port 1234" \
                "llamacpp|llama.cpp / llama-server on port 8080" \
                "custom|Another OpenAI-compatible local address"
            case "$server_kind" in
                lmstudio) MODEL_HOST="http://127.0.0.1:1234" ;;
                llamacpp) MODEL_HOST="http://127.0.0.1:8080" ;;
                custom) ask_text MODEL_HOST "Model server URL:" "${MODEL_HOST:-http://127.0.0.1:1234}" ;;
            esac
            if [ "$MODEL_WAS_SET" = "0" ]; then
                MODEL="smollm2-135m-instruct-q4_1"
            fi
            ask_text MODEL "Model name/id shown by that server:" "$MODEL"
            BACKEND="openai"
            ;;
        custom)
            ask_text MODEL "Ollama model to download/use:" "${MODEL:-qwen3:4b}"
            MODEL_WAS_SET=1
            BACKEND="ollama"
            ;;
        auto)
            if [ "$MODEL_WAS_SET" = "0" ]; then
                MODEL="$BUNDLED_OLLAMA_MODEL"
                [ -z "$MODEL_FILE" ] && MODEL_FILE="$BUNDLED_MODEL_FILE"
            fi
            BACKEND="auto"
            ;;
    esac

    ask_yes_no MODEL_DISCOVERY "Should Milelik read all AI model files in its models/ folder at startup and choose from them?" "$MODEL_DISCOVERY"
    if [ "$MODEL_DISCOVERY" = "1" ]; then
        local models_location="installed"
        if [ "$MODELS_DIR" = "$SCRIPT_DIR/models" ]; then
            models_location="repository"
        elif [ -n "$MODELS_DIR" ]; then
            models_location="custom"
        fi
        ask_choice models_location "Which models folder should Milelik read?" "$models_location" \
            "installed|Installed Milelik models folder (~/.local/share/milelik/models)" \
            "repository|This repository's models folder ($SCRIPT_DIR/models)" \
            "custom|Another folder"
        case "$models_location" in
            installed) MODELS_DIR="" ;;
            repository) MODELS_DIR="$SCRIPT_DIR/models" ;;
            custom) ask_text MODELS_DIR "Folder containing your AI model files:" "${MODELS_DIR:-$HOME/models}" ;;
        esac
        if [ "$MODEL_SOURCE" != "gguf" ]; then
            ask_text MODEL_FILE "Preferred model filename/path inside models/ (blank = Milelik chooses automatically):" "$MODEL_FILE"
        fi
    fi

    echo
    echo "Step 3 of 7 — speed versus quality"
    ask_choice PERFORMANCE_PROFILE "Pick a performance profile:" "$PERFORMANCE_PROFILE" \
        "balanced|Balanced (recommended) — 8192 context, 12 tool steps" \
        "fast|Fast / lower RAM — 4096 context, 8 tool steps" \
        "quality|Quality / more RAM — 16384 context, 16 tool steps" \
        "custom|Custom — choose the numbers myself"
    CTX_WAS_SET=0; MAX_STEPS_WAS_SET=0; CMD_TIMEOUT_WAS_SET=0
    apply_performance_profile
    if [ "$PERFORMANCE_PROFILE" = "custom" ]; then
        ask_int CTX "Model context size (tokens)" "$CTX" 1024 131072
        ask_int MAX_STEPS "Maximum tool steps per task" "$MAX_STEPS" 1 50
        ask_int CMD_TIMEOUT "Default shell-command timeout (seconds)" "$CMD_TIMEOUT" 5 900
    fi

    echo
    echo "Step 4 of 7 — safety"
    ask_choice AUTO_APPROVE "When Milelik wants to run a command or make a change:" "$AUTO_APPROVE" \
        "0|Ask me first (recommended)" \
        "1|Auto-approve ordinary actions (dangerous and sudo actions still ask)"

    echo
    echo "Step 5 of 7 — browser front end and workspace"
    ask_yes_no FRONTEND_ENABLED "Install Milelik's browser front end, coding workspace and local APIs?" "$FRONTEND_ENABLED"
    if [ "$FRONTEND_ENABLED" = "1" ]; then
        ask_text WORKSPACE_DIR "Where should Milelik keep your editable/runnable workspace?" "$WORKSPACE_DIR"
        ask_text WEB_HOST "Front-end bind address (127.0.0.1 = only this PC; 0.0.0.0 = trusted LAN):" "$WEB_HOST"
        ask_int WEB_PORT "Front-end port" "$WEB_PORT" 1 65535
        ask_yes_no OPEN_BROWSER "Open your browser automatically when the front end starts?" "$OPEN_BROWSER"
        ask_choice UI_THEME "Which front-end theme do you prefer?" "$UI_THEME" \
            "system|Match my system theme" \
            "dark|Dark" \
            "light|Light"
        ask_choice UI_DEFAULT_VIEW "Which page should Milelik open on?" "$UI_DEFAULT_VIEW" \
            "chat|Chat" \
            "workspace|Workspace / code runner" \
            "apis|APIs for coding, maths and utilities" \
            "settings|Preferences"
    fi

    echo
    echo "Step 6 of 7 — GitHub (optional)"
    ask_yes_no GITHUB_LOGIN "Run GitHub login (gh auth login) after installation?" "$GITHUB_LOGIN"

    echo
    echo "Step 7 of 7 — what to open when installation finishes"
    if [ "$LAUNCH_WAS_SET" = "1" ]; then
        echo "Launch after install: $LAUNCH_AFTER_INSTALL (set by command line or environment)"
    elif [ "$FRONTEND_ENABLED" = "1" ]; then
        local default_launch="$LAUNCH_AFTER_INSTALL"
        [ "$default_launch" = "none" ] && default_launch="frontend"
        ask_choice LAUNCH_AFTER_INSTALL "After installing:" "$default_launch" \
            "frontend|Open the Milelik front end/workspace" \
            "cli|Show me the terminal command (do not open anything)" \
            "none|Finish quietly"
    else
        ask_choice LAUNCH_AFTER_INSTALL "After installing:" "$LAUNCH_AFTER_INSTALL" \
            "cli|Show me the terminal command" \
            "none|Finish quietly"
    fi

    print_summary
    local proceed=1
    ask_yes_no proceed "Proceed with these choices?" 1
    if [ "$proceed" != "1" ]; then
        echo "Installation cancelled. Nothing was installed."
        # setup_milelik.py sets this so it knows not to launch Milelik after
        # a cancelled wizard. Direct ./install.sh runs keep exit code 0.
        exit "${MILELIK_WIZARD_CANCEL_EXIT:-0}"
    fi
}

if [ "${EUID:-1}" -eq 0 ] && [ "${MILELIK_ALLOW_ROOT:-0}" != "1" ]; then
    echo "Error: do not run the Milelik installer as root/sudo." >&2
    echo "Run it as your normal desktop user:" >&2
    echo "  python3 setup_milelik.py" >&2
    echo "The installer requests sudo only for system packages/services. Running" >&2
    echo "the whole installer as root would install Milelik under /root instead" >&2
    echo "of your own account. Set MILELIK_ALLOW_ROOT=1 only for a root-only test" >&2
    echo "environment." >&2
    exit 2
fi

INTERACTIVE=0
if [ "$FORCE_WIZARD" = "1" ] || { [ "$ASSUME_YES" = "0" ] && [ -t 0 ] && [ -t 1 ]; }; then
    INTERACTIVE=1
fi

echo "Installing Milelik"
if [ "$INTERACTIVE" = "1" ]; then
    run_setup_wizard
else
    echo "(non-interactive/default mode — run ./install.sh in a terminal for preference questions)"
    print_summary
fi

case "$MODEL_SOURCE" in
    auto|gguf|server|custom) ;;
    *) echo "Invalid MODEL_SOURCE: $MODEL_SOURCE" >&2; exit 2 ;;
esac
case "$BACKEND" in
    auto|ollama|openai) ;;
    *) echo "Invalid BACKEND: $BACKEND (use auto, ollama or openai)" >&2; exit 2 ;;
esac
case "$LAUNCH_AFTER_INSTALL" in
    frontend|cli|none) ;;
    *) echo "Invalid LAUNCH_AFTER_INSTALL: $LAUNCH_AFTER_INSTALL" >&2; exit 2 ;;
esac
if ! [[ "$WEB_PORT" =~ ^[0-9]+$ ]] || [ "$WEB_PORT" -lt 1 ] || [ "$WEB_PORT" -gt 65535 ]; then
    echo "Invalid WEB_PORT: $WEB_PORT" >&2
    exit 2
fi
[ -n "$GGUF" ] && GGUF="$(expand_path "$GGUF")"
[ -n "$MODELS_DIR" ] && MODELS_DIR="$(expand_path "$MODELS_DIR")"
case "$MODEL_FILE" in
    */*|"~"|"~"/*) MODEL_FILE="$(expand_path "$MODEL_FILE")" ;;
esac
[ -z "$MODEL_FILE" ] && [ -n "$GGUF" ] && MODEL_FILE="$GGUF"
WORKSPACE_DIR="$(expand_path "$WORKSPACE_DIR")"

# --- 1. System dependencies declared in this repository ----------------------
case "$DEPENDENCY_MODE" in
    full)
        bash "$SCRIPT_DIR/dependencies/install-dependencies.sh" --full
        ;;
    required|minimal)
        bash "$SCRIPT_DIR/dependencies/install-dependencies.sh" --required
        ;;
    with-optional|optional)
        bash "$SCRIPT_DIR/dependencies/install-dependencies.sh" --with-optional
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
    echo "  bash $SCRIPT_DIR/dependencies/install-dependencies.sh --required" >&2
    exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
    echo "Milelik's installer needs curl." >&2
    echo "Install the repository's dependencies with:" >&2
    echo "  bash $SCRIPT_DIR/dependencies/install-dependencies.sh --required" >&2
    exit 1
fi

# --- 2. Model runtime/model (unless an existing local server was chosen) -----
USE_OLLAMA=1
if [ "$MODEL_SOURCE" = "server" ] || [ "$BACKEND" = "openai" ]; then
    USE_OLLAMA=0
fi

if [ "$USE_OLLAMA" = "1" ]; then
    if ! command -v ollama >/dev/null 2>&1; then
        echo "Installing Ollama…"
        curl -fsSL https://ollama.com/install.sh | sh
    fi

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

    MODEL="$(python3 - "$SCRIPT_DIR/milelik.py" "$MODEL" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("milelik", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(m.resolve_ollama_model_name(sys.argv[2]))
PY
)"

    GGUF_CANDIDATE="${GGUF:-}"
    if [ -z "$GGUF_CANDIDATE" ] && { [ "$MODEL_SOURCE" = "gguf" ] || \
         { [ "$MODEL_SOURCE" = "auto" ] && \
           { [ "$MODEL_WAS_SET" = "0" ] || [ "$MODEL" = "$BUNDLED_OLLAMA_MODEL" ] || \
             [ "$MODEL" = "$TARGET_OLLAMA_MODEL" ]; }; }; }; then
        GGUF_PATTERN='*smollm2*135m*q4_1*.gguf'
        if [ "$MODEL" = "$TARGET_OLLAMA_MODEL" ]; then
            GGUF_PATTERN='*qwen2.5-coder*7b*abliterat*q4_k_m*.gguf'
        fi
        for d in "$MODELS_DIR" "$SCRIPT_DIR/models" "$APP_DIR/models" \
                 "$PWD/models" "$SCRIPT_DIR" "$PWD" "$HOME/Downloads" \
                 "$HOME/models" "$HOME/Models" "$HOME/.lmstudio/models" \
                 "$HOME/.cache/lm-studio/models"; do
            [ -d "$d" ] || continue
            GGUF_CANDIDATE="$(find "$d" -maxdepth 6 \( -type f -o -type l \) \
                -iname "$GGUF_PATTERN" -print -quit \
                2>/dev/null || true)"
            [ -n "$GGUF_CANDIDATE" ] && break
        done
    fi

    if [ -n "$GGUF_CANDIDATE" ] && [ -f "$GGUF_CANDIDATE" ]; then
        [ -z "$MODEL_FILE" ] && MODEL_FILE="$GGUF_CANDIDATE"
        echo "Found your GGUF: $GGUF_CANDIDATE"
        echo "Importing it (no download)…"
        MODEL="$(python3 - "$SCRIPT_DIR/milelik.py" "$GGUF_CANDIDATE" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("milelik", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(m.ollama_name_for_model_file(sys.argv[2]))
PY
)"
        OLLAMA_NAME="$MODEL" bash "$SCRIPT_DIR/setup-model.sh" "$GGUF_CANDIDATE"
        BACKEND="ollama"
    elif [ "$MODEL_SOURCE" = "gguf" ]; then
        echo "No GGUF file was found to import." >&2
        echo "Run the installer again and choose the bundled model, or use:" >&2
        echo "  GGUF=/path/to/model.gguf ./install.sh" >&2
        exit 1
    else
        if [ "$MODEL" = "$BUNDLED_OLLAMA_MODEL" ]; then
            echo "Bundled GGUF was not found; downloading SmolLM2 135M from Ollama…"
            MODEL="smollm2:135m"
        else
            echo "Downloading model '$MODEL' (first time only)…"
        fi
        ollama pull "$MODEL"
    fi
else
    echo "Using your existing local model server; not installing Ollama or downloading a model copy."
    BACKEND="openai"
    if [ -z "$MODEL_HOST" ]; then
        MODEL_HOST="http://127.0.0.1:1234"
    fi
fi

# --- 3. Milelik, front end and launchers --------------------------------------
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$WORKSPACE_DIR"
install -m 0755 "$SCRIPT_DIR/milelik.py" "$APP_DIR/milelik.py"
if [ "$FRONTEND_ENABLED" = "1" ]; then
    install -m 0755 "$SCRIPT_DIR/milelik_web.py" "$APP_DIR/milelik_web.py"
else
    rm -f "$APP_DIR/milelik_web.py"
fi
for extra in setup-model.sh setup_milelik.py Modelfile.qwen2.5-coder-abliterated; do
    if [ -f "$SCRIPT_DIR/$extra" ]; then
        install -m 0755 "$SCRIPT_DIR/$extra" "$APP_DIR/$extra"
    fi
done

rm -rf "$APP_DIR/dependencies"
cp -a "$SCRIPT_DIR/dependencies" "$APP_DIR/dependencies"
chmod +x "$APP_DIR/dependencies/install-dependencies.sh"
rm -rf "$APP_DIR/frontend"
if [ "$FRONTEND_ENABLED" = "1" ]; then
    cp -a "$SCRIPT_DIR/frontend" "$APP_DIR/frontend"
fi

# Milelik's installed models/ folder. Never delete this folder during an
# upgrade: it may contain the user's multi-gigabyte model files. Repository
# model files are hard-linked (or symlinked) rather than copied, to avoid
# duplicating gigabytes.
mkdir -p "$APP_DIR/models"
for extra in README.md .gitkeep LICENSE-SmolLM2-Apache-2.0.txt; do
    if [ -f "$SCRIPT_DIR/models/$extra" ]; then
        install -m 0644 "$SCRIPT_DIR/models/$extra" "$APP_DIR/models/$extra"
    fi
done
if [ -d "$SCRIPT_DIR/models" ]; then
    while IFS= read -r -d '' source_model; do
        relative_model="${source_model#$SCRIPT_DIR/models/}"
        installed_model="$APP_DIR/models/$relative_model"
        if [ -e "$installed_model" ] || [ -L "$installed_model" ]; then
            continue
        fi
        mkdir -p "$(dirname "$installed_model")"
        ln "$source_model" "$installed_model" 2>/dev/null || \
            ln -s "$source_model" "$installed_model" 2>/dev/null || \
            cp -a "$source_model" "$installed_model"
    done < <(find "$SCRIPT_DIR/models" \( -type f -o -type l \) \
        \( -iname '*.gguf' -o -iname '*.ggml' -o -iname '*.bin' \
           -o -iname '*.safetensors' -o -iname '*.onnx' \
           -o -iname '*.pt' -o -iname '*.pth' \) -print0 2>/dev/null)
fi

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
if [ "$FRONTEND_ENABLED" = "1" ]; then
    cat > "$BIN_DIR/milelik-web" <<EOF
#!/usr/bin/env bash
exec python3 "$APP_DIR/milelik_web.py" "\$@"
EOF
    chmod +x "$BIN_DIR/milelik-web"
else
    rm -f "$BIN_DIR/milelik-web"
fi
if [ -f "$APP_DIR/setup_milelik.py" ]; then
    cat > "$BIN_DIR/milelik-setup" <<EOF
#!/usr/bin/env bash
exec python3 "$APP_DIR/setup_milelik.py" "\$@"
EOF
    chmod +x "$BIN_DIR/milelik-setup"
else
    rm -f "$BIN_DIR/milelik-setup"
fi

if [ -f "$SCRIPT_DIR/milelik.desktop" ]; then
    install -m 0644 "$SCRIPT_DIR/milelik.desktop" "$DESKTOP_DIR/milelik.desktop"
fi
if [ "$FRONTEND_ENABLED" = "1" ] && [ -f "$SCRIPT_DIR/milelik-workspace.desktop" ]; then
    install -m 0644 "$SCRIPT_DIR/milelik-workspace.desktop" "$DESKTOP_DIR/milelik-workspace.desktop"
else
    rm -f "$DESKTOP_DIR/milelik-workspace.desktop"
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo
       echo "Warning: $BIN_DIR is not on your PATH in this shell."
       echo "   linux Mint adds it automatically at login - open a NEW terminal,"
       echo "   or run:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# Save every answer so the CLI and front end share the same preferences.
MILELIK_INSTALL_MODEL="$MODEL" \
MILELIK_INSTALL_BACKEND="$BACKEND" \
MILELIK_INSTALL_HOST="$MODEL_HOST" \
MILELIK_INSTALL_MODELS_DIR="$MODELS_DIR" \
MILELIK_INSTALL_MODEL_FILE="$MODEL_FILE" \
MILELIK_INSTALL_MODEL_DISCOVERY="$MODEL_DISCOVERY" \
MILELIK_INSTALL_MODEL_SOURCE="$MODEL_SOURCE" \
MILELIK_INSTALL_DEPENDENCY_MODE="$DEPENDENCY_MODE" \
MILELIK_INSTALL_PERFORMANCE_PROFILE="$PERFORMANCE_PROFILE" \
MILELIK_INSTALL_CTX="$CTX" \
MILELIK_INSTALL_MAX_STEPS="$MAX_STEPS" \
MILELIK_INSTALL_CMD_TIMEOUT="$CMD_TIMEOUT" \
MILELIK_INSTALL_AUTO="$AUTO_APPROVE" \
MILELIK_INSTALL_WORKSPACE="$WORKSPACE_DIR" \
MILELIK_INSTALL_WEB_HOST="$WEB_HOST" \
MILELIK_INSTALL_WEB_PORT="$WEB_PORT" \
MILELIK_INSTALL_OPEN_BROWSER="$OPEN_BROWSER" \
MILELIK_INSTALL_FRONTEND_ENABLED="$FRONTEND_ENABLED" \
MILELIK_INSTALL_UI_THEME="$UI_THEME" \
MILELIK_INSTALL_UI_DEFAULT_VIEW="$UI_DEFAULT_VIEW" \
python3 - <<'PY'
import json, os, pathlib

p = (pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config"))
     / "milelik" / "config.json")
try:
    cfg = json.loads(p.read_text())
    if not isinstance(cfg, dict):
        cfg = {}
except Exception:
    cfg = {}

def env(name, default=""):
    return os.environ.get(name, default)

def integer(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def boolean(name):
    return os.environ.get(name) == "1"

cfg.update({
    "model": env("MILELIK_INSTALL_MODEL"),
    "backend": env("MILELIK_INSTALL_BACKEND", "auto"),
    "host": env("MILELIK_INSTALL_HOST"),
    "models_dir": env("MILELIK_INSTALL_MODELS_DIR"),
    "model_file": env("MILELIK_INSTALL_MODEL_FILE"),
    "model_discovery": boolean("MILELIK_INSTALL_MODEL_DISCOVERY"),
    "model_source": env("MILELIK_INSTALL_MODEL_SOURCE", "auto"),
    "dependency_mode": env("MILELIK_INSTALL_DEPENDENCY_MODE", "full"),
    "performance_profile": env("MILELIK_INSTALL_PERFORMANCE_PROFILE", "balanced"),
    "ctx": integer("MILELIK_INSTALL_CTX", 8192),
    "max_steps": integer("MILELIK_INSTALL_MAX_STEPS", 12),
    "cmd_timeout": integer("MILELIK_INSTALL_CMD_TIMEOUT", 60),
    "auto": boolean("MILELIK_INSTALL_AUTO"),
    "workspace": env("MILELIK_INSTALL_WORKSPACE"),
    "web_host": env("MILELIK_INSTALL_WEB_HOST", "127.0.0.1"),
    "web_port": integer("MILELIK_INSTALL_WEB_PORT", 8765),
    "open_browser": boolean("MILELIK_INSTALL_OPEN_BROWSER"),
    "frontend_enabled": boolean("MILELIK_INSTALL_FRONTEND_ENABLED"),
    "ui_theme": env("MILELIK_INSTALL_UI_THEME", "system"),
    "ui_default_view": env("MILELIK_INSTALL_UI_DEFAULT_VIEW", "chat"),
})
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, indent=2))
print(f"Saved your Milelik preferences in {p}")
PY

echo
if [ "$SKIP_CHECK" = "1" ]; then
    echo "Skipping final check (--skip-check)."
else
    python3 "$APP_DIR/milelik.py" --check || true
fi

if [ "$GITHUB_LOGIN" = "1" ]; then
    echo
    if command -v gh >/dev/null 2>&1; then
        echo "Starting GitHub login…"
        gh auth login || echo "GitHub login was not completed; run 'gh auth login' whenever you're ready."
    else
        echo "GitHub CLI (gh) is not available, so login was skipped."
    fi
fi

case "$LAUNCH_AFTER_INSTALL" in
    frontend)
        if [ "$FRONTEND_ENABLED" = "1" ]; then
            echo
            echo "Opening the Milelik front end in the background…"
            if [ "$OPEN_BROWSER" = "1" ]; then
                nohup python3 "$APP_DIR/milelik.py" --gui --workspace-dir "$WORKSPACE_DIR" \
                    --web-host "$WEB_HOST" --web-port "$WEB_PORT" --open-browser \
                    >"$APP_DIR/frontend.log" 2>&1 &
            else
                nohup python3 "$APP_DIR/milelik.py" --gui --workspace-dir "$WORKSPACE_DIR" \
                    --web-host "$WEB_HOST" --web-port "$WEB_PORT" --no-open-browser \
                    >"$APP_DIR/frontend.log" 2>&1 &
            fi
            echo "Front-end log/API token: $APP_DIR/frontend.log"
        else
            echo "Front-end launch requested, but the front end was disabled in preferences."
        fi
        ;;
    cli|none)
        ;;
esac

echo
echo "Done. Open Milelik your way:"
if [ "$FRONTEND_ENABLED" = "1" ]; then
    echo "   Front end + workspace + APIs:  milelik --gui"
    echo "   (also available as the “Milelik Workspace” app and: milelik-web)"
fi
echo "   Chat in a terminal:            milelik --interactive"
echo "   One task:                      milelik \"what is my local IP, public IP, and disk space?\""
echo "   Check my setup:                milelik --check"
echo "   Your workspace:                $WORKSPACE_DIR"
echo "   GitHub account (once):         gh auth login"
