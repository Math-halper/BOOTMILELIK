# 🤖 Milelik — BOOTMILELIK

A **fast, local AI assistant for linux Mint**. The AI model runs 100% on your
own computer (via [Ollama](https://ollama.com) — no cloud, no API keys, no
accounts), and it can **run shell commands** and **use the network** (fetch web
pages, search the web, DNS lookups, port checks) to actually get things done.

It does *not* control your mouse/screen, and it asks your permission before
running commands — see [Safety](#-safety) below.

```
You ──► milelik (agent) ──► local model (Ollama, on your PC)
              │
              ├──► bash commands        (with your OK)
              ├──► read/write files     (with your OK)
              ├──► fetch_url / web_search ──► internet
              └──► DNS / port checks ──► your network
```

Single Python file, **zero pip dependencies** (standard library only), so it
installs in seconds and can't break from dependency updates.

## 🚀 Install (linux Mint)

```bash
git clone https://github.com/Math-halper/BOOTMILELIK.git
cd BOOTMILELIK
./install.sh
```

That installs Ollama (if needed), downloads the default model (`qwen3:8b`,
~5 GB), installs the `milelik` command, and adds a Milelik entry to your app menu.

Then open a **new terminal** and try:

```bash
milelik "what is my local IP, public IP, and how much disk space is free?"
milelik --interactive
```

**Weaker/older PC?** Use a smaller, faster model:

```bash
MODEL=qwen3:4b ./install.sh
```

**Beefy PC / good GPU?** A bigger model follows instructions better:

```bash
MODEL=qwen3:14b ./install.sh
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
| `/model qwen3:4b` | switch model |
| `/models` | list downloaded models |
| `/yolo on` / `/yolo off` | auto-approve ordinary commands |
| `/info` | system info |
| `/clear` | forget the conversation |
| `!df -h` | run a shell command yourself |
| `/exit` | quit |

Useful flags: `milelik --help` for all of them.

```bash
milelik --check                 # diagnose Ollama / model / network
milelik --model qwen3:4b "..."  # run one task with a different model
milelik --yolo "..."            # don't ask before ordinary commands
milelik -v "..."                 # show full tool output
milelik --max-steps 20 "..."    # allow longer, multi-step tasks
```

Settings live in `~/.config/milelik/config.json` (`milelik --init-config`
creates one): `model`, `host`, `ctx`, `max_steps`, `auto`.
Environment variables `MILELIK_MODEL` and `OLLAMA_HOST` also work — the latter
lets Milelik use a model running on another machine on your network.

## ⚡ Making it fast

- **Model size is the biggest lever.** `qwen3:4b` (~2.5 GB) is roughly twice
  as fast as `qwen3:8b` (~5 GB) and fine for simple commands; 8B is noticeably
  better at multi-step tasks. `qwen2.5-coder:7b` is a good pick if most tasks
  are scripting/coding; `llama3.1:8b` also supports tool calling.
- The first question after a while is slower (model loading into RAM/VRAM);
  Milelik keeps the model loaded for 10 minutes between calls.
- Context is capped at 8192 tokens by default (`--ctx`) and tool output is
  truncated, so long command output doesn't slow the model down.
- A GPU helps a lot, but a modern CPU with 16 GB RAM runs the 4B/8B models fine.

> Honest expectations: local models are dumber than big cloud AIs. An 8B model
> handles "find big files", "what's using this port", "fetch and summarise
> this page" reliably, but long autonomous jobs may need you to steer it.
> If it ever goes off track, just decline the command when it asks.

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
  is Ollama on `localhost` plus whatever pages/searches *you* asked it to do.

Still: you're letting an AI run commands. Don't start it in a folder full of
irreplaceable files with `--yolo` on day one, and read commands before you
approve them until you trust your setup.

## 🧩 How it works

`milelik.py` is an agent loop around Ollama's `/api/chat`:

1. Your task + a system prompt go to the local model along with tool schemas.
2. The model replies with a tool call (e.g. `run_command {"command": "df -h"}`).
3. Milelik checks it for danger, asks you if needed, runs it, and sends the
   (truncated) output back to the model.
4. Repeat until the model gives a final answer (max 12 steps by default,
   repeated identical calls are cut off so it can't loop forever).

Models without native function-calling still work: Milelik automatically
falls back to a JSON tool protocol. Tools: `run_command`, `fetch_url`,
`web_search` (DuckDuckGo — no API key), `read_file`, `write_file`, `list_dir`,
`dns_lookup`, `check_port`, `system_info`.

## 🔧 Troubleshooting

| Problem | Fix |
|---|---|
| "Cannot reach Ollama" | Run `ollama serve` in a terminal, or re-run `./install.sh`. `milelik --check` shows the state. |
| "Model '…' is not downloaded" | `ollama pull qwen3:8b` (or let Milelik do it when it asks). |
| Very slow answers | Switch to a smaller model: `milelik --model qwen3:4b` after `ollama pull qwen3:4b`. Close other heavy apps. |
| `milelik: command not found` after install | Open a new terminal (Mint adds `~/.local/bin` to PATH at login), or `export PATH="$HOME/.local/bin:$PATH"`. |
| Web search returns nothing | DuckDuckGo sometimes rate-limits; wait a bit, or ask it to `fetch_url` a specific site. |
| Model never uses tools / talks nonsense | It may not support tool calling well — try `qwen3:8b`, `qwen2.5:7b`, or `llama3.1:8b`. |

## 🧪 Tests

No model or internet required (a fake Ollama server is used):

```bash
python3 -m unittest discover -s tests -v
```

## 🗑️ Uninstall

```bash
./uninstall.sh   # removes Milelik only; Ollama/models are left alone
```

## License

MIT
