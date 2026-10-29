# 🤖 Milelik — BOOTMILELIK

A **fast, local AI assistant for linux Mint**, tuned for
**Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M**. The AI model runs 100% on
your own computer — via [Ollama](https://ollama.com), or from the exact GGUF
already loaded in **LM Studio** / **llama.cpp** — with no cloud and no
accounts. It can **run shell commands** and **use the network** (fetch web
pages, search the web, DNS lookups, port checks) to actually get things done.

It does *not* control your mouse/screen, and it asks your permission before
running commands — see [Safety](#-safety) below.

```
You ──► milelik (agent) ──► local model (Ollama / LM Studio / llama.cpp)
              │
              ├──► bash commands        (with your OK)
              ├──► read/write files     (with your OK)
              ├──► fetch_url / web_search ──► internet
              └──► DNS / port checks ──► your network
```

Single Python file, **zero pip dependencies** (standard library only), so it
installs in seconds and can't break from dependency updates.

## 🎯 The model: Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M

This model goes by different names in different places; Milelik recognises
**all of them** and treats them as the same model:

| Where | Name |
|---|---|
| GGUF file | `Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf` |
| Ollama library | `huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M` (note: "abliterate") |
| LM Studio | `qwen2.5-coder-7b-instruct-abliterated-q4_k_m` |
| Hugging Face | `bartowski/Qwen2.5-Coder-7B-Instruct-abliterated-GGUF:Q4_K_M` |
| After `./setup-model.sh` | `milelik-coder:7b` (your GGUF imported into Ollama) |

Three ways to run it — use whichever matches what you already have:

**1. Ollama (default)** — `./install.sh` pulls it for you, or manually:
```bash
ollama pull huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M
milelik "check my disk space, memory, and local IP"
```
You can also just pass the name you know — it is translated for you:
```bash
milelik --model Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M "check my disk"
```

**2. You already have the GGUF file** — import it (with the correct
Qwen2.5-Coder ChatML + tool-calling template) instead of downloading it again:
```bash
./setup-model.sh ~/Downloads/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf
# or, equivalently:
milelik --import-gguf ~/Downloads/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf
milelik "check my disk"   # now uses it by default, as milelik-coder:7b
```
`./setup-model.sh` with no arguments searches this folder, `~/Downloads`,
`~/models` and LM Studio's model folders for the file. The Modelfile used is
[`Modelfile.qwen2.5-coder-abliterated`](Modelfile.qwen2.5-coder-abliterated) —
importing a raw Qwen GGUF *without* its template is the usual reason tool
calls come out as rambling text or the model never stops talking.

**3. It's loaded in LM Studio or llama.cpp** — no Ollama copy needed at all:
```bash
# LM Studio: load the GGUF, then Developer/Serve -> Start Server (port 1234)
milelik --check        # should say the target model was found
milelik "check my disk"

# llama.cpp:
llama-server -m ~/models/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf \
    --host 127.0.0.1 --port 8080 -c 8192 --jinja
milelik "check my disk"
```
Milelik auto-detects whichever of Ollama (11434), LM Studio (1234) or
llama.cpp (8080) is already serving the model. Force one with
`--backend ollama|openai` and/or `--host`.

Other models still work too — anything in Ollama or on an OpenAI-compatible
local server, via `--model`. But the prompt format, sampling
(temperature 0.1, top_p 0.8, top_k 20, repeat_penalty 1.05), one-tool-at-a-time
loop and tool-call parsing in Milelik are specifically tuned so this 7B coder
reliably emits valid `<tool_call>` JSON.

## 🚀 Install (linux Mint)

```bash
git clone https://github.com/Math-halper/BOOTMILELIK.git
cd BOOTMILELIK
./install.sh
```

That installs Ollama (if needed), sets up
**Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M** (~4.7 GB — imported from a
GGUF it finds in the usual places, or downloaded), installs the `milelik`
command, and adds a Milelik entry to your app menu.

Then open a **new terminal** and try:

```bash
milelik "what is my local IP, public IP, and how much disk space is free?"
milelik --interactive
```

**Already have the GGUF somewhere else?** Point the installer at it — no
download, and no second copy of the weights outside Ollama's store:

```bash
GGUF=/path/to/Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf ./install.sh
```

**Want a different model anyway?** Any Ollama model works:

```bash
MODEL=qwen3:4b ./install.sh    # smaller/faster, weaker at multi-step tasks
MODEL=qwen3:14b ./install.sh   # smarter, slower, needs more RAM/VRAM
```

## 💬 Using it

One-off task:

```bash
milelik "find the 10 biggest folders in my home directory"
milelik "what process is using port 8080?"
milelik "check for apt updates and list them, do not install anything"
milelik "is port 22 open on 192.168.1.1? also find its hostname"
milelik "search the web for how to enable SSH on linux Mint and summarise the steps"
milelik "read ~/Documents/notes.txt and summarise it"
milelik "write a bash backup script for my Documents folder and save it to ~/backup.sh"
```

Interactive chat (`milelik -i`) — keeps context between tasks. Inside it:

| Command | What it does |
|---|---|
| `/model Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M` | switch model (any of its names work) |
| `/models` | list models on the current backend |
| `/yolo on` / `/yolo off` | auto-approve ordinary commands |
| `/info` | system info |
| `/clear` | forget the conversation |
| `!df -h` | run a shell command yourself |
| `/exit` | quit |

Useful flags: `milelik --help` for all of them.

```bash
milelik --check                 # diagnose Ollama/LM Studio/llama.cpp, model, network
milelik --model Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M "..."
milelik --backend openai --host http://127.0.0.1:1234 "..."   # force LM Studio
milelik --backend ollama "..."   # force Ollama
milelik --ctx 32768 "..."        # this model's full 32K context (slower, more RAM)
milelik --yolo "..."            # don't ask before ordinary commands
milelik -v "..."                 # show full tool output + which backend/model is used
milelik --max-steps 20 "..."    # allow longer, multi-step tasks
```

Settings live in `~/.config/milelik/config.json` (`milelik --init-config`
creates one): `model`, `backend` (`auto`/`ollama`/`openai`), `host`, `ctx`,
`max_steps`, `auto`. Environment variables `MILELIK_MODEL`, `MILELIK_BACKEND`,
`MILELIK_HOST`, `OLLAMA_HOST`, `OPENAI_BASE_URL` and `MILELIK_API_KEY` also
work — `host` can point at a model server on another machine on your network.

## ⚡ Making it fast

- The default model is a 7B at Q4_K_M (~4.7 GB) — a good speed/quality
  balance. It needs roughly 6 GB of free RAM/VRAM once context is included.
  On a weaker machine, `MODEL=qwen3:4b ./install.sh` is faster but noticeably
  weaker at following multi-step tasks.
- The first question after a while is slower (model loading into RAM/VRAM);
  with Ollama, Milelik keeps the model loaded for 10 minutes between calls.
- Context is capped at 8192 tokens by default (`--ctx 32768` uses this
  model's full window, at the cost of speed/RAM) and tool output is
  truncated, so long command output doesn't slow the model down.
- A GPU helps a lot, but a modern CPU with 16 GB RAM runs a 7B Q4 fine.
- Milelik runs this model **one tool call at a time** with low temperature —
  slightly more round-trips, far fewer malformed calls and dead ends.

> Honest expectations: a 7B local model is dumber than big cloud AIs. It
> handles "find big files", "what's using this port", "fetch and summarise
> this page" and most scripting/sysadmin tasks reliably, but long autonomous
> jobs may need you to steer it. It is also an *abliterated* (uncensored)
> model, so Milelik's command confirmations matter more, not less — it will
> happily propose destructive commands if asked. If it goes off track, just
> decline the command when it asks.

## 🛡️ Safety

Milelik can only do what *your user account* can do, and by default it can't
even do that without asking:

- **Every shell command and file write asks `[y/N]` first.** Saying no tells
  the model to find another way — it won't just retry.
- **`--yolo` only skips the ordinary prompts.** Dangerous commands
  (`rm -rf /`, `mkfs`, `dd` to a disk, `shutdown`/`reboot`,
  `curl … | bash`, fork bombs, …) always require you to type `YES`, and
  `sudo` commands always ask too. In non-interactive use (piped input),
  things needing confirmation are refused.
- Web fetches are GET-only and capped at ~2 MB; binary downloads aren't shown
  to the model.
- Everything the model sees stays on your machine — the only network traffic
  is your local model server on `localhost` plus whatever pages/searches
  *you* asked it to do.

Still: you're letting an AI run commands. Don't start it in a folder full of
irreplaceable files with `--yolo` on day one, and read commands before you
approve them until you trust your setup.

## 🧩 How it works

`milelik.py` is an agent loop around your local model server — Ollama's
`/api/chat`, or `/v1/chat/completions` for LM Studio / llama.cpp
(auto-detected; see `--backend`):

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
Python-style `run_command(command="…")`. Common synonym mistakes by a 7B
(`bash`, `execute_command`, `cmd`, `search_query`, …) are normalised, a
malformed call gets sent back with "reply in exactly this format" (twice),
and leaked ChatML tokens (`<|im_end|>` etc.) are stripped from answers.
If a server rejects native tools outright, Milelik falls back to the text
protocol automatically. Tools: `run_command`, `fetch_url`,
`web_search` (DuckDuckGo — no API key), `read_file`, `write_file`, `list_dir`,
`dns_lookup`, `check_port`, `system_info`.

## 🔧 Troubleshooting

| Problem | Fix |
|---|---|
| "No local model server is reachable" | `milelik --check` probes Ollama (11434), LM Studio (1234) and llama.cpp (8080) and shows which is up and where the model was found. Start one: `ollama serve`, LM Studio's server, or `llama-server …`. |
| Model not found in Ollama | `./setup-model.sh --download`, or `ollama pull huihui_ai/qwen2.5-coder-abliterate:7b-instruct-q4_K_M`. Passing `--model Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M` is fine — the name is translated. |
| Model not found in LM Studio | Actually load the GGUF in LM Studio (not just download it) and start the server, then `milelik --check`. Or pass the exact id LM Studio shows via `--model`. |
| It imported my GGUF but talks nonsense / never uses tools | You imported it without a template (`ollama create` with a bare `FROM`). Re-import with `./setup-model.sh /path/to/model.gguf` — it applies [`Modelfile.qwen2.5-coder-abliterated`](Modelfile.qwen2.5-coder-abliterated) (ChatML + `<tool_call>` format + stop tokens). |
| Model describes commands instead of running them | Usually a template problem (above), or the server rejected native tools. Milelik already falls back to text `<tool_call>` calls; with llama.cpp make sure you started `llama-server` with `--jinja`. |
| Very slow answers | Smaller context (`--ctx 4096`), close other heavy apps, or use a smaller model (`ollama pull qwen3:4b`). First reply after idle includes model load time. |
| `milelik: command not found` after install | Open a new terminal (Mint adds `~/.local/bin` to PATH at login), or `export PATH="$HOME/.local/bin:$PATH"`. |
| Web search returns nothing | DuckDuckGo sometimes rate-limits; wait a bit, or ask it to `fetch_url` a specific site. |

## 🧪 Tests

34 tests, no model or internet required (fake Ollama and fake
LM Studio/OpenAI-compatible servers are used):

```bash
python3 -m unittest discover -s tests -v
```

## 🗑️ Uninstall

```bash
./uninstall.sh   # removes Milelik only; Ollama/models are left alone
```

## License

MIT
