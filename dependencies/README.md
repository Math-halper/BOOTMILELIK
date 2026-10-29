# Milelik dependencies

Milelik's complete dependency specification lives in this repository.
There is no separate list that users must copy from the documentation
and install by hand.

## What is included

| Dependency type | Included in the repository as | Installed/used by |
|---|---|---|
| Python packages | None — `requirements.txt` and `pyproject.toml` declare zero third-party packages | Not applicable; Python 3.10+ standard library only |
| Core system packages | [`apt-required.txt`](apt-required.txt) | [`install-dependencies.sh`](install-dependencies.sh) |
| VLC/media, desktop, LibreOffice, Git/GitHub, Node/npm and Python project packages | [`apt-tools.txt`](apt-tools.txt) | `install-dependencies.sh` (the default full mode) |
| Extra fallback programs | [`apt-optional.txt`](apt-optional.txt) | `install-dependencies.sh --with-optional` |
| Ollama runtime | Bootstrap logic in [`../install.sh`](../install.sh) | The main installer |
| Qwen2.5-Coder model | Model identity/template/setup logic in [`../milelik.py`](../milelik.py), [`../setup-model.sh`](../setup-model.sh) and [`../Modelfile.qwen2.5-coder-abliterated`](../Modelfile.qwen2.5-coder-abliterated), plus the user-supplied [`../models/`](../models/) folder read at startup | The main installer / Milelik startup discovery, from an existing GGUF or the Ollama library |
| LM Studio / llama.cpp | Optional alternative model servers detected by `milelik.py` | The user, only if they prefer them to Ollama |

## Install or verify the system packages

The main installer does this automatically. To run the bundled
dependency installer directly:

```bash
# Full set: required packages + app/VLC/desktop/GitHub/LibreOffice tools
./dependencies/install-dependencies.sh

# Core runtime only
./dependencies/install-dependencies.sh --required

# Full set plus optional fallback screenshot/wallpaper programs
./dependencies/install-dependencies.sh --with-optional

# Verify packages and commands without changing anything
./dependencies/install-dependencies.sh --check

# Show the resolved package list
./dependencies/install-dependencies.sh --list
```

`./install.sh` accepts:

```bash
DEPENDENCY_MODE=full ./install.sh           # default
DEPENDENCY_MODE=with-optional ./install.sh  # also install apt-optional.txt
DEPENDENCY_MODE=required ./install.sh       # core packages only
DEPENDENCY_MODE=none ./install.sh           # skip apt; manage packages yourself
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

The same principle applies to the Ollama runtime and the roughly
4.7 GB model weights: the scripts in this repository install them from
their upstream distribution points, or import the user's existing
`Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf`, rather than storing
the large binary artifacts in Git.
