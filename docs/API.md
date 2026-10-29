# Milelik Local API

Milelik's front end also gives you a small local HTTP API for your own
scripts and automations. It runs on your device — by default at
`http://127.0.0.1:8765` — and is started with:

```bash
milelik --gui
# or
milelik-web
```

The startup output prints a browser URL containing a one-time token. Opening
that URL sets an HttpOnly browser cookie. Scripts should send the token
explicitly:

```http
X-Milelik-Token: THE_TOKEN_PRINTED_AT_STARTUP
```

Treat the token like a password: some APIs can run code and Milelik tools on
your computer. Keep the default `127.0.0.1` bind address unless you
understand the risk of exposing it to a LAN.

The machine-readable OpenAPI description is available from
`GET /api/openapi.json`, and the front end's **APIs** page is an interactive
playground for everything below.

## Conventions

- Requests and responses are JSON unless downloading a workspace file.
- Errors look like `{"error": "what went wrong"}`.
- `GET /api/health` is the only unauthenticated API endpoint.
- Model/tool actions that need permission wait for approval in the browser.
  Dangerous actions require typing `YES` in that approval dialog.

## AI and Milelik tools

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/chat` | Send a natural-language task to the local model and Milelik's tools |
| `POST` | `/api/chat/reset` | Clear the web chat history/context |
| `GET` | `/api/chat/history` | Visible web chat transcript |
| `GET` | `/api/tools` | All 26 Milelik tool schemas |
| `POST` | `/api/tools/run` | Call one Milelik tool directly |
| `GET` | `/api/approvals` | Pending browser approvals |
| `POST` | `/api/approvals/{id}/decide` | Approve/deny a pending action |
| `GET` | `/api/models` | Backend models plus every AI model file discovered in `models/` |
| `POST` | `/api/models/select` | Choose a local model file (or clear the choice) |
| `GET` | `/api/status` | Model/server/workspace/helper status, including the `models/` inventory |
| `GET` | `/api/system` | Structured + text system information |
| `GET`/`POST` | `/api/settings` | Read or save Milelik preferences |

```bash
curl -H "X-Milelik-Token: TOKEN" -H "Content-Type: application/json" \
  -d '{"message":"What is my disk space and local IP?"}' \
  http://127.0.0.1:8765/api/chat
```

```bash
curl -H "X-Milelik-Token: TOKEN" -H "Content-Type: application/json" \
  -d '{"tool":"github_account","arguments":{"action":"auth_status"}}' \
  http://127.0.0.1:8765/api/tools/run
```

### Local model files

`GET /api/models` returns the selected backend's server models in `models`,
and the startup `models/` inventory in `local_models`:

```json
{
  "backend": "ollama",
  "models": ["milelik-smollm2:135m"],
  "local_models": {
    "directories": ["/home/you/.local/share/milelik/models"],
    "runnable_count": 1,
    "selected": {
      "name": "smollm2-135m-instruct-q4_1.gguf",
      "supported": true,
      "ollama_name": "milelik-smollm2:135m",
      "is_bundled_default": true,
      "is_default": true
    },
    "files": []
  }
}
```

Each model record also includes `is_qwen25_coder`, `is_bundled_default` and
`is_default` flags. On a fresh configuration, Milelik's bundled
`smollm2-135m-instruct-q4_1.gguf` is the default; Qwen and other discovered
GGUFs remain selectable.

Choose a discovered file by id, path, filename, stem or generated Ollama
name. Unsupported formats and incomplete split GGUFs are rejected:

```bash
curl -H "X-Milelik-Token: TOKEN" -H "Content-Type: application/json" \
  -d '{"model_file":"smollm2-135m-instruct-q4_1.gguf"}' \
  http://127.0.0.1:8765/api/models/select

# Use the configured/server model again:
curl -H "X-Milelik-Token: TOKEN" -H "Content-Type: application/json" \
  -d '{"model_file":""}' http://127.0.0.1:8765/api/models/select
```

The same preference can be saved with `POST /api/settings` using
`models_dir`, `model_file` and `model_discovery`. Selecting a GGUF resets the
web agent; on the next chat, Milelik imports it into Ollama first if Ollama is
the selected backend and that import does not exist yet.

## Workspace and code runner

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/workspace` | Workspace root, file tree and suggested project commands |
| `GET` | `/api/workspace/file?path=...` | Read a text file (2 MB editor limit) |
| `PUT`/`POST` | `/api/workspace/file` | Create/replace a text file |
| `POST` | `/api/workspace/folder` | Create a folder |
| `POST` | `/api/workspace/rename` | Rename/move inside the workspace |
| `POST`/`DELETE` | `/api/workspace/delete` or `/api/workspace/file?path=...` | Delete a file or empty folder |
| `GET` | `/api/workspace/download?path=...` | Download a workspace file (10 MB limit) |
| `POST` | `/api/workspace/run-file` | Run a `.py`, `.sh`/`.bash` or `.js` file |
| `POST` | `/api/workspace/clone` | `git clone --depth 1` a GitHub repo into the workspace |
| `GET` | `/api/code/runtimes` | Python/Bash/Node/npm/Git/gh availability |
| `POST` | `/api/code/run` | Run a Python, Bash or JavaScript snippet |
| `POST` | `/api/code/check` | Syntax-check a snippet without running it |
| `POST` | `/api/code/regex` | Test a regular expression |
| `POST` | `/api/code/encode` | Base64/URL/HTML/hex transforms and JSON formatting |

All workspace paths are relative to the configured workspace and are checked
so `../` cannot escape it. Code runs as your linux user in the workspace;
Milelik removes common API/GitHub tokens from the child environment, but it
is still intentionally a local code runner, not a sandbox for untrusted code.

```bash
curl -H "X-Milelik-Token: TOKEN" -H "Content-Type: application/json" \
  -d '{"path":"hello.py","content":"print(2 + 3)\n"}' \
  -X PUT http://127.0.0.1:8765/api/workspace/file

curl -H "X-Milelik-Token: TOKEN" -H "Content-Type: application/json" \
  -d '{"path":"hello.py"}' \
  http://127.0.0.1:8765/api/workspace/run-file
```

Clone and run a project without using a browser download/ZIP:

```bash
curl -H "X-Milelik-Token: TOKEN" -H "Content-Type: application/json" \
  -d '{"url":"https://github.com/owner/repository.git"}' \
  http://127.0.0.1:8765/api/workspace/clone
```

## Mathematics

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/math/evaluate` | Safely evaluate arithmetic (AST-based, never Python `eval`) |
| `POST` | `/api/math/statistics` | Mean/median/mode/range/variance/quartiles |
| `POST` | `/api/math/convert` | Length, mass, time, data, speed, area, volume, pressure, energy, temperature |
| `POST` | `/api/math/quadratic` | Solve `ax² + bx + c = 0`, vertex and complex roots |
| `GET` | `/api/math/constants` | Supported constants, functions and units |

```bash
curl -H "X-Milelik-Token: TOKEN" -H "Content-Type: application/json" \
  -d '{"expression":"sqrt(x^2 + 4^2) + 2*pi","variables":{"x":3}}' \
  http://127.0.0.1:8765/api/math/evaluate
# {"result": 11.283185307179586, ...}
```

Supported functions include `sqrt`, `sin`, `cos`, `tan`, `asin`, `acos`,
`atan`, `atan2`, `log`, `log2`, `log10`, `exp`, `floor`, `ceil`, `round`,
`abs`, `min`, `max`, `factorial`, `gcd`, `hypot`, `degrees` and `radians`.
In API examples, `^` is accepted as a typing convenience and converted to
Python's `**` by the evaluator endpoint.

## Utilities

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/utilities/text` | Text statistics, case transforms, slugify, reverse, word frequencies |
| `POST` | `/api/utilities/hash` | MD5, SHA-1, SHA-256, SHA-512 or BLAKE2b |
| `POST` | `/api/utilities/password` | Generate a random password locally |
| `GET` | `/api/utilities/time` | Local/UTC time and Unix timestamp |
| `GET` | `/api/docs` | Endpoint catalogue used by the front end |
| `GET` | `/api/openapi.json` | OpenAPI 3 description |

## Python example

```python
import json
import urllib.request

TOKEN = "paste-the-token-printed-by-milelik---gui"

def call(path, payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:8765{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Milelik-Token": TOKEN,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

print(call("/api/math/evaluate", {"expression": "sqrt(3^2 + 4^2)"}))
print(call("/api/code/run", {
    "language": "python",
    "code": "for n in range(3): print(n, n*n)",
}))
```
