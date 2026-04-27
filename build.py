"""Build a standalone executable of auto_form_filler with PyInstaller.

Usage:
    pip install pyinstaller
    python build.py            # one-file binary, console + GUI
    python build.py --no-cli   # GUI-only (no console window on Windows)

Output:
    dist/auto_form_filler          (Linux/macOS)
    dist/auto_form_filler.exe      (Windows)

Notes:
    PyInstaller is platform-specific — to get a Windows .exe you must run this
    on Windows. The build does NOT bundle Chromium (it's ~150 MB and managed
    by Playwright). After distributing the binary, the end-user runs it once
    and on first launch it will prompt to download the browser:
        path/to/auto_form_filler --install-browser
    or simply:
        python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Files to bundle as data (read at runtime via Path(__file__).with_name).
DATA_FILES = [
    "_browser_overlay.js",
    "config.json",
    "README.md",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-cli", action="store_true",
                        help="Hide the console window (GUI-only). Recommended for end-user builds.")
    parser.add_argument("--name", default="auto_form_filler", help="Output binary name.")
    parser.add_argument("--keep", action="store_true", help="Keep build/ and *.spec files for inspection.")
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed — run: pip install pyinstaller", file=sys.stderr)
        sys.exit(1)

    sep = ";" if sys.platform.startswith("win") else ":"
    add_data = []
    for f in DATA_FILES:
        if (ROOT / f).exists():
            add_data += ["--add-data", f"{f}{sep}."]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", args.name,
        "--onefile",
        "--clean",
        "--noconfirm",
        # Keep Tkinter visible — needed by the GUI even with --no-cli on Windows.
        "auto_fill_gui.py",
    ]
    if args.no_cli and sys.platform.startswith("win"):
        cmd.append("--noconsole")
    cmd += add_data
    # Hidden imports — Playwright relies on package-resource discovery.
    cmd += ["--collect-all", "playwright"]

    print("$ " + " ".join(cmd))
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        sys.exit(rc)

    out = ROOT / "dist" / (args.name + (".exe" if sys.platform.startswith("win") else ""))
    print(f"\nBuilt: {out}")

    if not args.keep:
        for path in [ROOT / "build", ROOT / f"{args.name}.spec"]:
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
