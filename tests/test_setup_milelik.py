"""Tests for Milelik's Python setup-and-run script."""
import contextlib
import importlib.util
import io
import sys
from types import SimpleNamespace
import tempfile
import unittest
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
        self.assertEqual(command[:2], ["bash", str(ROOT / "install.sh")])
        self.assertIn("--yes", command)
        launch_index = command.index("--launch")
        self.assertEqual(command[launch_index + 1], "none")
        self.assertIn("--skip-check", command)
        self.assertIn("/tmp/models", command)
        self.assertIn("Other.gguf", command)
        self.assertIn("9876", command)

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
        self.assertNotIn("install.sh", text)
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
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("install.sh", " ".join(calls[0][0]))
        self.assertIn("Setup cancelled", output.getvalue())


if __name__ == "__main__":
    unittest.main()
