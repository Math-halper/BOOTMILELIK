# Milelik — BOOTMILELIK

A **fast, local AI desktop companion for linux Mint**. It now ships with
**SmolLM2-135M-Instruct-Q4_1** in `models/` as its small default brain, while
larger local GGUFs such as Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M remain
supported upgrades. The AI model runs 100% on your own computer — via
[Ollama](https://ollama.com), or from a GGUF loaded in **LM Studio** /
**llama.cpp** — with no cloud and no accounts. It can **run shell commands**,
**use the network**, **open and control desktop apps** (VLC, browsers, files,
terminal, LibreOffice…), and work with **Git and GitHub** (repos, issues,
pull requests, CI, releases) to actually get things done. The browser front
end uses Milelik's original black-and-white glitch companion sprite, sharp
speech plates and technical black/white styling inspired by the desktop
companion look of Capri Desktop v0.7.4.

It does *not* control your mouse/screen by simulating clicks, and it asks
your permission before running commands or making changes — see
[Safety](#-safety) below.

```
Browser ──► Milelik front end/workspace/APIs ──► milelik agent ──► local model
Terminal ─────────────────────────────────────►    │        (Ollama / LM Studio / llama.cpp)
                                                   ├──► bash commands + files (with your OK)
                                                   ├──► workspace editor + Python/Bash/JS runner
                                                   ├──► local coding / maths / utility APIs
                                                   ├──► fetch_url / web_search ──► internet
                                                   ├──► apps: VLC, browser, files, LibreOffice…
                                                   └──► git_repo / github_* ──► GitHub
```

Pure Python standard library, **zero pip dependencies**. At startup Milelik
reads every AI model file in its `models/` folder and chooses a runnable GGUF
from what it finds; the bundled SmolLM2 file is preferred on a fresh setup.
The browser setup page asks for your model, safety, workspace and front-end
preferences. Every system dependency is declared in this repository under
[`dependencies/`](dependencies/) and installed automatically, so there are no
extra dependency commands to run by hand.

## The bundled default model: SmolLM2-135M-Instruct-Q4_1

Milelik includes this model in the repository:

```text
models/smollm2-135m-instruct-q4_1.gguf
```

| Where | Name |
|---|---|
| Bundled GGUF | `smollm2-135m-instruct-q4_1.gguf` (98,362,432 bytes) |
| After Ollama import | `milelik-smollm2:135m` |
| Ollama library fallback | `smollm2:135m` |
| LM Studio / llama.cpp | `smollm2-135m-instruct-q4_1` |
| SHA-256 | `b179c9523d0e6a0f98a330c7562b682750a6f8c8c15e5bc70ea373728110db53` |

The weights are HuggingFaceTB's SmolLM2-135M-Instruct under Apache-2.0 and
were supplied from `Math-halper/mox`; the full license text is in
[`models/LICENSE-SmolLM2-Apache-2.0.txt`](models/LICENSE-SmolLM2-Apache-2.0.txt).
Milelik's code remains MIT-licensed; the model weights keep their own terms. This tiny model is fast and
needs very little RAM, but it is much less capable than a 7B coder: give it
one clear, short task at a time. Milelik automatically uses tighter sampling
and its strict one-tool-at-a-time prompt for the bundled model.

### Optional larger model: Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M

The former default remains fully supported and is recommended for harder
coding and multi-step tool work. Milelik recognises all of its names:

| Where | Name |
|---|---|
| GGUF file | `Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf` |
| Ollama library | `huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M` (note: "abliterate") |
| LM Studio | `qwen2.5-coder-7b-instruct-abliterated-q4_k_m` |
| Hugging Face | `bartowski/Qwen2.5-Coder-7B-Instruct-abliterated-GGUF:Q4_K_M` |
| After `./setup-model.sh` | `milelik-coder:7b` (your GGUF imported into Ollama) |

### Put model files in `models/`

Milelik has a dedicated [`models/`](models/) folder. Put one or more GGUF
files there (subfolders are fine), and when Milelik starts it inventories all
of them and chooses from those files:

```bash
ls models/                  # bundled SmolLM2 is already there
cp ~/Downloads/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf models/  # optional
milelik --list-models       # see every model file Milelik can see
milelik --choose-model -i   # pick a file interactively, then chat
milelik --model-file smollm2-135m-instruct-q4_1.gguf "check my disk"
```

The installed copy also has `~/.local/share/milelik/models/`; Milelik checks
that folder, a `./models/` folder in your launch directory, and an optional
`--models-dir`/`models_dir` override. GGUF is the runnable format for Ollama,
LM Studio and llama.cpp. SafeTensors, ONNX, PyTorch and generic `.bin` files
are listed in the inventory but marked non-runnable rather than being fed to
the wrong runtime. Split GGUF shards (`...-00001-of-00002.gguf`, etc.) are
shown as one model. If an Ollama model has not been imported yet, selecting a
local GGUF imports it automatically; Qwen2.5-Coder files receive Milelik's
Qwen template, while other GGUFs keep a minimal template so Ollama can use the
file's own metadata. Additional model binaries inside `models/` are ignored by
Git; the bundled SmolLM2 GGUF is the one intentional tracked exception — see
[`models/README.md`](models/README.md).

Three ways to run it — use whichever matches what you already have:

**1. Ollama (default)** — `./install.sh` imports the bundled GGUF for you, or
manually:
```bash
bash setup-model.sh
milelik "check my disk space, memory, and local IP"
```
You can also pass the bundled model by any of its names — it is translated:
```bash
milelik --model SmolLM2-135M-Instruct-Q4_1 "check my disk"
milelik --model-file smollm2-135m-instruct-q4_1.gguf "check my disk"
```

**2. You already have another GGUF file** — import it instead of downloading
weights again:
```bash
bash setup-model.sh ~/Downloads/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf
# or, equivalently:
milelik --import-gguf ~/Downloads/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf
milelik "check my disk"   # now uses that import (milelik-coder:7b for Qwen)
```
`bash setup-model.sh` with no arguments searches Milelik's `models/` folders
for the bundled SmolLM2 file. Qwen2.5-Coder imports use
[`Modelfile.qwen2.5-coder-abliterated`](Modelfile.qwen2.5-coder-abliterated) —
importing that raw GGUF *without* its template is the usual reason Qwen tool
calls come out as rambling text or the model never stops talking.

**3. It's loaded in LM Studio or llama.cpp** — no Ollama copy needed at all:
```bash
# LM Studio: load models/smollm2-135m-instruct-q4_1.gguf, then start Server
milelik --check        # should say the bundled model was found
milelik "check my disk"

# llama.cpp:
llama-server -m models/smollm2-135m-instruct-q4_1.gguf \
    --host 127.0.0.1 --port 8080 -c 8192 --jinja
milelik "check my disk"
```
Milelik auto-detects whichever of Ollama (11434), LM Studio (1234) or
llama.cpp (8080) is already serving the model. Force one with
`--backend ollama|openai` and/or `--host`.

Other models still work too — anything in Ollama or on an OpenAI-compatible
local server, via `--model`. Bundled SmolLM2 gets conservative low-temperature
sampling and the strict one-tool prompt block; Qwen2.5-Coder keeps its
specialised sampling (temperature 0.1, top_p 0.8, top_k 20,
repeat_penalty 1.05), one-tool-at-a-time loop and `<tool_call>` parsing.

## Install (linux Mint)

```bash
git clone https://github.com/Math-halper/BOOTMILELIK.git
cd BOOTMILELIK
python3 setup_milelik.py
```

That Python setup script is the easiest way to start. On a desktop it opens
Milelik's **browser setup page** instead of asking setup questions in the
terminal. The local page lets you choose the model, dependencies, safety,
workspace, front-end/theme and launch options; then it runs the bundled
installer and opens the Milelik front end. The terminal only remains the
place linux may ask for your sudo password for system packages. Use
`--terminal-setup` for the old terminal questions or `--yes` for unattended
defaults.

> **Do not run the setup script with `sudo`.** Run
> `python3 setup_milelik.py` as your normal user; the installer asks for sudo
> only when it needs system packages or services. If
> `./setup_milelik.py` says \"Permission denied\", the `python3` form above
> bypasses the file's executable bit. `sudo` cannot bypass a drive mounted
> with `noexec`, so keep the clone in your home folder rather than on some
> USB/Windows drives if that happens.

Useful forms include:

```bash
python3 setup_milelik.py                 # open browser setup, then the front end
python3 setup_milelik.py --browser-setup # force the browser setup page
python3 setup_milelik.py --terminal-setup # use the old terminal questions
python3 setup_milelik.py --yes           # default setup, then open the front end
python3 setup_milelik.py --run-only      # skip installation; run from this folder
python3 setup_milelik.py --run-only --cli
python3 setup_milelik.py --install-only
python3 setup_milelik.py --list-models
python3 setup_milelik.py --check
python3 setup_milelik.py --dry-run       # show the commands without running them
```

You can also run `bash install.sh` directly. With no options on a desktop it
delegates to the same **browser setup page**. In the browser you can choose:

1. Dependency set (full/core/optional/none)
2. Where the model comes from: the bundled SmolLM2 GGUF, your existing GGUF,
   a model already loaded in LM Studio/llama.cpp, or another Ollama model —
   plus which `models/` folder to scan at startup and an optional preferred
   model file
3. Fast / balanced / quality / custom performance settings
4. Ask-before-acting or auto-approve ordinary actions
5. Browser front end, workspace folder, port, theme and opening page
6. Optional GitHub login
7. What to open when installation finishes

When you submit the page, Milelik saves the answers to
`~/.config/milelik/config.json`, installs **all declared dependencies**, sets
up the bundled **SmolLM2-135M-Instruct-Q4_1** unless you selected another
model/server, and adds both **Milelik** and **Milelik Workspace** to
your app menu.

```bash
bash install.sh              # open browser setup on a desktop
bash install.sh --browser-setup # force browser setup
bash install.sh --wizard     # force the old terminal questions
bash install.sh --yes        # no questions; sensible defaults
bash install.sh --help       # every installer option
```

The system packages live in this repository in
[`dependencies/apt-required.txt`](dependencies/apt-required.txt) (core),
[`dependencies/apt-tools.txt`](dependencies/apt-tools.txt) (VLC/media,
desktop, LibreOffice, Git/GitHub, Node/npm and Python project tools) and
[`dependencies/apt-optional.txt`](dependencies/apt-optional.txt) (fallbacks).
`./install.sh` runs
[`dependencies/install-dependencies.sh`](dependencies/install-dependencies.sh)
for you. See [`dependencies/README.md`](dependencies/README.md) for the map.

If you installed without launching Milelik, open a **new terminal** and try
the front end or CLI:

```bash
milelik --gui          # browser chat + workspace/code runner + local APIs
milelik "what is my local IP, public IP, and how much disk space is free?"
milelik --interactive
```

**Already have the GGUF somewhere else?** Point the installer at it — no
download, and no second copy of the weights outside Ollama's store:

```bash
python3 setup_milelik.py --gguf /path/to/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf
# or:
GGUF=/path/to/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf ./install.sh
```

**Want a different model anyway?** Any Ollama model works:

```bash
MODEL=qwen3:4b ./install.sh    # smaller/faster, weaker at multi-step tasks
MODEL=qwen3:14b ./install.sh   # smarter, slower, needs more RAM/VRAM
```

## Front end, workspace and local APIs

Start Milelik's browser app with:

```bash
milelik --gui
# equivalent commands:
milelik-web
milelik --gui --web-port 8766 --workspace-dir ~/Projects
```

It runs locally at `http://127.0.0.1:8765` by default and opens your browser.
The terminal prints a one-time token link; that sets a browser cookie for the
token-protected APIs. Keep the token private because the workspace APIs can
run code on your device.

The front end has four pages:

- **Chat** — the full Milelik agent with quick prompts, its 26 tools and
  browser approval dialogs (dangerous actions still require typing `YES`).
- **Workspace** — a file tree and editor on your device, Python/Bash/
  JavaScript file runner, stdin box, syntax check, workspace terminal,
  suggested project commands, downloads, and **Clone from GitHub**. Clone a
  repository directly into the workspace instead of downloading a ZIP in a
  browser, extracting it elsewhere and hunting for a terminal.
- **APIs** — interactive local APIs for coding, mathematics and utilities,
  plus an endpoint catalogue and generic API tester.
- **Preferences** — the same model/backend/performance/safety/workspace/theme
  preferences asked by the installer, saved for both CLI and front end. Its
  **Model file from `models/`** dropdown lists every discovered model file,
  lets you select one immediately, and includes a **Rescan models folder**
  button.

The sidebar includes Milelik's original black-and-white angular glitch
companion sprite and speech plate. All visual sprite artwork is organized as
**SVG only** in [`frontend/sprites/`](frontend/sprites/): the companion and
browser icon live there, and the front end/browser setup do not use
PNG/JPG/GIF/WebP sprite images. It idles, blinks, thinks, talks, glitches on
errors, alerts during approval prompts, and sleeps when the model backend is
unavailable.
Reduced-motion browser preferences disable its animation.

The first launch creates `~/MilelikWorkspace` (or your chosen folder) with
small Python/JavaScript examples. Workspace paths cannot escape that folder.
Code execution is deliberately local and runs as your linux user; it is meant
for code you wrote or chose to clone, not as a sandbox for untrusted code.

Local APIs include chat and all Milelik tools, workspace files/cloning, code
run/syntax/regex/encoding, safe maths evaluation, statistics, unit
conversion, quadratics, text tools, hashing, password generation and time.
Full examples and the endpoint list are in [`docs/API.md`](docs/API.md), and a
running server exposes OpenAPI at `/api/openapi.json`.

## Using it

One-off task:

```bash
milelik "find the 10 biggest folders in my home directory"
milelik "what process is using port 8080?"
milelik "check for apt updates and list them, do not install anything"
milelik "is port 22 open on 192.168.1.1? also find its hostname"
milelik "search the web for how to enable SSH on linux Mint and summarise the steps"
milelik "read ~/Documents/notes.txt and summarise it"
milelik "write a bash backup script for my Documents folder and save it to ~/backup.sh"
milelik "play ~/Videos/demo.mp4 in VLC fullscreen"
milelik "pause VLC, set its volume to 40%, and tell me what's playing"
milelik "open Firefox at github.com, open my Documents folder, and take a screenshot"
milelik "convert ~/Documents/report.odt to PDF"
milelik "in this repo: show git status, list open GitHub PRs and any failing CI"
milelik "search GitHub for linux Mint screenshot tools written in Python"
```

### Apps, VLC and desktop tools

Just ask in plain English — Milelik picks the right tool:

| Tool | What it can do |
|---|---|
| `list_apps` | List/search installed desktop apps (from `.desktop` files) |
| `open_app` | Launch an app by name/id (VLC, Firefox, Files, Text Editor, Settings…), optionally with a file/URL |
| `open_target` | Open a file/folder/URL in its default app (`xdg-open`) or a named app |
| `open_terminal` | Open a terminal window in a folder, optionally running a command |
| `vlc` | Play/enqueue a file or stream in VLC (fullscreen/pause options), now-playing/status, play/pause/stop/next/previous, seek, volume/mute, quit |
| `media_player` | Same playback controls for any MPRIS player (Celluloid, Rhythmbox, Spotify, browsers) via `playerctl` |
| `media_info` | Duration/codecs/resolution of a local media file (`ffprobe`) |
| `desktop_control` | Notifications, screenshots, clipboard read/write, system volume, brightness, battery, lock screen, wallpaper |
| `office_document` | Open LibreOffice documents, or convert them (PDF/DOCX/ODT/TXT/CSV…) headlessly |

Examples:
```bash
milelik "what media apps do I have installed?"
milelik "play https://example.com/stream.m3u8 in VLC and set the volume to 70%"
milelik "skip to the next track and tell me what's playing now"
milelik "how long is ~/Videos/holiday.mp4 and what resolution is it?"
milelik "notify me when you're done, copy the result to my clipboard"
milelik "turn the brightness down 20% and check my battery"
```
Playback controls use `playerctl`, and both it and VLC are in the bundled
full dependency set installed by `./install.sh`. App launches, screenshots,
clipboard access, screen-lock and wallpaper changes ask for confirmation
first (unless `--yolo`).

### Git & GitHub tools

| Tool | What it can do |
|---|---|
| `git_repo` | Local Git: status, diff, log, branches, remotes; add, commit, push, pull, fetch, checkout/create branch, stash |
| `github_account` | Auth status (never shows tokens), whoami, user profiles, notifications, gists |
| `github_search` | Search repositories, code, issues/PRs, users, commits |
| `github_repo` | View/list repos, clone, create (private by default), fork — *no repo deletion, by design* |
| `github_issues` | List/view/create/comment/close/reopen issues (with labels) |
| `github_pull_requests` | List/view/checks/changed files/diff, create (incl. drafts), comment, close/reopen, merge (merge/squash/rebase — needs you to type `YES`) |
| `github_actions` | Workflows and runs: list, view, jobs, logs, rerun, cancel, manually dispatch a workflow |
| `github_releases` | List/latest/view releases with asset names and sizes; create releases (draft by default) and upload assets |

Inside a cloned repo, `repo` can be left out — Milelik uses that repo's
GitHub `origin` remote. Read-only lookups work without an account for public
repos; anything that *writes* uses the GitHub CLI if you're logged in
(`gh auth login`) or a `GITHUB_TOKEN`/`GH_TOKEN` environment variable.
**Never paste a token into the chat.**

Examples:
```bash
milelik "who am I on GitHub, and do I have any unread notifications?"
milelik "show the open issues labelled bug in Math-halper/BOOTMILELIK"
milelik "list the latest release of neovim/neovim and its download assets"
milelik "check whether the CI passed on PR #4 in this repo"
milelik "clone cli/cli into ~/src and show me its recent commits"
milelik "create a draft PR from my current branch titled 'Fix the thing'"
milelik "comment 'looks good to me' on issue #3"
```

Interactive chat (`milelik -i`) — keeps context between tasks. Inside it:

| Command | What it does |
|---|---|
| `/model SmolLM2-135M-Instruct-Q4_1` | switch model (any of its names work) |
| `/models` | list model files in `models/` and models on the current backend |
| `/yolo on` / `/yolo off` | auto-approve ordinary commands |
| `/info` | system info |
| `/clear` | forget the conversation |
| `!df -h` | run a shell command yourself |
| `/exit` | quit |

Useful flags: `milelik --help` for all of them.

```bash
milelik --gui                   # browser front end + workspace + local APIs
milelik --gui --web-port 9876 --workspace-dir ~/Projects
milelik --check                 # diagnose Ollama/LM Studio/llama.cpp, model, network
milelik --list-models           # read and list all model files in models/
milelik --choose-model -i        # choose a model file at startup
milelik --model-file Other-Model.gguf "..."
milelik --models-dir ~/AI/models --list-models
milelik --no-model-scan "..."    # use only the configured/server model
milelik --model SmolLM2-135M-Instruct-Q4_1 "..."
milelik --model Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M "..."  # optional 7B
milelik --backend openai --host http://127.0.0.1:1234 "..."   # force LM Studio
milelik --backend ollama "..."   # force Ollama
milelik --ctx 8192 "..."         # bundled SmolLM2 context (the default)
milelik --yolo "..."            # don't ask before ordinary commands
milelik -v "..."                 # show full tool output + which backend/model is used
milelik --max-steps 20 "..."    # allow longer, multi-step tasks
```

Settings live in `~/.config/milelik/config.json`. Browser setup creates
them, the front end's **Preferences** page edits them, and
`milelik --init-config` can recreate defaults. They include `model`,
`backend`, `host`, `models_dir`, `model_file`, `model_discovery`, `ctx`,
`max_steps`, `cmd_timeout`, `auto`, `workspace`, `web_host`, `web_port`,
`open_browser`, `frontend_enabled`, `ui_theme` and `ui_default_view`.
Environment variables `MILELIK_MODEL`, `MILELIK_BACKEND`, `MILELIK_HOST`,
`MILELIK_MODELS_DIR`, `MILELIK_MODEL_FILE`, `MILELIK_WORKSPACE`,
`MILELIK_WEB_HOST`, `MILELIK_WEB_PORT`, `OLLAMA_HOST`, `OPENAI_BASE_URL` and
`MILELIK_API_KEY` also work.

## Making it fast

- The bundled default is a 135M Q4_1 model (~94 MB), chosen for very fast
  loading and low RAM use. It is suitable for short local questions and
  simple one-step tool requests, not long autonomous coding jobs.
- For a major quality upgrade, add a larger GGUF (for example the supported
  7B Qwen2.5-Coder) to `models/` and choose it. A 7B Q4 needs roughly 6 GB of
  free RAM/VRAM once context is included.
- The first question after a while is slower (model loading into RAM/VRAM);
  with Ollama, Milelik keeps the model loaded for 10 minutes between calls.
- Context is 8192 tokens by default and tool output is truncated, so long
  command output doesn't slow the model down.
- Milelik runs the bundled model and Qwen **one tool call at a time** with low
  temperature — slightly more round-trips, far fewer malformed calls.

> Honest expectations: SmolLM2-135M is deliberately tiny. Ask it for one
> specific thing at a time ("what is my disk usage?", "open VLC", "check port
> 22 on this address") and expect to steer it. For multi-file coding, complex
> GitHub work or long web research, select a larger model. Milelik's command
> confirmations still matter: read proposed commands before approving them.

## Safety

Milelik can only do what *your user account* can do, and by default it can't
even do that without asking:

- **Every shell command and file write asks `[y/N]` first**, and so do app
  launches, Git write operations (commit/push/checkout…), and every GitHub
  write (creating/commenting/closing issues & PRs, rerunning CI…). GitHub
  actions run **as you**, with your `gh`/token credentials. Saying no tells
  the model to find another way — it won't just retry. Merging a PR always
  requires typing `YES`, repository deletion isn't supported at all, and
  token values are never printed or sent to the model.
- **`--yolo` only skips the ordinary prompts.** Dangerous commands
  (`rm -rf /`, `mkfs`, `dd` to a disk, `shutdown`/`reboot`,
  `curl … | bash`, fork bombs, …) always require you to type `YES`, and
  `sudo` commands always ask too. In non-interactive use (piped input),
  things needing confirmation are refused.
- Web fetches are GET-only and capped at ~2 MB; binary downloads aren't shown
  to the model.
- The front end binds to `127.0.0.1` by default and its APIs require a random
  per-launch token (browser cookie or `X-Milelik-Token` header). Workspace
  file APIs reject paths outside the chosen workspace. The code runner is
  still intentionally *your* code running as *your* user, so do not expose it
  to an untrusted network or run cloned code you have not inspected.
- Everything the model sees stays on your machine — the only network traffic
  is your local model server on `localhost` plus whatever pages/searches
  *you* asked it to do.

Still: you're letting an AI run commands. Don't start it in a folder full of
irreplaceable files with `--yolo` on day one, and read commands before you
approve them until you trust your setup.

## How it works

`milelik.py` is an agent loop around your local model server — Ollama's
`/api/chat`, or `/v1/chat/completions` for LM Studio / llama.cpp
(auto-detected; see `--backend`). `milelik_web.py` reuses that same agent and
toolbox, serves `frontend/`, routes tool confirmations to browser approval
dialogs through `Toolbox(confirm_callback=…)`, and provides the workspace and
utility APIs:

1. Your task + a system prompt go to the local model along with tool schemas.
2. The model replies with a tool call (e.g. `run_command {"command": "df -h"}`).
3. Milelik checks it for danger, asks you if needed, runs it, and sends the
   (truncated) output back to the model.
4. Repeat until the model gives a final answer (max 12 steps by default,
   repeated identical calls are cut off so it can't loop forever).

Tool calls are understood in every dialect this class of model emits:
native Ollama/OpenAI `tool_calls`, Qwen's
`<tool_call>{"name": …, "arguments": …}</tool_call>` (and the `[tool_call]`
variant), Hermes `<function=…><parameter=…>` XML, plain/fenced JSON, and even
Python-style `run_command(command="…")`. Common synonym mistakes by small
models (`bash`, `execute_command`, `cmd`, `search_query`, …) are normalised, a
malformed call gets sent back with "reply in exactly this format" (twice),
and leaked ChatML tokens (`<|im_end|>` etc.) are stripped from answers.
If a server rejects native tools outright, Milelik falls back to the text
protocol automatically. The 26 tools: `run_command`, `fetch_url`,
`web_search` (DuckDuckGo — no API key), `read_file`, `write_file`, `list_dir`,
`dns_lookup`, `check_port`, `system_info`, `list_apps`, `open_app`,
`open_target`, `open_terminal`, `vlc`, `media_player`, `media_info`,
`desktop_control`, `office_document`, `git_repo`, `github_search`,
`github_repo`, `github_issues`, `github_pull_requests`, `github_actions`,
`github_releases`, `github_account`. App tools use the apps' own CLIs/desktop
entries (argument lists, never a shell), media tools use MPRIS via
`playerctl`, and GitHub reads use the REST API while writes go through the
`gh` CLI when available.

Python dependencies are deliberately empty (`requirements.txt` and
`pyproject.toml` declare none). System dependencies are version-controlled
as apt manifests in `dependencies/`; `install-dependencies.sh` installs or
checks them, and `install.sh` copies the manifests, dependency installer,
`milelik_web.py`, `frontend/` and a preserved `models/` folder beside the
installed `milelik.py`. Repository model files are hard-linked/symlinked into
the installed `models/` folder when needed instead of being copied again. Apt
supplies the signed distribution binaries and transitive libraries for the
user's Mint release, while the browser setup front end lives in
`setup_milelik.py` and the installer/model bootstrap logic remains in
`install.sh` and `setup-model.sh`. The maths evaluator parses
a restricted Python AST rather than calling `eval`, and workspace subprocesses
run without GitHub/model API tokens in their environment.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Permission denied` when starting setup, including with `sudo` | Do not run the whole setup as root. From the repository folder run `python3 setup_milelik.py` (not `./setup_milelik.py` and not `sudo`). The `python3` form does not need the script's executable bit. If it still fails, run `findmnt -T . -o TARGET,OPTIONS`; if the options contain `noexec`, move/clone Milelik into your home folder (for example `~/BOOTMILELIK`) and run it there. `chmod +x setup_milelik.py` only fixes a missing executable bit; it cannot override `noexec`. |
| "No local model server is reachable" | `milelik --check` probes Ollama (11434), LM Studio (1234) and llama.cpp (8080) and shows which is up and where the model was found. Start one: `ollama serve`, LM Studio's server, or `llama-server …`. |
| Model not found in Ollama | Run `bash setup-model.sh` to import `models/smollm2-135m-instruct-q4_1.gguf`, or `ollama pull smollm2:135m`. Passing `--model SmolLM2-135M-Instruct-Q4_1` is fine — the name is translated to `milelik-smollm2:135m`. |
| Model not found in LM Studio | Actually load the GGUF in LM Studio (not just download it) and start the server, then `milelik --check`. Or pass the exact id LM Studio shows via `--model`. |
| My model file is not listed | Run `milelik --list-models` and check the folders it prints. Put the file in the repository `models/`, the installed `~/.local/share/milelik/models/`, `./models/` where you launch Milelik, or pass `--models-dir /path/to/models`. Incomplete downloads ending `.part` are ignored. |
| A file in `models/` says “unsupported” | Milelik's Ollama/LM Studio/llama.cpp path runs GGUF files. Convert SafeTensors/PyTorch weights to GGUF first, and keep all shards of a split GGUF together. |
| It imported my GGUF but talks nonsense / never uses tools | First check whether you are using the tiny 135M model; short, single-step prompts work much better. If it is Qwen2.5-Coder, a bare `ollama create` may have omitted its template. Re-import with `bash setup-model.sh /path/to/model.gguf` — Qwen imports apply [`Modelfile.qwen2.5-coder-abliterated`](Modelfile.qwen2.5-coder-abliterated). |
| Model describes commands instead of running them | Usually a template problem (above), or the server rejected native tools. Milelik already falls back to text `<tool_call>` calls; with llama.cpp make sure you started `llama-server` with `--jinja`. |
| Very slow answers | Smaller context (`--ctx 4096`) or close other heavy apps. The bundled 135M model should be quick; a selected 7B model will be slower and its first reply after idle includes load time. |
| `milelik: command not found` after install | Open a new terminal (Mint adds `~/.local/bin` to PATH at login), or `export PATH="$HOME/.local/bin:$PATH"`. |
| Web search returns nothing | DuckDuckGo sometimes rate-limits; wait a bit, or ask it to `fetch_url` a specific site. |
| VLC opens but pause/volume/now-playing don't work | Playback control needs MPRIS via `playerctl`, which is in the bundled full set. Run `./dependencies/install-dependencies.sh --check` (or rerun `./install.sh`) and `milelik --check`. Also make sure the same desktop session is running Milelik and VLC (don't run one over plain SSH without a D-Bus session). |
| "Application not found" | Ask it to `list_apps` first to get exact names/ids, or install the app. Flatpak/Snap apps appear once their `.desktop` files are in the standard application dirs. |
| GitHub says 401/403 / "needs authentication" | Read-only public lookups need no login; writes do. Run `gh auth login`, or set `GITHUB_TOKEN` in your shell profile. 403 can also be rate limiting — being logged in raises the limit a lot. Check with "who am I on GitHub?" / `github_account auth_status`. |
| GitHub tool says "no repository given" | Run Milelik inside the cloned repo (it reads the `origin` remote), or name it: "…in owner/repo". |
| Front end says “Connection token needed” | Open it using the complete URL printed by `milelik --gui` (it contains `?token=…` and sets a cookie), or paste the printed token into Preferences → Connection. For scripts, send `X-Milelik-Token`. |
| Front-end port is already in use | Another Milelik/server is probably already running. Use the existing browser tab, stop the old process with Ctrl+C, or start `milelik --gui --web-port 8766`. |
| Front end loads but the model does not | The workspace/APIs work without a model. For Chat, run `ollama serve` or load the GGUF in LM Studio/llama.cpp, then check model/backend/host in Preferences and run `milelik --check`. |
| JavaScript code will not run in Workspace | Install/check the full bundled dependencies (`./dependencies/install-dependencies.sh --check`) — it includes Node.js and npm. Python and Bash snippets do not need Node. |
| PR merge bounces at the prompt | By design: merging needs you to type `YES` (capitalised) in the terminal or browser approval dialog — it can't happen from `--yolo` or piped input. |

## Tests

87 tests, no running model or internet required (fake Ollama, fake LM Studio/
OpenAI-compatible and fake GitHub API servers, fake apt/`gh`/`playerctl`/VLC/
LibreOffice binaries, the real front-end and browser-setup HTTP servers on
ephemeral ports, a scripted terminal-wizard fallback run, and a real
temporary Git repo are used):

```bash
python3 -m unittest discover -s tests -v
```

## Uninstall

```bash
./uninstall.sh   # removes Milelik only; Ollama/models are left alone
```

## License

Milelik's code and its original sprite artwork are MIT-licensed. The bundled
SmolLM2-135M-Instruct weights are Apache-2.0 and retain HuggingFaceTB's model
terms; see [`models/README.md`](models/README.md). Capri Desktop inspired the
companion framing only; no Capri code, font or sprite asset is included.
