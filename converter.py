#!/usr/bin/env python3
"""
ACSM to EPUB/PDF Converter

Converts Adobe ACSM ebook tokens to DRM-free EPUB and PDF files
for personal offline reading.

Prerequisites (installed automatically by setup):
    brew install pugixml libzip openssl curl cmake
    libgourou (built from source)
    Calibre (brew install --cask calibre)

Usage:
    python3 converter.py --setup          # First-time setup
    python3 converter.py ebook.acsm       # Convert an ACSM file
"""

import argparse
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIBGOUROU_DIR = SCRIPT_DIR / "libgourou"
LIBGOUROU_BIN = LIBGOUROU_DIR / "utils"
ADEPT_DIR = Path.home() / ".config" / "adept"


def run(cmd, **kwargs):
    """Run a command and return the result, printing errors on failure."""
    defaults = {"capture_output": True, "text": True}
    defaults.update(kwargs)
    return subprocess.run(cmd, **defaults)


def check_command(name):
    """Check if a CLI command is available in PATH or local build."""
    # Check local libgourou build first
    local = LIBGOUROU_BIN / name
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name)


def find_tool(name):
    """Find a tool, checking local build directory first."""
    local = LIBGOUROU_BIN / name
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    system = shutil.which(name)
    if system:
        return system
    return None


# ─── Setup ───────────────────────────────────────────────────────────────


def setup_brew_deps():
    """Install build dependencies via Homebrew."""
    if not shutil.which("brew"):
        print("Homebrew is required. Install from https://brew.sh")
        sys.exit(1)

    deps = ["pugixml", "libzip", "openssl", "curl", "cmake"]
    print(f"Installing build dependencies: {', '.join(deps)}")
    result = run(["brew", "install"] + deps)
    if result.returncode != 0:
        print(f"brew install failed:\n{result.stderr}")
        sys.exit(1)
    print("[OK] Build dependencies installed.")


def _get_brew_prefixes():
    """Get Homebrew prefix paths for dependencies."""
    prefixes = {}
    for dep in ["pugixml", "libzip", "openssl", "curl"]:
        r = run(["brew", "--prefix", dep])
        prefixes[dep] = r.stdout.strip() if r.returncode == 0 else f"/opt/homebrew/opt/{dep}"
    return prefixes


def _patch_makefiles(brew_prefixes):
    """Patch libgourou Makefiles for macOS compatibility."""
    include_flags = " ".join(f"-I{p}/include" for p in brew_prefixes.values())
    lib_flags = " ".join(f"-L{p}/lib" for p in brew_prefixes.values())

    # Patch root Makefile for macOS:
    # 1. Replace ar --thin (not supported) with libtool -static (handles archive merging)
    root_mk = LIBGOUROU_DIR / "Makefile"
    content = root_mk.read_text()
    content = content.replace(
        "$(AR) rcs --thin $@ $^",
        "libtool -static -o $@ $^",
    )
    root_mk.write_text(content)

    # Patch utils Makefile: add Homebrew include/lib paths
    utils_mk = LIBGOUROU_DIR / "utils" / "Makefile"
    content = utils_mk.read_text()
    # Add brew include paths to CXXFLAGS
    content = content.replace(
        "CXXFLAGS=-Wall -fPIC -I$(ROOT)/include",
        f"CXXFLAGS=-Wall -fPIC -I$(ROOT)/include {include_flags}",
    )
    # Add brew lib paths to LDFLAGS
    content = content.replace(
        "LDFLAGS += -L$(ROOT) -lcrypto",
        f"LDFLAGS += -L$(ROOT) {lib_flags} -lcrypto",
    )
    utils_mk.write_text(content)


def build_libgourou():
    """Clone and build libgourou from source."""
    if (LIBGOUROU_BIN / "acsmdownloader").exists():
        print("[OK] libgourou already built.")
        return

    repo_url = "https://forge.soutade.fr/soutade/libgourou.git"

    if not LIBGOUROU_DIR.exists():
        print("Cloning libgourou...")
        result = run(["git", "clone", "--recurse-submodules", repo_url, str(LIBGOUROU_DIR)])
        if result.returncode != 0:
            print(f"Clone failed:\n{result.stderr}")
            sys.exit(1)

    brew_prefixes = _get_brew_prefixes()
    include_flags = " ".join(f"-I{p}/include" for p in brew_prefixes.values())

    print("Patching Makefiles for macOS...")
    _patch_makefiles(brew_prefixes)

    print("Building libgourou...")
    env = os.environ.copy()
    env["CXXFLAGS"] = include_flags

    result = run(
        ["make", "BUILD_UTILS=1", "BUILD_STATIC=1", "BUILD_SHARED=0"],
        cwd=str(LIBGOUROU_DIR),
        env=env,
    )
    if result.returncode != 0:
        print(f"Build failed:\n{result.stdout}\n{result.stderr}")
        print("\nTry installing missing deps: brew install pugixml libzip openssl curl")
        sys.exit(1)

    # Verify build
    if not (LIBGOUROU_BIN / "acsmdownloader").exists():
        print("Build completed but binaries not found.")
        print(f"Check {LIBGOUROU_BIN} for build output.")
        sys.exit(1)

    print("[OK] libgourou built successfully.")


def setup_calibre():
    """Ensure Calibre is installed."""
    if shutil.which("ebook-convert"):
        print("[OK] Calibre already installed.")
        return

    # Check common macOS Calibre locations
    calibre_convert = "/Applications/calibre.app/Contents/MacOS/ebook-convert"
    if Path(calibre_convert).exists():
        print("[OK] Calibre found at /Applications/calibre.app")
        return

    print("Installing Calibre...")
    result = run(["brew", "install", "--cask", "calibre"])
    if result.returncode != 0:
        print(f"Calibre installation failed:\n{result.stderr}")
        print("You can install manually from https://calibre-ebook.com/download")
        sys.exit(1)
    print("[OK] Calibre installed.")


def do_setup():
    """Run full first-time setup."""
    print("=== Setting up ACSM Converter ===\n")
    setup_brew_deps()
    print()
    build_libgourou()
    print()
    setup_calibre()
    print("\n=== Setup complete! ===")
    print("You can now convert ACSM files:")
    print("  python3 converter.py ebook.acsm")


# ─── Conversion ──────────────────────────────────────────────────────────


def find_ebook_convert():
    """Find ebook-convert from Calibre."""
    # Check PATH
    cmd = shutil.which("ebook-convert")
    if cmd:
        return cmd
    # Check macOS app bundle
    app_cmd = "/Applications/calibre.app/Contents/MacOS/ebook-convert"
    if Path(app_cmd).exists():
        return app_cmd
    return None


def check_ready():
    """Verify all tools are available."""
    problems = []

    if not find_tool("acsmdownloader"):
        problems.append("libgourou not built (run: python3 converter.py --setup)")
    if not find_tool("adept_activate"):
        problems.append("libgourou not built (run: python3 converter.py --setup)")
    if not find_tool("adept_remove"):
        problems.append("libgourou not built (run: python3 converter.py --setup)")
    if not find_ebook_convert():
        problems.append("Calibre not installed (run: python3 converter.py --setup)")

    if problems:
        print("Not ready. Missing components:")
        for p in set(problems):
            print(f"  - {p}")
        sys.exit(1)

    print("[OK] All tools ready.")


def detect_format(acsm_path):
    """Parse the ACSM file to detect if the download is EPUB or PDF."""
    tree = ET.parse(acsm_path)
    root = tree.getroot()
    ns = {"adept": "http://ns.adobe.com/adept"}

    src_elem = root.find(".//adept:src", ns)
    if src_elem is not None and src_elem.text:
        src = src_elem.text.lower()
        if ".pdf" in src or "output=pdf" in src:
            return "pdf"
        if ".epub" in src or "output=epub" in src:
            return "epub"

    return "pdf"


def register_device():
    """Register an Adobe device (one-time setup)."""
    device_file = ADEPT_DIR / "device.xml"
    if device_file.exists():
        print("[OK] Adobe device already registered.")
        return

    print("Registering Adobe device (anonymous)...")
    tool = find_tool("adept_activate")
    try:
        result = run([tool, "-a"], timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Device registration timed out (30s).")
    if result.returncode != 0:
        raise RuntimeError(f"Device registration failed: {result.stdout}\n{result.stderr}")

    print("[OK] Adobe device registered.")


def fulfill_acsm(acsm_path, output_path):
    """Download the DRM-protected ebook by fulfilling the ACSM token."""
    print(f"Fulfilling ACSM: {acsm_path.name}")
    tool = find_tool("acsmdownloader")
    try:
        result = run([tool, "-f", str(acsm_path), "-o", str(output_path)], timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Download timed out (120s). The ACSM token may be expired or the server is unreachable.")
    if result.returncode != 0:
        stderr = result.stderr or result.stdout or ""
        print(f"ACSM fulfillment failed:\n{stderr}", flush=True)
        raise RuntimeError(f"ACSM download failed (exit code {result.returncode}): {stderr[:500]}")

    if not output_path.exists():
        raise RuntimeError(f"Download completed but output file not found. stdout: {result.stdout[:200]}")

    size_kb = output_path.stat().st_size / 1024
    print(f"[OK] Downloaded: {output_path.name} ({size_kb:.0f} KB)")


def remove_drm(input_path, output_path):
    """Remove DRM from the downloaded ebook."""
    print(f"Removing DRM: {input_path.name}")
    tool = find_tool("adept_remove")
    try:
        result = run([tool, "-f", str(input_path), "-o", str(output_path)], timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("DRM removal timed out (60s).")
    if result.returncode != 0:
        raise RuntimeError(f"DRM removal failed: {(result.stderr or result.stdout)[:300]}")

    print(f"[OK] DRM removed: {output_path.name}")


def convert_format(input_path, output_path):
    """Convert between EPUB and PDF using Calibre."""
    print(f"Converting: {input_path.suffix} -> {output_path.suffix}")
    tool = find_ebook_convert()
    result = run([tool, str(input_path), str(output_path)])
    if result.returncode != 0:
        print(f"Conversion failed:\n{result.stderr or result.stdout}")
        sys.exit(1)

    print(f"[OK] Converted: {output_path.name}")


def convert_pipeline(acsm_path, output_dir):
    """Generator that yields (step, message) tuples for each conversion step.

    Used by both the CLI (do_convert) and the web interface (app.py).
    Raises RuntimeError on failure instead of calling sys.exit.

    Always produces 5 steps — downloads and removes DRM only.
    Format conversion (PDF→EPUB) is handled separately.
    """
    acsm_path = Path(acsm_path).resolve()
    if not acsm_path.exists():
        raise RuntimeError(f"File not found: {acsm_path}")
    if acsm_path.suffix != ".acsm":
        raise RuntimeError(f"Not an ACSM file: {acsm_path}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = acsm_path.stem

    # Step 1: Check tools
    problems = []
    if not find_tool("acsmdownloader"):
        problems.append("libgourou not built (run: python3 converter.py --setup)")
    if not find_tool("adept_activate"):
        problems.append("libgourou not built (run: python3 converter.py --setup)")
    if not find_tool("adept_remove"):
        problems.append("libgourou not built (run: python3 converter.py --setup)")
    if problems:
        raise RuntimeError("Missing components: " + "; ".join(set(problems)))
    yield (1, "All tools ready.")

    # Step 2: Detect format
    fmt = detect_format(acsm_path)
    yield (2, f"Detected format: {fmt.upper()}")

    # Step 3: Register device
    register_device()
    yield (3, "Device registered.")

    # Step 4: Download
    drm_file = output_dir / f"{stem}_drm.{fmt}"
    fulfill_acsm(acsm_path, drm_file)
    yield (4, f"Downloaded: {drm_file.name}")

    # Step 5: Remove DRM
    clean_file = output_dir / f"{stem}.{fmt}"
    remove_drm(drm_file, clean_file)
    drm_file.unlink()
    yield (5, f"DRM removed: {clean_file.name}")

    # Done
    size_mb = clean_file.stat().st_size / (1024 * 1024) if clean_file.exists() else 0
    yield ("done", f"Conversion complete! File: {clean_file.name} ({size_mb:.1f} MB)")


def do_convert(acsm_file, output_dir, no_convert=False):
    """Run the full ACSM conversion pipeline (CLI entry point).

    If no_convert is False (default), also run format conversion after
    the pipeline (PDF→EPUB or EPUB→PDF).
    """
    try:
        clean_file = None
        for step, message in convert_pipeline(acsm_file, output_dir):
            if step == "done":
                print(f"\n=== Done! ===\n{message}")
            else:
                print(f"\n=== Step {step}/5: {message} ===")
                if step == 5:
                    # Extract the clean filename from the message
                    # message is like "DRM removed: filename.pdf"
                    parts = message.split(": ", 1)
                    if len(parts) == 2:
                        clean_file = Path(output_dir) / parts[1]
    except RuntimeError as e:
        print(str(e))
        sys.exit(1)

    if not no_convert and clean_file and clean_file.exists():
        fmt = clean_file.suffix
        if fmt == ".pdf":
            other_file = clean_file.with_suffix(".epub")
        else:
            other_file = clean_file.with_suffix(".pdf")
        print(f"\n=== Converting {fmt[1:].upper()} → {other_file.suffix[1:].upper()} ===")
        convert_format(clean_file, other_file)


def main():
    parser = argparse.ArgumentParser(
        description="Convert ACSM ebook tokens to DRM-free EPUB and PDF.",
        epilog="First run: python3 converter.py --setup",
    )
    parser.add_argument(
        "acsm_file",
        nargs="?",
        help="Path to the .acsm file to convert",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Install dependencies and build tools (run once)",
    )
    parser.add_argument(
        "--no-convert",
        action="store_true",
        help="Skip format conversion (only download and remove DRM)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="output",
        help="Output directory (default: output)",
    )
    args = parser.parse_args()

    if args.setup:
        do_setup()
        return

    if not args.acsm_file:
        parser.print_help()
        sys.exit(1)

    do_convert(args.acsm_file, args.output_dir, no_convert=args.no_convert)


if __name__ == "__main__":
    main()
