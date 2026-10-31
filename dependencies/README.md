# Milelik dependencies

Milelik's complete dependency specification lives in this repository.
There is no separate list that users must copy from the documentation
and install by hand.

## What is included

| Dependency type | Included in the repository as | Installed/used by |
|---|---|---|
| Python packages | None — `requirements.txt` and `pyproject.toml` declare zero third-party packages | Not applicable; Python 3.10+ standard library only |
| Core system packages | [`apt-required.txt`](apt-required.txt) | [`install_dependencies.py`](install_dependencies.py) |
| VLC/media, desktop, LibreOffice, Git/GitHub, Node/npm and Python project packages | [`apt-tools.txt`](apt-tools.txt) | `install_dependencies.py` (the default full mode) |
| Extra fallback programs | [`apt-optional.txt`](apt-optional.txt) | `install_dependencies.py --with-optional` |
| Ollama runtime | Python bootstrap logic in [`../install_milelik.py`](../install_milelik.py) | The main Python installer |
| Qwen2.5-Coder model | Model identity/template/setup logic in [`../milelik.py`](../milelik.py), [`../setup_model.py`](../setup_model.py) and [`../Modelfile.qwen2.5-coder-abliterated`](../Modelfile.qwen2.5-coder-abliterated), plus the user-supplied [`../models/`](../models/) folder read at startup | The main installer / Milelik startup discovery, from an existing GGUF or the Ollama library |
| LM Studio / llama.cpp | Optional alternative model servers detected by `milelik.py` | The user, only if they prefer them to Ollama |

## Install or verify the system packages

The main installer does this automatically. To run the bundled
dependency installer directly:

```bash
# Full set: required packages + app/VLC/desktop/GitHub/LibreOffice tools
python3 dependencies/install_dependencies.py

# Core runtime only
python3 dependencies/install_dependencies.py --required

# Full set plus optional fallback screenshot/wallpaper programs
python3 dependencies/install_dependencies.py --with-optional

# Verify packages and commands without changing anything
python3 dependencies/install_dependencies.py --check

# Show the resolved package list
python3 dependencies/install_dependencies.py --list
```

`python3 install_milelik.py --yes` accepts:

```bash
DEPENDENCY_MODE=full python3 install_milelik.py --yes           # default
DEPENDENCY_MODE=with-optional python3 install_milelik.py --yes  # also install apt-optional.txt
DEPENDENCY_MODE=required python3 install_milelik.py --yes       # core packages only
DEPENDENCY_MODE=none python3 install_milelik.py --yes           # skip apt; manage packages yourself
```

## Why apt packages are declared rather than copied as binaries

The repository contains the canonical manifests and installation logic.
Linux packages such as VLC, FFmpeg, Git, `gh` and LibreOffice are
architecture- and distribution-release-specific, have many transitive
shared-library dependencies, and receive security updates through
linux Mint. Apt resolves the correct signed packages and transitive
dependencies for the installed Mint release; checking hundreds of
megabytes of distribution binaries into Git would be brittle and would
go stale.

The same principle applies to the Ollama runtime and large optional
model weights: Milelik's Python installer downloads/imports them from
their upstream distribution points, or imports the user's existing
`Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf`, rather than storing
those artifacts in Git. The small bundled SmolLM2 GGUF is the exception
and is already in `models/`.
