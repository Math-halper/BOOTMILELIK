# Milelik engine — remade local runtime (no Ollama needed)

This folder is a from-parts remake of the Ollama runtime, sized for the
repository: a real CPU inference server plus an Ollama-compatible front,
all vendored here. A brand-new linux Mint machine with no internet runs
Milelik straight from this folder.

| File | Purpose |
| --- | --- |
| [`llama-server`](llama-server) | CPU-only inference server binary, x86-64, built from [`ggml-org/llama.cpp`](https://github.com/ggml-org/llama.cpp) (see [`ENGINE.json`](ENGINE.json) for the exact tag, commit and build flags) |
| [`ollama_compat.py`](ollama_compat.py) | Stdlib-only Ollama-compatible front: `GET /api/tags` + `POST /api/chat` translated to the server |
| [`serve.py`](serve.py) | Launcher: boots the server on 127.0.0.1:8080 and the front on 127.0.0.1:11434 |
| [`ENGINE.json`](ENGINE.json) | Provenance manifest: upstream, commit, build flags, binary hash |

## Run it

```bash
# Terminal 1: boot the engine (loads models/smollm2-135m-instruct-q4_1.gguf)
python3 engine/serve.py

# Terminal 2: Milelik talks to it like Ollama — zero configuration
milelik "check my disk space, memory, and local IP"
```

Why zero configuration: Milelik's default host is already
`http://127.0.0.1:11434`, and the front answers the exact two calls its
Ollama backend uses (`/api/tags` for detection, `/api/chat` with
`stream: false` for replies, including the bundled `<tool_call>`
text protocol, which is parsed from the reply content as usual).

Other forms:

```bash
python3 engine/serve.py --check            # end-to-end self test on ports 18080/11435
python3 engine/serve.py --llama-only       # OpenAI-style API on :8080 only (then: milelik --backend openai)
python3 engine/serve.py --model ~/my.gguf  # serve a different GGUF (e.g. Qwen2.5-Coder)
python3 engine/serve.py --ctx 4096         # smaller context for small-RAM machines
milelik-engine                             # same launcher, after `python3 install_milelik.py`
```

## What it needs (fresh Mint)

- `python3` (preinstalled) and the `llama-server` binary in this folder.
- The binary is a portable CPU build: no CUDA, no OpenMP, no curl. It
  needs only the base C/C++ runtime libraries every Mint ships
  (`libc`, `libstdc++`, `libm`, `libgcc`). `ENGINE.json` records the
  exact linked set from the build machine.
- The build machine ran glibc 2.36 (Debian 12); the binary runs on any
  system with glibc 2.36 or newer — that covers linux Mint 22.x
  (Ubuntu 24.04 base) out of the box. On older Mint 21.x rebuild from
  the pinned source below (one command, ~10 minutes).

## Rebuild it (bit-audit path)

Every value needed to reproduce the binary is pinned in `ENGINE.json`:

```bash
# needs: a C++17 compiler, make, cmake, internet (one time, on any PC)
TAG=$(python3 -c "import json; print(json.load(open('engine/ENGINE.json'))['tag'])")
curl -sL -o /tmp/llama.cpp.tar.gz \
  https://codeload.github.com/ggml-org/llama.cpp/tar.gz/refs/tags/$TAG
mkdir -p /tmp/llama-rebuild && tar xzf /tmp/llama.cpp.tar.gz -C /tmp/llama-rebuild
cd /tmp/llama-rebuild/llama.cpp-*/
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF \
  -DGGML_OPENMP=OFF -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF
cmake --build build --target llama-server -j"$(nproc)"
strip build/bin/llama-server
cp build/bin/llama-server /path/to/BOOTMILELIK/engine/llama-server
```

then refresh `binary.size_bytes`/`binary.sha256` in `ENGINE.json`
(`sha256sum engine/llama-server`) so `tests/test_engine.py` keeps
proving the vendored bytes.

## Upgrade it

Pick a newer [`llama.cpp` tag](https://github.com/ggml-org/llama.cpp/tags),
rebuild as above, update `tag`/`commit`/`binary` in `ENGINE.json`, and run
`python3 engine/serve.py --check` plus the repo test suite.

## Limits (honest)

- CPU only: no GPU acceleration. The bundled 135M model is fast on any
  modern CPU; a 7B model answers slower than on Ollama+CUDA but works.
- The front implements exactly what Milelik uses (`/api/tags`,
  `/api/chat` non-streaming, `/api/version`, `/`). It is not a full
  Ollama clone: no `ollama pull`/`create`/`push`, no streaming.
- One model at a time: whichever `--model` file was given at boot
  (default: the bundled SmolLM2).
