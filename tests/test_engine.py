"""Tests for Milelik's remade bundled CPU engine (engine/).

Covers the vendored llama-server binary, its provenance manifest, the
Ollama-compatibility translators and the serve.py launcher helpers. All
tests are offline and fast: nothing binds a port or loads the model here
(run `python3 engine/serve.py --check` for the live end-to-end proof).
"""
import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "engine"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compat = load_module("milelik_engine_compat", ROOT / "engine" / "ollama_compat.py")
serve = load_module("milelik_engine_serve", ROOT / "engine" / "serve.py")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TestEngineBinary(unittest.TestCase):
    def test_provenance_manifest_matches_binary(self):
        manifest = json.loads((ROOT / "engine" / "ENGINE.json").read_text(
            encoding="utf-8"))
        self.assertEqual(manifest["upstream"],
                         "https://github.com/ggml-org/llama.cpp")
        self.assertTrue(manifest["tag"])
        self.assertTrue(manifest["commit"])
        self.assertTrue((ROOT / "engine" / "README.md").is_file())
        binary = ROOT / "engine" / "llama-server"
        self.assertTrue(binary.is_file(), "engine/llama-server must be vendored")
        self.assertTrue(binary.stat().st_mode & stat.S_IXUSR,
                        "engine binary must keep its executable bit in git")
        self.assertEqual(binary.stat().st_size,
                         manifest["binary"]["size_bytes"])
        self.assertEqual(sha256_of(binary), manifest["binary"]["sha256"])

    def test_binary_reports_version(self):
        completed = subprocess.run(
            [str(ROOT / "engine" / "llama-server"), "--version"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue((completed.stdout + completed.stderr).strip())


class TestOllamaCompatTranslations(unittest.TestCase):
    def test_model_names_mirror_milelik(self):
        self.assertEqual(
            compat.ollama_name_for_model_file("smollm2-135m-instruct-q4_1.gguf"),
            "milelik-smollm2:135m")
        self.assertEqual(
            compat.ollama_name_for_model_file(
                "Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf"),
            "milelik-coder:7b")
        self.assertEqual(
            compat.ollama_name_for_model_file("My Fancy Model.Q4.gguf"),
            "my-fancy-model-q4:latest")
        names = compat.advertised_names("smollm2-135m-instruct-q4_1.gguf")
        self.assertIn("milelik-smollm2:135m", names)
        tags = compat.tags_response(names)
        self.assertEqual([m["name"] for m in tags["models"]], names)

    def test_messages_fold_tool_turns_into_text(self):
        out = compat.translate_messages([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking",
             "tool_calls": [{"function": {"name": "run_command",
                                          "arguments": {"command": "ls"}}}]},
            {"role": "tool", "tool_name": "run_command", "content": "out"},
            {"role": "weird", "content": "x"},
        ])
        self.assertEqual([m["role"] for m in out],
                         ["system", "user", "assistant", "user", "user"])
        self.assertIn("run_command", out[2]["content"])
        self.assertIn("[tool result from run_command]", out[3]["content"])
        for message in out:
            self.assertNotIn("tool_calls", message)

    def test_chat_request_maps_options(self):
        body = compat.translate_chat_request({
            "model": "milelik-smollm2:135m", "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
            "options": {"temperature": 0.1, "num_ctx": 8192, "top_p": 0.8,
                        "top_k": 20, "repeat_penalty": 1.05,
                        "num_predict": 64},
            "tools": [{"type": "function"}]})
        self.assertEqual(body["model"], "milelik-smollm2:135m")
        self.assertFalse(body["stream"])
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual(body["top_p"], 0.8)
        self.assertEqual(body["top_k"], 20)
        self.assertEqual(body["repeat_penalty"], 1.05)
        self.assertEqual(body["max_tokens"], 64)
        self.assertNotIn("num_ctx", body)
        self.assertEqual(body["tools"], [{"type": "function"}])
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])

    def test_chat_response_maps_to_ollama_shape(self):
        reply = compat.translate_chat_response(
            {"choices": [{"message": {"role": "assistant", "content": "hello"},
                          "finish_reason": "stop"}]},
            "milelik-smollm2:135m")
        self.assertEqual(reply["model"], "milelik-smollm2:135m")
        self.assertEqual(reply["message"],
                         {"role": "assistant", "content": "hello"})
        self.assertTrue(reply["done"])
        self.assertEqual(reply["done_reason"], "stop")
        cut = compat.translate_chat_response(
            {"choices": [{"message": {"content": "half"},
                          "finish_reason": "length"}]}, "m")
        self.assertEqual(cut["done_reason"], "length")


class TestServeHelpers(unittest.TestCase):
    def test_find_model_defaults_to_bundled_gguf(self):
        found = serve.find_model("")
        self.assertEqual(found.name, "smollm2-135m-instruct-q4_1.gguf")
        self.assertTrue(found.is_file())
        with self.assertRaises(serve.EngineError):
            serve.find_model("/nonexistent/model.gguf")

    def test_llama_command_targets_localhost(self):
        command = serve.build_llama_command(
            Path("/tmp/m.gguf"), port=8080, ctx=4096)
        self.assertEqual(command[0], str(ROOT / "engine" / "llama-server"))
        self.assertIn("127.0.0.1", command)
        self.assertNotIn("0.0.0.0", command)
        self.assertIn("4096", command)


if __name__ == "__main__":
    unittest.main()
