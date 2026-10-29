"""Tests for Milelik's browser front end, workspace and local APIs."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_module(name, filename):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = load_module("milelik", "milelik.py")
web = load_module("milelik_web", "milelik_web.py")


class FakeClient:
    backend = "ollama"
    display_name = "Fake Ollama"
    model = "fake-model:latest"
    host = "http://127.0.0.1:11434"

    def is_up(self):
        return True

    def ensure_running(self):
        return True

    def tags(self, timeout=3):
        return [self.model]

    def resolve_available_model(self, model=None, update=False):
        return self.model

    def model_available(self, model=None):
        return True


class FakeAgent:
    def __init__(self):
        self.tasks = []
        self.reset_count = 0

    def run(self, task):
        self.tasks.append(task)
        return f"Fake answer to: {task}"

    def reset(self):
        self.reset_count += 1


class TestCoreWebHooks(unittest.TestCase):
    def test_default_config_contains_frontend_preferences(self):
        cfg = core.default_config()
        for key in ("workspace", "web_host", "web_port", "open_browser",
                    "frontend_enabled", "ui_theme", "ui_default_view",
                    "cmd_timeout", "models_dir", "model_file", "model_discovery"):
            self.assertIn(key, cfg)
        self.assertEqual(cfg["web_port"], 8765)
        self.assertFalse(cfg["auto"])

    def test_gui_cli_arguments(self):
        args = core.parse_args([
            "--gui", "--workspace-dir", "/tmp/ws", "--web-host", "127.0.0.1",
            "--web-port", "9999", "--no-open-browser",
        ])
        self.assertTrue(args.gui)
        self.assertEqual(args.workspace_dir, "/tmp/ws")
        self.assertEqual(args.web_port, 9999)
        self.assertFalse(args.open_browser)

    def test_toolbox_confirm_callback_is_used_for_ordinary_and_dangerous(self):
        seen = []

        def callback(**kwargs):
            seen.append(kwargs)
            return kwargs.get("dangerous_reason") is None

        toolbox = core.Toolbox(auto=False, cwd="/tmp", confirm_callback=callback)
        self.assertTrue(toolbox.confirm("run a command", "df -h"))
        self.assertFalse(toolbox.confirm(
            "delete everything", "rm -rf /", dangerous_reason="deleting everything"))
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1]["dangerous_reason"], "deleting everything")


class TestMathAndUtilityFunctions(unittest.TestCase):
    def test_safe_math_statistics_conversion_and_quadratic(self):
        self.assertEqual(web.evaluate_math_expression("sqrt(x**2 + 9)", {"x": 4}), 5)
        self.assertAlmostEqual(
            web.evaluate_math_expression("2*pi + factorial(4)"), 30.283185307179586)
        with self.assertRaises(web.ApiError):
            web.evaluate_math_expression("__import__('os').system('echo hacked')")

        stats = web.statistics_payload([4, 8, 15, 16, 23, 42])
        self.assertEqual(stats["count"], 6)
        self.assertEqual(stats["sum"], 108)
        self.assertEqual(stats["median"], 15.5)

        conversion = web.convert_units(100, "C", "F")
        self.assertEqual(conversion["value"], 212)
        self.assertEqual(web.convert_units(1, "mile", "km")["value"], 1.609344)

        quadratic = web.solve_quadratic(1, -3, 2)
        self.assertEqual(quadratic["roots"], [1, 2])

    def test_coding_and_text_utilities(self):
        matches = web.regex_search(r"(\w+)@example\.com", "a@example.com", "i")
        self.assertEqual(matches["matches"][0]["groups"], ["a"])
        self.assertEqual(
            web.encode_decode("base64_encode", "Milelik")["result"], "TWlsZWxpaw==")
        stats = web.text_utility("stats", "Hello local Milelik world.")
        self.assertEqual(stats["words"], 4)
        password = web.generate_password(24, True)
        self.assertEqual(len(password["password"]), 24)


class TestMilelikWebServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.env_patch = mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(root / "config")})
        self.env_patch.start()
        self.client_patch = mock.patch.object(
            core, "select_client", lambda *a, **kw: FakeClient())
        self.client_patch.start()
        self.server = web.create_server(
            "127.0.0.1", 0, self.workspace, core_module=core, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.state.bound_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.client_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def request(self, path, method="GET", body=None, token="test-token", raw=False):
        headers = {}
        data = None
        if token:
            headers["X-Milelik-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = response.read()
                if raw:
                    return response.status, payload, response.headers
                text = payload.decode()
                try:
                    return response.status, json.loads(text), response.headers
                except ValueError:
                    return response.status, text, response.headers
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            try:
                data = json.loads(payload)
            except ValueError:
                data = payload
            return e.code, data, e.headers

    def test_frontend_and_authentication(self):
        status, page, _headers = self.request("/", token=None)
        self.assertEqual(status, 200)
        self.assertIn("Milelik — Local Companion Workspace", page)
        self.assertIn('id="spriteStage"', page)
        self.assertIn('class="milelik-sprite"', page)
        self.assertNotIn("MILELIK_COMPANION_SPRITE -->", page)
        self.assertIn('id="chatForm"', page)
        self.assertIn('id="codeEditor"', page)
        self.assertIn('id="endpointList"', page)

        status, _js, _headers = self.request("/app.js", token=None)
        self.assertEqual(status, 200)
        status, favicon, headers = self.request(
            "/sprites/milelik-icon.svg", token=None, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/svg+xml")
        self.assertTrue(favicon.lstrip().startswith(b"<svg"))
        status, alias_icon, _headers = self.request(
            "/favicon.svg", token=None, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(alias_icon, favicon)
        status, companion, headers = self.request(
            "/sprites/milelik-companion.svg", token=None, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/svg+xml")
        self.assertIn(b'class="milelik-sprite"', companion)
        status, error, _headers = self.request("/api/status", token=None)
        self.assertEqual(status, 401)
        self.assertIn("token", error["error"].lower())

        status, status_data, _headers = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(status_data["model"]["model_available"])
        self.assertIn("github_pull_requests", status_data["tools"])

    def test_workspace_files_code_runner_and_path_safety(self):
        status, workspace, _ = self.request("/api/workspace")
        self.assertEqual(status, 200)
        names = {node["name"] for node in workspace["tree"]}
        self.assertIn("README.md", names)
        self.assertIn("examples", names)

        status, saved, _ = self.request(
            "/api/workspace/file", "PUT",
            {"path": "demo.py", "content": "name = input()\nprint(f'Hello {name}')\n"})
        self.assertEqual(status, 200)
        self.assertTrue(saved["saved"])

        status, run, _ = self.request(
            "/api/workspace/run-file", "POST",
            {"path": "demo.py", "stdin": "Ada\n"})
        self.assertEqual(status, 200)
        self.assertEqual(run["exit_code"], 0)
        self.assertIn("Hello Ada", run["stdout"])

        status, code, _ = self.request(
            "/api/code/run", "POST",
            {"language": "bash", "code": "printf 'shell-ok\\n'"})
        self.assertEqual(status, 200)
        self.assertIn("shell-ok", code["stdout"])

        status, error, _ = self.request("/api/workspace/file?path=../outside.txt")
        self.assertEqual(status, 400)
        self.assertIn("outside the workspace", error["error"])

        status, clone_error, _ = self.request(
            "/api/workspace/clone", "POST", {"url": "https://example.com/a/b.git"})
        self.assertEqual(status, 400)
        self.assertIn("GitHub", clone_error["error"])

    def test_math_coding_utility_and_openapi_endpoints(self):
        calls = [
            ("/api/math/evaluate", {"expression": "sqrt(3^2+4^2)"}, "result"),
            ("/api/math/statistics", {"numbers": "1,2,3,4"}, "mean"),
            ("/api/math/convert", {"value": 72, "from": "F", "to": "C"}, "value"),
            ("/api/math/quadratic", {"a": 1, "b": -3, "c": 2}, "roots"),
            ("/api/code/check", {"language": "python", "code": "print('ok')"}, "ok"),
            ("/api/code/regex", {"pattern": "\\d+", "text": "A1 B22"}, "matches"),
            ("/api/code/encode", {"operation": "base64_encode", "text": "x"}, "result"),
            ("/api/utilities/text", {"operation": "stats", "text": "one two"}, "words"),
            ("/api/utilities/hash", {"algorithm": "sha256", "text": "Milelik"}, "digest"),
            ("/api/utilities/password", {"length": 16, "symbols": False}, "password"),
        ]
        for path, body, key in calls:
            with self.subTest(path=path):
                status, data, _ = self.request(path, "POST", body)
                self.assertEqual(status, 200, data)
                self.assertIn(key, data)

        status, docs, _ = self.request("/api/docs")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(docs["endpoints"]), 20)

        status, spec, _ = self.request("/api/openapi.json")
        self.assertEqual(status, 200)
        self.assertIn("/api/math/evaluate", spec["paths"])
        self.assertIn("MilelikToken", spec["components"]["securitySchemes"])

    def test_settings_are_validated_and_saved(self):
        new_workspace = Path(self.tmp.name) / "MyCode"
        status, result, _ = self.request("/api/settings", "POST", {
            "model": "fake-model:latest",
            "backend": "ollama",
            "ctx": 4096,
            "max_steps": 7,
            "cmd_timeout": 45,
            "auto": False,
            "workspace": str(new_workspace),
            "ui_theme": "light",
            "ui_default_view": "workspace",
        })
        self.assertEqual(status, 200, result)
        self.assertEqual(result["settings"]["ui_theme"], "light")
        self.assertTrue(new_workspace.is_dir())
        saved = json.loads((Path(self.tmp.name) / "config" / "milelik" / "config.json").read_text())
        self.assertEqual(saved["ctx"], 4096)

        status, error, _ = self.request(
            "/api/settings", "POST", {"backend": "cloud"})
        self.assertEqual(status, 400)
        self.assertIn("backend", error["error"])

    def test_local_model_files_are_listed_and_selectable(self):
        models_dir = Path(self.tmp.name) / "models"
        models_dir.mkdir()
        target = models_dir / core.TARGET_GGUF_FILENAME
        bundled = models_dir / core.BUNDLED_MODEL_FILENAME
        other = models_dir / "Other-Model.gguf"
        target.write_bytes(b"GGUFtarget")
        bundled.write_bytes(b"GGUFbundled")
        other.write_bytes(b"GGUFother")
        (models_dir / "weights.safetensors").write_bytes(b"123")
        self.server.state.models_dir_override = str(models_dir)
        self.server.state.refresh_local_models()

        status, models, _ = self.request("/api/models")
        self.assertEqual(status, 200, models)
        local = models["local_models"]
        self.assertIn(str(models_dir.resolve()), local["directories"])
        self.assertGreaterEqual(local["runnable_count"], 3)
        self.assertTrue({
            target.name, bundled.name, other.name, "weights.safetensors"
        }.issubset({f["name"] for f in local["files"]}))
        bundled_record = next(
            f for f in local["files"] if f["name"] == bundled.name)
        self.assertTrue(bundled_record["is_bundled_default"])
        self.assertTrue(bundled_record["is_default"])
        self.assertEqual(bundled_record["ollama_name"],
                         core.BUNDLED_MODEL_OLLAMA_NAME)

        status, selected, _ = self.request(
            "/api/models/select", "POST", {"model_file": other.name})
        self.assertEqual(status, 200, selected)
        self.assertEqual(selected["local_models"]["selected"]["name"], other.name)
        self.assertEqual(selected["settings"]["model_file"], str(other.resolve()))
        self.assertEqual(selected["settings"]["model"], other.stem)
        saved = json.loads((Path(self.tmp.name) / "config" / "milelik" /
                            "config.json").read_text())
        self.assertEqual(saved["model_file"], str(other.resolve()))

        status, status_data, _ = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            status_data["model"]["selected_model_file"]["name"], other.name)

        status, unsupported, _ = self.request(
            "/api/models/select", "POST", {"model_file": "weights.safetensors"})
        self.assertEqual(status, 400)
        self.assertIn("not runnable", unsupported["error"])

        status, cleared, _ = self.request(
            "/api/models/select", "POST", {"model_file": ""})
        self.assertEqual(status, 200)
        self.assertIsNone(cleared["local_models"]["selected"])

        status, settings, _ = self.request("/api/settings", "POST", {
            "models_dir": str(models_dir),
            "model_file": target.name,
            "model_discovery": True,
            "backend": "ollama",
        })
        self.assertEqual(status, 200, settings)
        self.assertEqual(settings["settings"]["model"], core.LOCAL_IMPORTED_MODEL)
        self.assertEqual(settings["settings"]["model_file"], str(target.resolve()))

    def test_chat_uses_agent_and_records_history(self):
        fake = FakeAgent()
        self.server.state._agent = fake
        self.server.state._client = FakeClient()
        status, result, _ = self.request(
            "/api/chat", "POST", {"message": "Check my disk"})
        self.assertEqual(status, 200)
        self.assertIn("Fake answer", result["answer"])
        self.assertEqual(fake.tasks, ["Check my disk"])

        status, history, _ = self.request("/api/chat/history")
        self.assertEqual(status, 200)
        self.assertEqual([m["role"] for m in history["messages"]], ["user", "assistant"])

        status, reset, _ = self.request("/api/chat/reset", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(reset["reset"])
        self.assertEqual(fake.reset_count, 1)

    def test_browser_approval_requires_typed_yes_for_danger(self):
        answer = {}

        def ask():
            answer["value"] = self.server.state.confirm_from_tool(
                action="merge a pull request", detail="demo#1",
                dangerous_reason="merging code", sudo=False)

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        approval = None
        for _ in range(50):
            status, data, _ = self.request("/api/approvals")
            if data["approvals"]:
                approval = data["approvals"][0]
                break
            time.sleep(0.05)
        self.assertIsNotNone(approval)

        status, error, _ = self.request(
            f"/api/approvals/{approval['id']}/decide", "POST", {"approved": True})
        self.assertEqual(status, 400)
        self.assertIn("YES", error["error"])

        status, decided, _ = self.request(
            f"/api/approvals/{approval['id']}/decide", "POST",
            {"approved": True, "typed_yes": "YES"})
        self.assertEqual(status, 200)
        self.assertEqual(decided["status"], "approved")
        worker.join(timeout=5)
        self.assertTrue(answer["value"])

    def test_direct_tool_api_rejects_unknown_tools(self):
        status, result, _ = self.request(
            "/api/tools/run", "POST", {"tool": "system_info", "arguments": {}})
        self.assertEqual(status, 200)
        self.assertIn("Working directory", result["result"])

        status, error, _ = self.request(
            "/api/tools/run", "POST", {"tool": "not_a_tool", "arguments": {}})
        self.assertEqual(status, 400)
        self.assertIn("Unknown", error["error"])


class TestFrontendAssetsAndInstaller(unittest.TestCase):
    def test_frontend_assets_and_desktop_launcher_exist(self):
        html = (ROOT / "frontend" / "index.html").read_text()
        js = (ROOT / "frontend" / "app.js").read_text()
        css = (ROOT / "frontend" / "styles.css").read_text()
        desktop = (ROOT / "milelik-workspace.desktop").read_text()
        for text in ("Workspace — code on this device", "/api/math/evaluate",
                     "/api/code/run", "/api/workspace/clone", "X-Milelik-Token",
                     "set-model-file", "/api/models/select",
                     "Read models/ at startup"):
            self.assertTrue(text in html or text in js, text)
        self.assertIn(".workspace-layout", css)
        self.assertIn("Exec=milelik --gui", desktop)

        node = subprocess.run(
            ["node", "--check", str(ROOT / "frontend" / "app.js")],
            text=True, capture_output=True, timeout=20) if subprocess.run(
                ["which", "node"], capture_output=True).returncode == 0 else None
        if node is not None:
            self.assertEqual(node.returncode, 0, node.stderr)

    def test_black_and_white_companion_sprite_is_wired(self):
        html = (ROOT / "frontend" / "index.html").read_text()
        js = (ROOT / "frontend" / "app.js").read_text()
        css = (ROOT / "frontend" / "styles.css").read_text()
        sprite = (ROOT / "frontend" / "sprites" / "milelik-companion.svg").read_text()

        for needle in (
            'id="spriteStage"', "MILELIK_COMPANION_SPRITE",
            'id="spriteSpeech"', 'id="spriteStateLabel"',
            "SmolLM2 is bundled in models/",
        ):
            self.assertIn(needle, html)
        for needle in (
            'class="milelik-sprite"', 'class="sprite-eye"',
            'class="sprite-mouth"', "black-and-white angular glitch companion",
        ):
            self.assertIn(needle, sprite)
        self.assertIn("function setSpriteState", js)
        for state in ('"thinking"', '"talking"', '"glitch"', '"alert"', '"sleep"'):
            self.assertIn(state, js)
        for needle in (
            ".companion-card", ".sprite-stage", ".sprite-bubble",
            '@keyframes sprite-glitch', '@keyframes sprite-blink',
            'prefers-reduced-motion: reduce', "--bg: #050607",
            "--text: #f2f2f0", "clip-path: polygon(14px 0",
        ):
            self.assertIn(needle, css)

        sprites_dir = ROOT / "frontend" / "sprites"
        self.assertEqual(
            {path.name for path in sprites_dir.glob("*.svg")},
            {"milelik-companion.svg", "milelik-icon.svg"})
        self.assertTrue((sprites_dir / "README.md").is_file())
        self.assertFalse((ROOT / "frontend" / "favicon.svg").exists())
        favicon = (sprites_dir / "milelik-icon.svg").read_text()
        self.assertTrue(favicon.lstrip().startswith("<svg"))
        self.assertIn('type="image/svg+xml" href="/sprites/milelik-icon.svg"', html)
        combined = (html + sprite + favicon + js + css).lower()
        self.assertNotIn("<img", combined)
        for raster in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                       "data:image/png", "data:image/jpeg", "data:image/gif"):
            self.assertNotIn(raster, combined)

    def test_scripted_installer_wizard_saves_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            home = root / "home"
            bindir.mkdir()
            home.mkdir()
            apt_log = root / "apt.log"
            (bindir / "apt-get").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$FAKE_APT_LOG\"\n")
            (bindir / "sudo").write_text("#!/usr/bin/env bash\nexec \"$@\"\n")
            (bindir / "dpkg-query").write_text(
                "#!/usr/bin/env bash\necho 'install ok installed'\n")
            for path in bindir.iterdir():
                path.chmod(0o755)

            # Full deps; existing LM Studio; scan the installed models/
            # folder with no fixed preferred file; fast; ask-first; front
            # end in the default workspace on port 9876; light/workspace;
            # no GitHub login; launch nothing; proceed.
            answers = "\n".join([
                "1", "3", "1", "", "", "1", "", "2", "1", "", "", "",
                "9876", "n", "3", "2", "n", "3", "",
            ]) + "\n"
            env = dict(os.environ)
            env.update({
                "HOME": str(home),
                "PATH": str(bindir) + ":" + env.get("PATH", ""),
                "FAKE_APT_LOG": str(apt_log),
            })
            result = subprocess.run(
                [str(ROOT / "install.sh"), "--wizard", "--skip-check"],
                cwd=ROOT, env=env, input=answers, text=True,
                capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Step 1 of 7", result.stdout)
            self.assertIn("Saved your Milelik preferences", result.stdout)

            config = json.loads((home / ".config" / "milelik" / "config.json").read_text())
            self.assertEqual(config["backend"], "openai")
            self.assertEqual(config["host"], "http://127.0.0.1:1234")
            self.assertEqual(config["performance_profile"], "fast")
            self.assertEqual(config["web_port"], 9876)
            self.assertEqual(config["ui_theme"], "light")
            self.assertEqual(config["ui_default_view"], "workspace")
            self.assertTrue(config["model_discovery"])
            self.assertEqual(config["models_dir"], "")
            self.assertEqual(config["model_file"], "")
            self.assertTrue((home / ".local" / "share" / "milelik" / "models" / "README.md").is_file())
            self.assertTrue((home / ".local" / "share" / "milelik" / "models" / "LICENSE-SmolLM2-Apache-2.0.txt").is_file())
            self.assertTrue((home / ".local" / "share" / "milelik" / "frontend" / "app.js").is_file())
            self.assertTrue((home / ".local" / "share" / "milelik" / "frontend" / "sprites" / "milelik-companion.svg").is_file())
            self.assertTrue((home / ".local" / "share" / "milelik" / "frontend" / "sprites" / "milelik-icon.svg").is_file())
            self.assertTrue((home / ".local" / "share" / "milelik" / "setup_milelik.py").is_file())
            self.assertTrue((home / ".local" / "bin" / "milelik-web").is_file())
            self.assertTrue((home / ".local" / "bin" / "milelik-setup").is_file())
            self.assertTrue((home / ".local" / "share" / "applications" / "milelik-workspace.desktop").is_file())
            self.assertIn(" nodejs ", " " + apt_log.read_text() + " ")


if __name__ == "__main__":
    unittest.main()
