#!/usr/bin/env python3
"""Vendor the actual Ollama release bytes into this repository (one-time).

Run this ONCE on a machine with internet access::

    python3 ollama/fetch_ollama.py
    git add ollama/vendor ollama/vendor.json
    git commit -m "Vendor Ollama runtime for offline installs"

It downloads the pinned release from ``ollama/ollama.json``, verifies it
against the release's published SHA-256, and splits it into ~90 MB chunks
under ``ollama/vendor/`` (each chunk stays under GitHub's 100 MB
single-file limit) plus a ``ollama/vendor.json`` hash manifest.

After that commit, any machine holding this repository — including a
brand-new linux Mint box with no internet — installs Ollama with::

    python3 ollama/setup_ollama.py --offline

with no downloads and no package manager involved. Re-run this script to
switch CPU architecture or to vendor a newer pinned release.

Standard library only; no third-party packages needed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

try:
    import setup_ollama
except ImportError:  # allow running as `python3 ollama/fetch_ollama.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import setup_ollama

HERE = Path(__file__).resolve().parent


def clear_vendor_dir(vendor_dir: Path) -> None:
    """Remove previously vendored ``*.partNN`` chunks, keeping other files."""
    if not vendor_dir.is_dir():
        return
    for part in sorted(vendor_dir.glob("*.part??")):
        if part.is_file():
            part.unlink()


def write_vendor_manifest(path: Path, *, asset: str, ollama_arch: str,
                          pinned_version: str, chunk_size: int,
                          split: dict) -> dict:
    """Write vendor.json for a fresh split and return it."""
    vendor = {"asset": asset,
              "ollama_arch": ollama_arch,
              "pinned_version": pinned_version,
              "vendor_dir": setup_ollama.VENDOR_DIR_NAME,
              "chunk_metabytes": chunk_size / (1024 * 1024),
              "total_size": split["total_size"],
              "sha256": split["sha256"],
              "chunks": split["chunks"]}
    path.write_text(json.dumps(vendor, indent=2) + "\n", encoding="utf-8")
    return vendor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download the pinned Ollama release and vendor it into "
                    "ollama/vendor/ as verified Git-friendly chunks (one-time, "
                    "needs internet).")
    parser.add_argument("--manifest", default=str(HERE / setup_ollama.MANIFEST_NAME),
                        help="Pinned release manifest (default: ollama/ollama.json)")
    parser.add_argument("--arch", choices=("amd64", "arm64"), default=None,
                        help="CPU to vendor for (default: this PC; x86_64 uses amd64)")
    parser.add_argument("--vendor-dir", default=str(HERE / setup_ollama.VENDOR_DIR_NAME),
                        help="Chunk directory (default: ollama/vendor)")
    parser.add_argument("--chunk-mb", type=float, default=90.0,
                        help="Chunk size in MB; keep under 100 for GitHub (default: 90)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download and re-split even if valid chunks exist")
    parser.add_argument("--output-archive", default="",
                        help="Optional extra copy of the full verified archive "
                             "(outside the repo, e.g. for a USB stick)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    try:
        manifest = setup_ollama.load_manifest(options.manifest)
        if options.arch is None:
            ollama_arch, asset, _ = setup_ollama.resolve_arch(manifest=manifest)
        else:
            ollama_arch = options.arch
            asset = manifest["architectures"][options.arch]["asset"]
    except (setup_ollama.OllamaSetupError, KeyError) as error:
        print(f"Cannot vendor Ollama: {error}", file=sys.stderr)
        return 1

    chunk_size = int(options.chunk_mb * 1024 * 1024)
    if chunk_size <= 0 or options.chunk_mb >= 100:
        print("Chunk size must be positive and under 100 MB for GitHub.",
              file=sys.stderr)
        return 1

    vendor_dir = Path(options.vendor_dir)
    vendor_manifest_path = vendor_dir.parent / setup_ollama.VENDOR_MANIFEST_NAME
    pinned_version = manifest.get("pinned_version", "")

    existing = setup_ollama.find_vendored(vendor_manifest_path)
    if existing is not None and not options.force:
        found_dir, found = existing
        if found.get("asset") == asset and found.get("sha256") \
                and setup_ollama.vendor_is_valid(found_dir, found):
            print(f"Ollama {found.get('pinned_version', '')} ({asset}) is already "
                  f"vendored and verified in {found_dir} ({len(found['chunks'])} "
                  "chunks). Nothing to do.")
            return 0
        print("Existing vendored chunks are stale or incomplete; re-fetching…")

    if options.force:
        clear_vendor_dir(vendor_dir)
        if vendor_manifest_path.is_file():
            vendor_manifest_path.unlink()

    try:
        with tempfile.TemporaryDirectory(prefix="milelik-ollama-vendor-") as tmp:
            tmp_dir = Path(tmp)
            archive = tmp_dir / asset
            print(f"Downloading Ollama {pinned_version} for linux/{ollama_arch} "
                  f"({asset})…")
            print(f"From: {setup_ollama.download_url(manifest, asset)}")
            setup_ollama.download_to_file(
                setup_ollama.download_url(manifest, asset), archive)
            sums_path = tmp_dir / manifest.get("checksum_file", "sha256sum.txt")
            setup_ollama.download_to_file(
                setup_ollama.checksum_url(manifest), sums_path)
            setup_ollama.verify_archive(
                archive, sums_path.read_text(encoding="utf-8", errors="replace"),
                asset)
            print("SHA-256 verified against the release checksum file.")
            if options.output_archive:
                extra = Path(options.output_archive)
                extra.parent.mkdir(parents=True, exist_ok=True)
                print(f"Saving full archive copy to {extra} …")
                extra.write_bytes(archive.read_bytes())
            print(f"Splitting into ~{options.chunk_mb:g} MB chunks under {vendor_dir} …")
            clear_vendor_dir(vendor_dir)
            split = setup_ollama.split_file(archive, vendor_dir, chunk_size)
            vendor = write_vendor_manifest(
                vendor_manifest_path, asset=asset, ollama_arch=ollama_arch,
                pinned_version=pinned_version, chunk_size=chunk_size, split=split)
    except (setup_ollama.OllamaSetupError, OSError, urllib.error.URLError) as error:
        print(f"Cannot vendor Ollama: {error}", file=sys.stderr)
        return 1

    size_gb = vendor["total_size"] / (1024 ** 3)
    print(f"Vendored Ollama {pinned_version} ({asset}): {len(vendor['chunks'])} "
          f"chunks, {size_gb:.2f} GB total, manifest at {vendor_manifest_path}.")
    print("Next: git add ollama/vendor ollama/vendor.json && git commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
