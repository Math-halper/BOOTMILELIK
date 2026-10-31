"""Tests for Milelik's Python replacements for its former shell scripts."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dependencies = load_module(
    "milelik_install_dependencies", ROOT / "dependencies" / "install_dependencies.py")
setup_model = load_module("milelik_setup_model", ROOT / "setup_model.py")
uninstall = load_module("milelik_uninstall", ROOT / "uninstall_milelik.py")
installer = load_module("milelik_installer", ROOT / "install_milelik.py")
app = load_module("milelik_app_entrypoint", ROOT / "app.py")


class TestPythonReplacements(unittest.TestCase):
    def test_repository_has_no_shell_scripts_and_app_entrypoint_is_web(self):
        self.assertEqual(list(ROOT.rglob("*.sh")), [])
        import milelik_web
        self.assertIs(app.main, milelik_web.main)
        self.assertIn("zstd", dependencies.read_packages("required"))
        self.assertIn("zstd", dependencies.expected_commands("required"))

    def test_model_setup_imports_gguf_through_python_core(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append([str(part) for part in command])
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gguf = Path(tmp) / setup_model.TARGET_FILE
            gguf.write_bytes(b"GGUF")
            core = SimpleNamespace(
                ollama_name_for_model_file=lambda _path: "milelik-smollm2:135m")
            code = setup_model.main(
                [str(gguf)], runner=runner, which=lambda _name: "/fake/ollama",
                root=root, core=core)

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], sys.executable)
        self.assertIn("--import-gguf", calls[0])
        self.assertIn("milelik-smollm2:135m", calls[0])
        self.assertIn("--check", calls[1])

    def test_model_setup_download_fallback_saves_ollama_model(self):
        calls = []
        saved = {}

        def runner(command, **_kwargs):
            calls.append([str(part) for part in command])
            return SimpleNamespace(returncode=0)

        def update_config(**kwargs):
            saved.update(kwargs)
            return Path("/tmp/milelik-config.json")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            home = base / "home"
            cwd = base / "cwd"
            for path in (root, home, cwd):
                path.mkdir()
            core = SimpleNamespace(update_config=update_config)
            code = setup_model.main(
                ["--download"], runner=runner,
                which=lambda _name: "/fake/ollama", root=root, home=home,
                cwd=cwd, core=core, ready=lambda: True, sleep=lambda _s: None)

        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ["/fake/ollama", "pull", "smollm2:135m"])
        self.assertEqual(saved["model"], "smollm2:135m")
        self.assertIn("--check", calls[1])

    def test_uninstall_preserves_installed_models_and_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app_dir = home / ".local" / "share" / "milelik"
            model = app_dir / "models" / "keep.gguf"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"GGUF")
            (app_dir / "app.py").write_text("")
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "milelik").write_text("")
            (bin_dir / "milelik-app").write_text("")
            config_dir = home / ".config" / "milelik"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text("{}")
            workspace_file = home / "MilelikWorkspace" / "keep.txt"
            workspace_file.parent.mkdir()
            workspace_file.write_text("keep")

            preserved = uninstall.uninstall(home=home)

            self.assertEqual(preserved, app_dir / "models")
            self.assertTrue(model.is_file())
            self.assertTrue(workspace_file.is_file())
            self.assertFalse((app_dir / "app.py").exists())
            self.assertFalse((bin_dir / "milelik").exists())
            self.assertFalse((bin_dir / "milelik-app").exists())
            self.assertFalse(config_dir.exists())

    def test_direct_installer_opens_browser_setup_on_desktop(self):
        options = installer.InstallerOptions(raw_argv=())
        self.assertTrue(installer.should_open_browser_setup(
            options, {"DISPLAY": ":0"}, stdin_isatty=False,
            stdout_isatty=False))
        self.assertFalse(installer.should_open_browser_setup(
            options, {}, stdin_isatty=False, stdout_isatty=False))


if __name__ == "__main__":
    unittest.main()
