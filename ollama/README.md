# Ollama runtime (vendored in this repository)

Ollama is Milelik's default local model server. This folder makes it a
first-class, fully offline-capable part of the repository:

| File | Purpose |
| --- | --- |
| [`ollama.json`](ollama.json) | Pinned release manifest: exact version, download URLs and per-CPU file names |
| [`fetch_ollama.py`](fetch_ollama.py) | One-time vendor script: downloads + verifies the release, splits it into Git-friendly chunks |
| [`vendor/`](vendor/) + [`vendor.json`](vendor.json) | The actual Ollama release bytes as ~90 MB verified chunks (after fetching) |
| [`setup_ollama.py`](setup_ollama.py) | Installer: assembles vendored chunks offline, or downloads when they are absent |

## Fully offline flow (fresh linux Mint, no internet)

Do this **once** on any machine with internet (it downloads ~1.4 GB):

```bash
python3 ollama/fetch_ollama.py
git add ollama/vendor ollama/vendor.json
git commit -m "Vendor Ollama runtime for offline installs"
git push
```

From then on, every clone of this repository carries the real Ollama
bytes. On the brand-new Mint machine — with or without internet:

```bash
python3 ollama/setup_ollama.py --offline   # assemble + verify chunks, zero downloads
ollama serve &                              # start the model server
python3 setup_model.py                      # import models/smollm2-135m-instruct-q4_1.gguf
python3 app.py                              # Milelik front end (+ APIs/CLI)
```

or run the whole thing through the main installer, which prefers the
vendored chunks automatically and needs no downloads either:

```bash
DEPENDENCY_MODE=none python3 install_milelik.py --yes
```

(`DEPENDENCY_MODE=none` skips the apt step; everything those packages
provide is optional on top of the bundled runtime. The only system tools
the offline path uses are `python3`, `tar` and `zstd`, all preinstalled
on linux Mint. If `git` itself is missing on the fresh box, copy the
repository over on a USB stick instead of cloning.)

## Download flow (no vendored chunks committed yet)

If `ollama/vendor/` was never fetched, the same commands simply download
the pinned release instead:

```bash
python3 ollama/setup_ollama.py          # downloads the pinned release
python3 ollama/setup_ollama.py --check  # report install + server state, no downloads
python3 ollama/setup_ollama.py --print-url  # show the pinned URL for this PC
```

Useful options: `--prefix ~/my-ollama`, `--arch arm64`,
`--output-archive /mnt/usb/ollama.tar.zst` (fetch only), and
`OLLAMA_DOWNLOAD_BASE` for mirrors. Re-run `fetch_ollama.py` to switch
CPU architecture or to vendor a newer pinned release.

## “I use x86, not AMD” — read this first

On the Ollama download page the 64-bit x86 file is called
`ollama-linux-amd64.tar.zst`. **That `amd64` file is the correct build for
Intel processors too** — `amd64` is just linux's historical file-name for
64-bit x86 (x86_64), the architecture shared by Intel and AMD desktop CPUs.
There is no separate Intel-only build.

What to pick:

- **Intel or AMD 64-bit PC** (anything running 64-bit linux Mint): use the
  plain `amd64` file. Both scripts select it automatically when your CPU
  reports `x86_64`.
- **AMD graphics card**: only then consider the separate `-rocm` file, which
  is about AMD *GPUs*, not AMD CPUs. The plain `amd64` file already covers
  NVIDIA GPUs and CPU fallback.
- **32-bit x86** (`i386`/`i686`): Ollama publishes no 32-bit build, so the
  scripts stop with a clear error. Use a 64-bit OS install instead.

## How the vendoring stays trustworthy

- Every chunk's SHA-256 plus the whole archive's SHA-256 and size are
  recorded in `vendor.json` and re-verified on every install; any mismatch
  aborts before anything is extracted.
- Chunks are ~90 MB each: safely under GitHub's 100 MB single-file limit,
  so no Git LFS (itself a dependency) is needed.
- To upgrade Ollama later: bump `pinned_version`/`download_base` in
  `ollama.json`, re-run `fetch_ollama.py --force`, and commit.

`python3 uninstall_milelik.py` deliberately leaves Ollama and its models
in place.
