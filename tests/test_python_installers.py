"""Tests for Milelik's Python replacements for its former shell scripts."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "ollama") not in sys.path:
    sys.path.insert(0, str(ROOT / "ollama"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dependencies = load_module(
    "milelik_install_dependencies", ROOT / "dependencies" / "install_dependencies.py")
setup_ollama = load_module(
    "milelik_setup_ollama", ROOT / "ollama" / "setup_ollama.py")
fetch_ollama = load_module(
    "milelik_fetch_ollama", ROOT / "ollama" / "fetch_ollama.py")
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


class TestOllamaRuntime(unittest.TestCase):
    def test_pinned_manifest_covers_x86_and_arm(self):
        manifest = setup_ollama.load_manifest()
        self.assertTrue(manifest["pinned_version"].startswith("v"))
        self.assertIn(manifest["pinned_version"], manifest["download_base"])
        architectures = manifest["architectures"]
        self.assertEqual(architectures["amd64"]["asset"],
                         "ollama-linux-amd64.tar.zst")
        self.assertEqual(architectures["arm64"]["asset"],
                         "ollama-linux-arm64.tar.zst")
        for name in ("x86_64", "amd64", "x64", "intel64"):
            self.assertIn(name, architectures["amd64"]["platform_machine"])
        self.assertIn("i686", manifest["unsupported"]["platform_machine"])
        self.assertIn("64-bit", manifest["unsupported"]["reason"])
        self.assertTrue((ROOT / "ollama" / "README.md").is_file())

    def test_x86_cpus_resolve_to_amd64_asset(self):
        manifest = setup_ollama.load_manifest()
        for machine in ("x86_64", "amd64", "x64", "intel64", "X86_64"):
            arch, asset, _ = setup_ollama.resolve_arch(machine, manifest)
            self.assertEqual(arch, "amd64")
            self.assertEqual(asset, "ollama-linux-amd64.tar.zst")
        arch, asset, _ = setup_ollama.resolve_arch("aarch64", manifest)
        self.assertEqual((arch, asset), ("arm64", "ollama-linux-arm64.tar.zst"))
        with self.assertRaisesRegex(setup_ollama.OllamaSetupError, "64-bit"):
            setup_ollama.resolve_arch("i686", manifest)
        with self.assertRaises(setup_ollama.OllamaSetupError):
            setup_ollama.resolve_arch("sparc", manifest)

    def test_download_urls_come_from_pinned_manifest(self):
        manifest = setup_ollama.load_manifest()
        _arch, asset, _ = setup_ollama.resolve_arch("x86_64", manifest)
        url = setup_ollama.download_url(manifest, asset)
        self.assertTrue(url.startswith(manifest["download_base"]))
        self.assertTrue(url.endswith("ollama-linux-amd64.tar.zst"))
        self.assertTrue(setup_ollama.checksum_url(manifest).endswith(
            manifest["checksum_file"]))

    def test_installer_prefers_pinned_manifest_over_latest(self):
        manifest = setup_ollama.load_manifest()
        base, source = installer.ollama_download_base({}, root=ROOT)
        self.assertEqual(base, manifest["download_base"])
        self.assertIn("ollama.json", source)
        base, _source = installer.ollama_download_base(
            {"OLLAMA_DOWNLOAD_BASE": "https://mirror.example/ollama/"}, root=ROOT)
        self.assertEqual(base, "https://mirror.example/ollama")
        with tempfile.TemporaryDirectory() as tmp:
            base, _source = installer.ollama_download_base({}, root=Path(tmp))
            self.assertIn("releases/latest/download", base)

    def test_install_verifies_checksum_and_extracts(self):
        import hashlib

        manifest = setup_ollama.load_manifest()
        archive_bytes = b"fake-ollama-archive"
        digest = hashlib.sha256(archive_bytes).hexdigest()
        downloaded = []

        def downloader(url, destination, opener=None):
            downloaded.append(url)
            if url.endswith(manifest["checksum_file"]):
                destination.write_text(
                    f"{digest}  ollama-linux-amd64.tar.zst\n", encoding="utf-8")
            else:
                destination.write_bytes(archive_bytes)

        def extractor(_archive, destination):
            executable = destination / "bin" / "ollama"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\n")

        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            executable = setup_ollama.install_ollama(
                prefix=prefix, manifest=manifest, arch="amd64",
                downloader=downloader, extractor=extractor)
            self.assertEqual(executable, prefix / "bin" / "ollama")
            self.assertTrue(executable.is_file())
            self.assertEqual(len(downloaded), 2)

            def bad_downloader(url, destination, opener=None):
                if url.endswith(manifest["checksum_file"]):
                    destination.write_text(
                        f"{'0' * 64}  ollama-linux-amd64.tar.zst\n",
                        encoding="utf-8")
                else:
                    destination.write_bytes(archive_bytes)

            with self.assertRaises(setup_ollama.OllamaSetupError):
                setup_ollama.install_ollama(
                    prefix=prefix, manifest=manifest, arch="amd64",
                    downloader=bad_downloader, extractor=extractor)

    def test_find_ollama_checks_user_local_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            executable = local_bin / "ollama"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            self.assertEqual(setup_ollama.find_ollama(
                which=lambda _name: None, home=home), str(executable))
            executable.unlink()
            self.assertEqual(setup_ollama.find_ollama(
                which=lambda _name: None, home=home), "")

    def test_split_and_assemble_roundtrip_with_verification(self):
        data = os.urandom(500)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "ollama-linux-amd64.tar.zst"
            source.write_bytes(data)
            vendor_dir = base / "vendor"
            split = setup_ollama.split_file(source, vendor_dir, chunk_size=100)
            self.assertEqual(len(split["chunks"]), 5)
            self.assertEqual(split["total_size"], 500)
            vendor = {"asset": source.name, "vendor_dir": "vendor",
                      "total_size": split["total_size"], "sha256": split["sha256"],
                      "chunks": split["chunks"]}
            self.assertTrue(setup_ollama.vendor_is_valid(vendor_dir, vendor))
            assembled = base / "assembled.tar.zst"
            setup_ollama.assemble_archive(vendor_dir, vendor, assembled)
            self.assertEqual(assembled.read_bytes(), data)

            tampered = vendor_dir / split["chunks"][2]["name"]
            original = tampered.read_bytes()
            tampered.write_bytes(b"X" + original[1:])
            self.assertFalse(setup_ollama.vendor_is_valid(vendor_dir, vendor))
            with self.assertRaises(setup_ollama.OllamaSetupError):
                setup_ollama.assemble_archive(vendor_dir, vendor, assembled)
            tampered.write_bytes(original)

            tampered.unlink()
            with self.assertRaisesRegex(setup_ollama.OllamaSetupError, "missing"):
                setup_ollama.assemble_archive(vendor_dir, vendor, assembled)
            tampered.write_bytes(original)

            bad_vendor = dict(vendor, sha256="0" * 64)
            with self.assertRaises(setup_ollama.OllamaSetupError):
                setup_ollama.assemble_archive(vendor_dir, bad_vendor, assembled)

    def _write_vendor_fixture(self, base: Path) -> tuple[dict, Path, bytes]:
        manifest = setup_ollama.load_manifest()
        asset = manifest["architectures"]["amd64"]["asset"]
        payload = b"fake-vendored-ollama" * 50
        source = base / asset
        source.write_bytes(payload)
        split = setup_ollama.split_file(source, base / "vendor", chunk_size=100)
        vendor = {"asset": asset, "ollama_arch": "amd64",
                  "pinned_version": manifest["pinned_version"],
                  "vendor_dir": "vendor", "total_size": split["total_size"],
                  "sha256": split["sha256"], "chunks": split["chunks"]}
        vendor_json = base / "vendor.json"
        vendor_json.write_text(json.dumps(vendor), encoding="utf-8")
        return manifest, vendor_json, payload

    def test_install_uses_vendored_chunks_without_network(self):
        def boom(url, destination, opener=None):
            raise AssertionError(f"network used in vendored test: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manifest, vendor_json, payload = self._write_vendor_fixture(base)
            seen = {}

            def extractor(archive, destination):
                seen["bytes"] = archive.read_bytes()
                executable = destination / "bin" / "ollama"
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text("#!/bin/sh\n")

            for offline in (False, True):
                prefix = base / f"prefix-{offline}"
                executable = setup_ollama.install_ollama(
                    prefix=prefix, manifest=manifest, arch="amd64",
                    offline=offline, vendor_manifest_path=vendor_json,
                    downloader=boom, extractor=extractor)
                self.assertEqual(executable, prefix / "bin" / "ollama")
                self.assertTrue(executable.is_file())
            self.assertEqual(seen["bytes"], payload)

    def test_offline_without_vendored_chunks_fails_fast(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("network/extractor used in offline test")

        manifest = setup_ollama.load_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(setup_ollama.OllamaSetupError,
                                        "Offline mode"):
                setup_ollama.install_ollama(
                    prefix=Path(tmp) / "prefix", manifest=manifest,
                    arch="amd64", offline=True,
                    vendor_manifest_path=Path(tmp) / "missing.json",
                    downloader=boom, extractor=boom)

    def test_find_vendored_missing_and_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(setup_ollama.find_vendored(
                Path(tmp) / "missing.json"))
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(setup_ollama.OllamaSetupError):
                setup_ollama.find_vendored(bad)

    def test_main_installer_detects_matching_vendored_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _manifest, vendor_json, _payload = self._write_vendor_fixture(base)
            vendor = json.loads(vendor_json.read_text(encoding="utf-8"))
            self.assertTrue(setup_ollama.vendor_is_valid(base / "vendor", vendor))
            self.assertEqual(
                vendor["asset"],
                setup_ollama.load_manifest()["architectures"]["amd64"]["asset"])

    def test_fetch_helpers_manage_vendor_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vendor_dir = base / "vendor"
            vendor_dir.mkdir()
            (vendor_dir / "a.tar.zst.part00").write_bytes(b"old")
            (vendor_dir / ".gitkeep").write_text("keep\n")
            fetch_ollama.clear_vendor_dir(vendor_dir)
            self.assertEqual(sorted(p.name for p in vendor_dir.iterdir()),
                             [".gitkeep"])
            split = {"total_size": 3, "sha256": "abc",
                     "chunks": [{"name": "x.part00", "size": 3, "sha256": "abc"}]}
            vendor_json = base / "vendor.json"
            vendor = fetch_ollama.write_vendor_manifest(
                vendor_json, asset="x", ollama_arch="amd64",
                pinned_version="v9", chunk_size=90 * 1024 * 1024, split=split)
            self.assertEqual(vendor["asset"], "x")
            self.assertEqual(len(vendor["chunks"]), 1)
            stored = json.loads(vendor_json.read_text(encoding="utf-8"))
            self.assertEqual(stored["pinned_version"], "v9")

    def test_fetch_parser_defaults(self):
        options = fetch_ollama.build_parser().parse_args([])
        self.assertEqual(options.chunk_mb, 90.0)
        self.assertFalse(options.force)
        self.assertTrue(options.vendor_dir.endswith("vendor"))


class TestInstallerOffline(unittest.TestCase):
    def test_find_ollama_checks_home_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            candidate = home / "bin" / "ollama"
            candidate.parent.mkdir(parents=True)
            candidate.write_text("#!/bin/sh\n")
            candidate.chmod(0o755)
            self.assertEqual(
                installer.find_ollama(which=lambda _name: None, home=home),
                str(candidate))

    def test_find_ollama_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                installer.find_ollama(which=lambda _name: None, home=Path(tmp)),
                "")

    def test_parse_args_offline_flag_and_env(self):
        options = installer.parse_args(["--offline"], environ={}, home=Path("/tmp"))
        self.assertTrue(options.offline)
        options = installer.parse_args(
            [], environ={"MILELIK_OFFLINE": "1"}, home=Path("/tmp"))
        self.assertTrue(options.offline)
        options = installer.parse_args([], environ={}, home=Path("/tmp"))
        self.assertFalse(options.offline)

    def test_ensure_model_offline_refuses_runtime_download(self):
        calls = []
        pulls = []

        def fake_installer(*, home, environ):
            calls.append((home, environ))
            return home / ".local" / "bin" / "ollama"

        def fake_runner(command, **_kwargs):
            pulls.append(list(command))
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            options = installer.InstallerOptions(
                model="tiny:test", offline=True, model_was_set=True)
            code = installer.ensure_model(
                options, root=Path(tmp), home=Path(tmp),
                runner=fake_runner, which=lambda _name: None, env={},
                ready=lambda: True, ollama_installer=fake_installer)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual([cmd for cmd in pulls if "pull" in cmd], [])

    def test_ensure_model_offline_refuses_model_pull(self):
        pulls = []

        def fake_runner(command, **_kwargs):
            pulls.append(list(command))
            return SimpleNamespace(returncode=0)

        def fake_which(name):
            return "/fake/ollama" if name == "ollama" else None

        with tempfile.TemporaryDirectory() as tmp:
            options = installer.InstallerOptions(
                model="tiny:test", offline=True, model_was_set=True)
            code = installer.ensure_model(
                options, root=Path(tmp), home=Path(tmp),
                runner=fake_runner, which=fake_which, env={},
                ready=lambda: True)
        self.assertEqual(code, 1)
        self.assertEqual([cmd for cmd in pulls if "pull" in cmd], [])


if __name__ == "__main__":
    unittest.main()
