# Milelik models folder

Put local AI model files in this folder. When Milelik starts, it scans this
folder recursively and chooses from the model files it finds.

## What to put here

The runnable format for Milelik's Ollama, LM Studio and llama.cpp backends is
**GGUF**:

```text
models/
├── Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf
└── another-model/
    └── another-model-q4_k_m.gguf
```

Other common model files (`.safetensors`, `.onnx`, `.pt`, `.pth`, `.bin` and
`.ggml`) are listed in the inventory too, but Milelik cannot serve those
formats directly through Ollama/LM Studio/llama.cpp, so they are marked
unsupported in the chooser.

Split GGUFs are supported: keep every `…-00001-of-000NN.gguf` shard in the
same folder and select the model once; Milelik uses the first shard and links
the rest beside it for Ollama import.

## How selection works

At startup Milelik looks in:

1. a custom `models_dir` from `--models-dir`, Preferences/config or
   `MILELIK_MODELS_DIR`, if set;
2. the `models/` folder beside the running Milelik program (this folder when
   running from the repository; `~/.local/share/milelik/models/` after
   installation); and
3. `./models/` in the directory you launched Milelik from.

It prefers a remembered selection, then a file matching the configured model,
then Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M, then the first supported
GGUF alphabetically. Use `--choose-model` to force the terminal chooser; an
interactive first run with multiple supported GGUFs and no remembered file
also asks. The browser front end has the same list in
**Preferences → Model file from `models/`**.

Useful commands:

```bash
milelik --list-models       # show every model file found
milelik --choose-model      # force the startup chooser
milelik --model-file models/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf
milelik --no-model-scan     # ignore the models/ folders for this run
```

## Git note

Model weights are normally several gigabytes. This repository's `.gitignore`
ignores model binaries inside `models/`, so dropping a GGUF here makes it
available to your local Milelik without accidentally committing gigabytes to
Git. Remove or adjust those ignore rules only if you deliberately use Git LFS
or another large-file workflow.
