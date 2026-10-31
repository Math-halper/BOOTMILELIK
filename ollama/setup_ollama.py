#!/usr/bin/env python3
"""Install Milelik's pinned Ollama runtime on linux.

Ollama is Milelik's default local model server. The 1.4 GB runtime archive
is too large to store in Git, so this repository pins the exact tested
release in ``ollama/ollama.json`` and this script downloads, verifies
(SHA-256) and extracts it without running any upstream shell script.

Typical use on an x86 PC (Intel or AMD 64-bit)::

    python3 ollama/setup_ollama.py
    python3 ollama/setup_ollama.py --check

Standard library only; no third-party packages needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

MANIFEST_NAME = "ollama.json"
MANIFEST_PATH = Path(__file__).resolve().parent / MANIFEST_NAME
VENDOR_MANIFEST_NAME = "vendor.json"
VENDOR_DIR_NAME = "vendor"
VENDOR_MANIFEST_PATH = Path(__file__).resolve().parent / VENDOR_MANIFEST_NAME
# 90 MiB chunks stay safely under GitHub's 100 MB single-file limit, so the
# ~1.4 GB Ollama archive can live in ordinary Git with no LFS dependency.
DEFAULT_CHUNK_SIZE = 90 * 1024 * 1024
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"


class OllamaSetupError(ValueError):
    """The requested Ollama setup cannot proceed on this machine."""


def load_manifest(path: str | Path = MANIFEST_PATH) -> dict:
    """Read the pinned Ollama release manifest shipped in this repository."""
    manifest_path = Path(path)
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise OllamaSetupError(f"Cannot read Ollama manifest: {manifest_path} ({error})")
    except json.JSONDecodeError as error:
        raise OllamaSetupError(
            f"Ollama manifest is not valid JSON: {manifest_path} ({error})")


def resolve_arch(machine: str | None = None, manifest: dict | None = None) -> tuple[str, str, str]:
    """Map ``platform.machine()`` to ``(ollama_arch, asset, description)``.

    x86_64 Intel and AMD desktops both use Ollama's ``amd64`` asset:
    ``amd64`` is simply the linux file-name for 64-bit x86.
    """
    manifest = load_manifest() if manifest is None else manifest
    name = str(platform.machine() if machine is None else machine).strip().lower()
    for ollama_arch, entry in (manifest.get("architectures") or {}).items():
        if name in [str(item).lower() for item in entry.get("platform_machine", [])]:
            return ollama_arch, entry["asset"], entry.get("description", "")
    unsupported = manifest.get("unsupported") or {}
    if name in [str(item).lower() for item in unsupported.get("platform_machine", [])]:
        raise OllamaSetupError(
            f"Your CPU reports '{name}': {unsupported.get('reason', 'unsupported CPU.')}")
    raise OllamaSetupError(
        f"Unsupported CPU for automatic Ollama installation: '{name}'. "
        "Ollama publishes linux builds for x86_64 (amd64) and aarch64 (arm64) only.")


def download_url(manifest: dict, asset: str) -> str:
    """Full download URL for a release asset from the pinned manifest."""
    return f"{str(manifest['download_base']).rstrip('/')}/{asset}"


def checksum_url(manifest: dict) -> str:
    """Full download URL for the release's published checksum file."""
    return download_url(manifest, manifest.get("checksum_file", "sha256sum.txt"))


def load_vendor_manifest(path: str | Path = VENDOR_MANIFEST_PATH) -> dict:
    """Read vendor.json describing the vendored ``ollama/vendor/`` chunks."""
    manifest_path = Path(path)
    try:
        vendor = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise OllamaSetupError(
            f"Cannot read vendored-Ollama manifest: {manifest_path} ({error})")
    except json.JSONDecodeError as error:
        raise OllamaSetupError(
            f"Vendored-Ollama manifest is not valid JSON: {manifest_path} ({error})")
    if not isinstance(vendor, dict) or not vendor.get("chunks"):
        raise OllamaSetupError(
            f"Vendored-Ollama manifest lists no chunks: {manifest_path}")
    return vendor


def find_vendored(vendor_manifest_path: str | Path | None = None,
                  ) -> tuple[Path, dict] | None:
    """Return ``(vendor_dir, manifest)`` when chunks are vendored, else None."""
    manifest_path = Path(vendor_manifest_path) if vendor_manifest_path else VENDOR_MANIFEST_PATH
    if not manifest_path.is_file():
        return None
    vendor = load_vendor_manifest(manifest_path)
    vendor_dir = manifest_path.parent / str(vendor.get("vendor_dir") or VENDOR_DIR_NAME)
    if not vendor_dir.is_dir():
        return None
    return vendor_dir, vendor


def chunk_is_valid(part: Path, entry: dict) -> bool:
    """True when a chunk file matches its manifest size and SHA-256."""
    try:
        if part.stat().st_size != entry.get("size"):
            return False
    except OSError:
        return False
    digest = hashlib.sha256()
    try:
        with part.open("rb") as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(data)
    except OSError:
        return False
    return digest.hexdigest() == entry.get("sha256")


def vendor_is_valid(vendor_dir: str | Path, vendor: dict) -> bool:
    """True when every vendored chunk exists and matches vendor.json."""
    chunks = vendor.get("chunks") or []
    if not chunks:
        return False
    base = Path(vendor_dir)
    return all(chunk_is_valid(base / str(entry.get("name", "")), entry)
               for entry in chunks)


def split_file(source: str | Path, vendor_dir: str | Path,
               chunk_size: int = DEFAULT_CHUNK_SIZE) -> dict:
    """Split ``source`` into ``<name>.partNN`` files under ``vendor_dir``.

    Returns the ``{"chunks", "total_size", "sha256"}`` description that
    ``fetch_ollama.py`` stores in vendor.json.
    """
    if chunk_size <= 0:
        raise OllamaSetupError(f"Chunk size must be positive: {chunk_size}")
    source_path = Path(source)
    target_dir = Path(vendor_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[dict] = []
    total_digest = hashlib.sha256()
    total_size = 0
    with source_path.open("rb") as stream:
        index = 0
        while True:
            name = f"{source_path.name}.part{index:02d}"
            target = target_dir / name
            digest = hashlib.sha256()
            size = 0
            with target.open("wb") as output:
                remaining = chunk_size
                while remaining > 0:
                    data = stream.read(min(1024 * 1024, remaining))
                    if not data:
                        break
                    output.write(data)
                    digest.update(data)
                    total_digest.update(data)
                    size += len(data)
                    total_size += len(data)
                    remaining -= len(data)
            if size == 0:
                target.unlink()
                break
            chunks.append({"name": name, "size": size,
                           "sha256": digest.hexdigest()})
            index += 1
    if not chunks:
        raise OllamaSetupError(f"Cannot vendor an empty file: {source_path}")
    return {"chunks": chunks, "total_size": total_size,
            "sha256": total_digest.hexdigest()}


def assemble_archive(vendor_dir: str | Path, vendor: dict,
                     destination: str | Path) -> Path:
    """Concatenate verified ``vendor/`` chunks into ``destination``.

    Every chunk's SHA-256 plus the whole archive's SHA-256 and size are
    checked; any mismatch raises OllamaSetupError and nothing is installed.
    """
    chunks = vendor.get("chunks") or []
    if not chunks:
        raise OllamaSetupError("Vendored Ollama manifest lists no chunks.")
    base = Path(vendor_dir)
    target = Path(destination)
    total_digest = hashlib.sha256()
    total_size = 0
    with target.open("wb") as output:
        for entry in chunks:
            part = base / str(entry.get("name", ""))
            if not part.is_file():
                raise OllamaSetupError(
                    f"Vendored Ollama chunk is missing: {part.name}. Re-run "
                    "`python3 ollama/fetch_ollama.py` on a machine with internet.")
            digest = hashlib.sha256()
            size = 0
            with part.open("rb") as stream:
                for data in iter(lambda: stream.read(1024 * 1024), b""):
                    output.write(data)
                    digest.update(data)
                    total_digest.update(data)
                    size += len(data)
            if size != entry.get("size") or digest.hexdigest() != entry.get("sha256"):
                raise OllamaSetupError(
                    f"Vendored Ollama chunk failed verification: {part.name}. Re-run "
                    "`python3 ollama/fetch_ollama.py` on a machine with internet.")
            total_size += size
    if total_size != vendor.get("total_size") \
            or total_digest.hexdigest() != vendor.get("sha256"):
        raise OllamaSetupError(
            "Assembled Ollama archive failed verification; refusing to install.")
    return target


def find_ollama(which=shutil.which, home: Path | None = None) -> str:
    """Locate an installed ``ollama`` executable, or ``\"\"`` if none exists."""
    found = which("ollama")
    if found:
        return str(found)
    home = Path(home) if home is not None else Path.home()
    for candidate in (home / ".local" / "bin" / "ollama",
                      Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def ollama_api_ready(opener=urllib.request.urlopen) -> bool:
    """True when an Ollama server answers on 127.0.0.1:11434."""
    try:
        with opener(OLLAMA_TAGS_URL, timeout=2) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def sha256_of(path: Path) -> str:
    """SHA-256 hex digest of a file, streamed so large archives fit in RAM."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_to_file(url: str, destination: Path, opener=urllib.request.urlopen) -> None:
    """Download a URL to a file. Only used for Ollama's release assets."""
    request = urllib.request.Request(url, headers={"User-Agent": "Milelik-Ollama-Setup/1.0"})
    with opener(request, timeout=180) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def verify_archive(archive: Path, sums_text: str, asset: str) -> None:
    """Raise OllamaSetupError unless the archive matches sha256sum.txt."""
    expected = ""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and Path(parts[-1]).name == asset:
            expected = parts[0].lower()
            break
    if not expected:
        raise OllamaSetupError(
            f"No SHA-256 entry for {asset} in the release checksum file; refusing to install.")
    actual = sha256_of(archive)
    if actual != expected:
        raise OllamaSetupError(
            f"The downloaded {asset} failed its SHA-256 check; refusing to install.")


def extract_archive(archive: Path, destination: Path,
                    runner=subprocess.run, starter=subprocess.Popen) -> None:
    """Extract a .tar.zst Ollama archive with the system zstd/tar commands."""
    if not shutil.which("zstd") or not shutil.which("tar"):
        raise OllamaSetupError(
            "Installing Ollama needs the zstd and tar commands. "
            "Install Milelik's required dependencies first, then try again.")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "bin").mkdir(parents=True, exist_ok=True)
    old_lib = destination / "lib" / "ollama"
    if old_lib.exists():
        shutil.rmtree(old_lib)
    (destination / "lib" / "ollama").mkdir(parents=True, exist_ok=True)
    decompressor = starter(["zstd", "-dc", str(archive)], stdout=subprocess.PIPE)
    if decompressor.stdout is None:
        raise OllamaSetupError("Could not start zstd to decompress the Ollama archive.")
    try:
        extract = runner(["tar", "-xf", "-", "-C", str(destination)],
                         stdin=decompressor.stdout, check=False)
    finally:
        decompressor.stdout.close()
    if decompressor.wait() or extract.returncode:
        raise OllamaSetupError("Could not extract the Ollama release archive.")


def install_ollama(prefix: Path | None = None, home: Path | None = None,
                   manifest: dict | None = None, arch: str | None = None,
                   verify: bool = True, offline: bool = False,
                   allow_vendored: bool = True,
                   vendor_manifest_path: str | Path | None = None,
                   opener=urllib.request.urlopen,
                   downloader=download_to_file,
                   extractor=extract_archive) -> Path:
    """Install the pinned Ollama release from vendored chunks or download.

    Vendored ``ollama/vendor/`` chunks are preferred and need no network;
    otherwise the release is downloaded. With ``offline=True`` downloads are
    forbidden. Returns the installed ``ollama`` executable path. Raises
    OllamaSetupError (or URLError/OSError for network faults) on failure.
    """
    manifest = load_manifest() if manifest is None else manifest
    if arch is None:
        ollama_arch, asset, _description = resolve_arch(manifest=manifest)
    else:
        ollama_arch = arch
        try:
            asset = manifest["architectures"][arch]["asset"]
        except KeyError:
            raise OllamaSetupError(
                f"Unknown Ollama architecture '{arch}'. "
                f"Available: {', '.join((manifest.get('architectures') or {}))}.")
    home = Path(home) if home is not None else Path.home()
    destination = Path(prefix) if prefix is not None else home / ".local"

    vendored = find_vendored(vendor_manifest_path) if allow_vendored else None
    if vendored is not None and vendored[1].get("asset") != asset:
        if offline:
            raise OllamaSetupError(
                f"Offline mode, but ollama/vendor/ holds {vendored[1].get('asset')} "
                f"while this PC needs {asset}. Re-run "
                "`python3 ollama/fetch_ollama.py` on a machine with internet.")
        vendored = None

    with tempfile.TemporaryDirectory(prefix="milelik-ollama-") as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / asset
        if vendored is not None:
            vendor_dir, vendor = vendored
            print(f"Assembling vendored Ollama {vendor.get('pinned_version', '')} "
                  f"for linux/{ollama_arch} (offline, no download)…")
            assemble_archive(vendor_dir, vendor, archive)
            print("Vendored Ollama chunks verified (SHA-256).")
        else:
            if offline:
                raise OllamaSetupError(
                    "Offline mode and no vendored Ollama for this PC in "
                    "ollama/vendor/. Run `python3 ollama/fetch_ollama.py` on a "
                    "machine with internet first.")
            print(f"Downloading Ollama {manifest.get('pinned_version', '')} "
                  f"for linux/{ollama_arch} ({asset})…")
            print(f"From: {download_url(manifest, asset)}")
            downloader(download_url(manifest, asset), archive, opener=opener)
            if verify:
                sums_path = tmp_dir / manifest.get("checksum_file", "sha256sum.txt")
                downloader(checksum_url(manifest), sums_path, opener=opener)
                verify_archive(
                    archive, sums_path.read_text(encoding="utf-8", errors="replace"),
                    asset)
                print("SHA-256 verified against the release checksum file.")
            else:
                print("Warning: checksum verification skipped (--no-verify).")
        print(f"Extracting to {destination} …")
        extractor(archive, destination)

    executable = destination / "bin" / "ollama"
    if not executable.is_file():
        raise OllamaSetupError(
            f"Ollama executable was not found after extraction: {executable}")
    executable.chmod(0o755)
    return executable


def check_command(home: Path | None = None, which=shutil.which,
                  ready=ollama_api_ready) -> int:
    """Report the installed Ollama executable, version and server state."""
    ollama = find_ollama(which=which, home=home)
    if not ollama:
        print("Ollama is not installed (no 'ollama' on PATH, ~/.local/bin, "
              "/usr/local/bin or /usr/bin).")
        print("Install it with: python3 ollama/setup_ollama.py")
        return 1
    print(f"Ollama executable: {ollama}")
    try:
        completed = subprocess.run([ollama, "--version"], check=False,
                                   capture_output=True, text=True, timeout=15)
        version = (completed.stdout or completed.stderr or "").strip()
        print(f"Ollama version: {version or '(unknown)'}" if completed.returncode == 0
              else "Ollama version: (could not run --version)")
    except (OSError, subprocess.SubprocessError):
        print("Ollama version: (could not run --version)")
    if ready():
        print("Ollama server: running (127.0.0.1:11434 answers).")
        return 0
    print("Ollama server: not running. Start it with `ollama serve`, then import "
          "a model with `python3 setup_model.py`.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Milelik's pinned Ollama runtime (linux x86_64 or ARM64).")
    parser.add_argument("--manifest", default=str(MANIFEST_PATH),
                        help="Pinned release manifest (default: ollama/ollama.json)")
    parser.add_argument("--prefix", default="",
                        help="Install prefix holding bin/ and lib/ (default: ~/.local)")
    parser.add_argument("--arch", choices=("amd64", "arm64"), default=None,
                        help="Override CPU detection (default: detect; x86_64 uses amd64)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip the SHA-256 check (not recommended)")
    parser.add_argument("--offline", action="store_true",
                        help="Never download; use only the vendored ollama/vendor/ chunks")
    parser.add_argument("--check", action="store_true",
                        help="Report the installed Ollama without downloading anything")
    parser.add_argument("--print-url", action="store_true",
                        help="Print the pinned download URL for this PC and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    try:
        manifest = load_manifest(options.manifest)
    except OllamaSetupError as error:
        print(error, file=sys.stderr)
        return 1

    if options.check:
        return check_command()

    try:
        if options.arch is None:
            _ollama_arch, asset, description = resolve_arch(manifest=manifest)
        else:
            asset = manifest["architectures"][options.arch]["asset"]
            description = manifest["architectures"][options.arch].get("description", "")
    except (OllamaSetupError, KeyError) as error:
        print(error, file=sys.stderr)
        return 1

    if options.print_url:
        print(download_url(manifest, asset))
        return 0

    print(f"Pinned Ollama {manifest.get('pinned_version', '')}: {asset}")
    if description:
        print(description)
    try:
        executable = install_ollama(
            prefix=Path(options.prefix) if options.prefix else None,
            manifest=manifest, arch=options.arch, verify=not options.no_verify,
            offline=options.offline)
    except (OllamaSetupError, OSError, urllib.error.URLError) as error:
        print(f"Could not install Ollama: {error}", file=sys.stderr)
        return 1
    print(f"Ollama installed: {executable}")
    print("Next: `ollama serve` (in the background), then `python3 setup_model.py`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
