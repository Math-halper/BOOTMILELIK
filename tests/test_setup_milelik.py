"""Tests for Milelik's Python setup-and-run script."""
import contextlib
import importlib.util
import io
import json
import sys
from types import SimpleNamespace
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_milelik", ROOT / "setup_milelik.py")
setup = importlib.util.module_from_spec(spec)
sys.modules["setup_milelik"] = setup
spec.loader.exec_module(setup)


class TestSetupMilelikScript(unittest.TestCase):
    def test_install_command_uses_bundled_installer_and_python_launch_control(self):
        args = setup.parse_args([
            "--yes", "--skip-check", "--dependency-mode", "full",
            "--models-dir", "/tmp/models", "--model-file", "Other.gguf",
            "--workspace", "/tmp/ws", "--web-port", "9876",
        ])
        command = setup.build_install_command(args, root=ROOT, interactive_terminal=False)
        self.assertEqual(command[:2], [sys.executable, str(ROOT / "install_milelik.py")])
        self.assertIn("--yes", command)
        launch_index = command.index("--launch")
        self.assertEqual(command[launch_index + 1], "none")
        self.assertIn("--skip-check", command)
        self.assertIn("/tmp/models", command)
        self.assertIn("Other.gguf", command)
        self.assertIn("9876", command)

    def test_browser_setup_flags_and_installer_command(self):
        args = setup.parse_args(["--browser-setup"])
        self.assertTrue(args.browser_setup)
        self.assertFalse(args.terminal_setup)
        command = setup.build_install_command(
            args, root=ROOT, interactive_terminal=True)
        self.assertIn("--yes", command)
        self.assertNotIn("--wizard", command)
        self.assertTrue(setup.display_available({"DISPLAY": ":0"}))
        self.assertFalse(setup.display_available({}))

        def unexpected_input(_prompt):
            raise AssertionError("browser dry-run should not ask terminal questions")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = setup.main(
                ["--browser-setup", "--dry-run", "--install-only"],
                runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=99),
                input_fn=unexpected_input,
                stdin_isatty=True,
                stdout_isatty=True,
                installed_command=None,
                euid=1000,
            )
        self.assertEqual(code, 0)
        self.assertIn("would open Milelik browser setup", output.getvalue())

    def test_browser_setup_form_applies_choices(self):
        args = setup.parse_args(["--browser-setup"])
        form = {
            "action": ["install"], "dependency_mode": ["none"],
            "model_source": ["auto"], "gguf": [""], "model": [""],
            "backend": ["auto"], "host": [""], "models_dir": [""],
            "model_file": [""], "model_discovery": ["1"],
            "performance_profile": ["fast"], "frontend_enabled": ["1"],
            "workspace": ["/tmp/milelik-ws"], "web_host": ["127.0.0.1"],
            "web_port": ["9876"], "open_browser": ["1"],
            "ui_theme": ["dark"], "ui_default_view": ["workspace"],
            "launch": ["gui"],
        }
        result = setup.apply_browser_setup_form(args, form)
        self.assertEqual(result["action"], "install")
        self.assertEqual(args.dependency_mode, "none")
        self.assertEqual(args.web_port, 9876)
        self.assertEqual(args.launch, "gui")
        self.assertEqual(result["env"]["PERFORMANCE_PROFILE"], "fast")
        self.assertEqual(result["env"]["UI_DEFAULT_VIEW"], "workspace")

    def test_browser_setup_http_page_uses_svg_and_collects_choices(self):
        args = setup.parse_args(["--browser-setup"])
        core = setup.load_core(ROOT)
        opened = []
        session = setup.start_browser_setup(
            args, core=core, root=ROOT,
            browser_open=lambda url: opened.append(url) or True)
        try:
            self.assertEqual(opened, [session.url])
            with urllib.request.urlopen(session.url, timeout=10) as response:
                page = response.read().decode()
                cookie = response.headers.get("Set-Cookie", "").split(";")[0]
            self.assertIn('class="milelik-sprite"', page)
            self.assertIn(
                'type="image/svg+xml" href="/sprites/milelik-icon.svg"', page)
            self.assertNotIn("<img", page.lower())
            self.assertNotIn(".png", page.lower())
            self.assertEqual(
                setup.load_sprite_svg(ROOT),
                (ROOT / "frontend" / "sprites" / "milelik-companion.svg").read_text())
            with urllib.request.urlopen(
                    session.url.split("?")[0].rstrip("/")
                    + "/sprites/milelik-icon.svg", timeout=10) as response:
                self.assertEqual(response.headers.get_content_type(),
                                 "image/svg+xml")
                self.assertEqual(
                    response.read().decode(), setup.load_app_icon_svg(ROOT))

            form = {
                "token": session.token, "action": "install",
                "dependency_mode": "none", "model_source": "auto",
                "backend": "auto", "model_discovery": "1",
                "performance_profile": "balanced", "frontend_enabled": "1",
                "workspace": "/tmp/milelik-ws", "web_host": "127.0.0.1",
                "web_port": "9876", "open_browser": "1",
                "ui_theme": "system", "ui_default_view": "chat",
                "launch": "gui",
            }
            request = urllib.request.Request(
                session.url.split("?")[0].rstrip("/") + "/setup",
                data=urllib.parse.urlencode(form).encode(),
                headers={"Cookie": cookie,
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(request, timeout=10) as response:
                progress = response.read().decode()
            self.assertIn("Setting up Milelik", progress)
            result = session.wait()
            self.assertEqual(result["action"], "install")
            self.assertEqual(args.web_port, 9876)

            request = urllib.request.Request(
                session.url.split("?")[0].rstrip("/") + "/status",
                headers={"Cookie": cookie})
            with urllib.request.urlopen(request, timeout=10) as response:
                status = json.load(response)
            self.assertEqual(status["phase"], "installing")
        finally:
            session.complete("complete", "Test complete", delay=0)

    def test_main_browser_setup_runs_installer_without_terminal_wizard(self):
        calls = []

        class FakeSession:
            def __init__(self, args):
                self.args = args
                self.completed = []

            def wait(self):
                self.args.dependency_mode = "none"
                self.args.web_port = 9876
                return {"action": "install",
                        "env": {"MODEL_SOURCE": "auto",
                                "UI_THEME": "dark"}}

            def set_phase(self, phase, message):
                self.completed.append((phase, message))

            def complete(self, phase, message, delay=1.0):
                self.completed.append((phase, message))

        session_holder = {}

        def starter(args, **_kwargs):
            session_holder["session"] = FakeSession(args)
            return session_holder["session"]

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = setup.main(
                ["--browser-setup", "--install-only"],
                runner=runner,
                stdin_isatty=False,
                stdout_isatty=False,
                installed_command=None,
                euid=1000,
                browser_setup_starter=starter,
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("--yes", calls[0][0])
        self.assertNotIn("--wizard", calls[0][0])
        self.assertEqual(calls[0][1]["env"]["UI_THEME"], "dark")
        self.assertEqual(calls[0][1]["env"]["MILELIK_BROWSER_SETUP_ACTIVE"], "1")
        self.assertEqual(session_holder["session"].completed[-1][0], "complete")

    def test_launch_commands_for_gui_cli_task_and_installed_binary(self):
        gui = setup.parse_args(["--run-only", "--source", "--web-port", "9999"])
        command = setup.build_launch_command(gui, root=ROOT, installed_command=None)
        self.assertEqual(command[1], str(ROOT / "milelik.py"))
        self.assertIn("--gui", command)
        self.assertIn("9999", command)

        cli = setup.parse_args(["--run-only", "--cli", "--choose-model"])
        self.assertTrue(cli.source)
        command = setup.build_launch_command(cli, root=ROOT, installed_command=None)
        self.assertIn("--choose-model", command)
        self.assertIn("--interactive", command)
        self.assertNotIn("--gui", command)

        installed = Path("/tmp/fake-home/.local/bin/milelik")
        task = setup.parse_args(["--task", "check my disk"])
        command = setup.build_launch_command(task, root=ROOT, installed_command=installed)
        self.assertEqual(command[0], str(installed))
        self.assertEqual(command[-1], "check my disk")

        prefix = ["/usr/bin/python3", "/tmp/fake-home/.local/share/milelik/milelik.py"]
        command = setup.build_launch_command(task, root=ROOT, installed_command=prefix)
        self.assertEqual(command[:2], prefix)

    def test_ensure_models_folder_and_installed_command_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            custom = Path(tmp) / "all-models"
            created = setup.ensure_models_folder(str(custom), root=root)
            self.assertEqual(created, custom)
            self.assertTrue(custom.is_dir())
            self.assertTrue((root / "models").is_dir())

            installed = Path(tmp) / ".local" / "bin" / "milelik"
            installed.parent.mkdir(parents=True)
            installed.write_text("#!/bin/sh\nexit 0\n")
            installed.chmod(0o755)
            self.assertEqual(setup.resolve_installed_command(Path(tmp)), installed)

            app = Path(tmp) / ".local" / "share" / "milelik" / "milelik.py"
            app.parent.mkdir(parents=True)
            app.write_text("# installed app\n")
            app.chmod(0o644)
            installed.chmod(0o644)
            self.assertEqual(
                setup.installed_launch_prefix(installed, home=Path(tmp),
                                               python_executable="/usr/bin/python3"),
                ["/usr/bin/python3", str(app)],
            )

    def test_dry_run_list_models_does_not_call_runner(self):
        def runner(*_args, **_kwargs):
            raise AssertionError("runner should not be called during a dry run")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = setup.main(
                ["--list-models", "--dry-run"],
                runner=runner,
                stdin_isatty=False,
                stdout_isatty=False,
                installed_command=None,
            )
        self.assertEqual(code, 0)
        self.assertIn("Milelik Python setup", output.getvalue())
        self.assertIn("--list-models", output.getvalue())

    def test_interactive_user_can_skip_install_and_run_from_source(self):
        answers = iter(["n", "y"])

        def runner(*_args, **_kwargs):
            raise AssertionError("runner should not be called during a dry run")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = setup.main(
                ["--dry-run"],
                runner=runner,
                input_fn=lambda _prompt: next(answers),
                stdin_isatty=True,
                stdout_isatty=True,
                installed_command=None,
            )
        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn("install_milelik.py", text)
        self.assertIn("milelik.py --gui", text)

    def test_installer_cancellation_prevents_launch(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            self.assertEqual(kwargs["env"]["MILELIK_WIZARD_CANCEL_EXIT"], "10")
            return SimpleNamespace(returncode=setup.INSTALLER_CANCELLED)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = setup.main(
                ["--yes"],
                runner=runner,
                stdin_isatty=False,
                stdout_isatty=False,
                installed_command=None,
                euid=1000,
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("install_milelik.py", " ".join(calls[0][0]))
        self.assertIn("Setup cancelled", output.getvalue())

    def test_root_is_rejected_with_python3_without_sudo_instructions(self):
        def runner(*_args, **_kwargs):
            raise AssertionError("runner should not be called for a root run")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = setup.main(
                ["--run-only", "--cli"],
                runner=runner,
                stdin_isatty=False,
                stdout_isatty=False,
                installed_command=None,
                euid=0,
            )
        self.assertEqual(code, 2)
        self.assertIn("do not run setup_milelik.py as root/sudo", stderr.getvalue())
        self.assertIn("python3 setup_milelik.py", stderr.getvalue())
        self.assertTrue(setup.parse_args(["--allow-root", "--dry-run"]).allow_root)

    def test_permission_error_explains_noexec_and_returns_126(self):
        def runner(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = setup.run_command(["/tmp/milelik"], runner=runner)
        self.assertEqual(code, 126)
        self.assertIn("noexec", stderr.getvalue())
        self.assertIn("sudo does not bypass", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
