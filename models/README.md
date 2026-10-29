# Milelik models folder

Put local AI model files in this folder. When Milelik starts, it scans this
folder recursively and chooses from the model files it finds.

## Bundled default model

Milelik includes this small ready-to-run model:

```text
models/smollm2-135m-instruct-q4_1.gguf
```

- **Model:** SmolLM2-135M-Instruct-Q4_1 (`HuggingFaceTB`)
- **Size:** 98,362,432 bytes (93.8 MiB)
- **SHA-256:** `b179c9523d0e6a0f98a330c7562b682750a6f8c8c15e5bc70ea373728110db53`
- **Ollama import name:** `milelik-smollm2:135m`
- **License:** Apache-2.0 for the model weights; the full text is in
  [`LICENSE-SmolLM2-Apache-2.0.txt`](LICENSE-SmolLM2-Apache-2.0.txt). This
  file was supplied from the user's `Math-halper/mox` repository and is the
  one intentional model binary tracked in this repository.

On a fresh setup this is Milelik's default. It is deliberately tiny and fast;
for substantially better coding and multi-step tool use, add a larger GGUF
(such as Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M) and choose it with
`--choose-model`, `--model-file`, or the front end's Preferences page.

## What else to put here

The runnable format for Milelik's Ollama, LM Studio and llama.cpp backends is
**GGUF**:

```text
models/
├── smollm2-135m-instruct-q4_1.gguf          # bundled default
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

It prefers an explicit or remembered selection, then a file matching the
configured model, then the bundled SmolLM2 default, then
Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M, then the first supported GGUF.
Use `--choose-model` to force the terminal chooser. The browser front end has
the same list in **Preferences → Model file from `models/`**.

Useful commands:

```bash
milelik --list-models       # show every model file found
milelik --choose-model      # force the startup chooser
milelik --model-file models/smollm2-135m-instruct-q4_1.gguf
milelik --no-model-scan     # ignore the models/ folders for this run
python3 setup_model.py      # import the bundled default into Ollama
```

## Git note

Additional model weights are normally hundreds of megabytes or several
gigabytes. This repository's `.gitignore` ignores model binaries inside
`models/` except for the bundled SmolLM2 file above, so dropping another GGUF
here makes it available locally without accidentally committing it to Git.
