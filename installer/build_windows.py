"""Build the Windows Emma.exe with PyInstaller (Prompt 30, Part C1).

Runs ON Windows (the CI windows job, or the user's Win11 VM). py2app is mac-only, so
Windows uses PyInstaller. One-FOLDER build (faster startup than one-file). Tier-1
runs; Tier-2 calls hit the platform-layer stubs → friendly "no tengo eso todavía".

    pip install -e .[windows]
    python installer/build_windows.py
    # → dist/Emma-Windows/Emma.exe
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if sys.platform != "win32":
        print("build_windows.py must run on Windows.", file=sys.stderr)
        return 2
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "Emma",
        "--onedir",                       # faster startup than --onefile
        "--noconsole",                    # background app, no console window
        "--icon", str(ROOT / "installer" / "assets" / "icon-1024.png"),
        # The registry discovers tools dynamically (importlib over tools/*), so
        # whole-include the first-party packages PyInstaller's graph can miss.
        "--collect-submodules", "tools",
        "--collect-submodules", "core",
        "--collect-submodules", "memory",
        "--hidden-import", "core.platform._win.notes",
        "--hidden-import", "core.platform._win.fs",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        str(ROOT / "emma" / "__main__.py"),
    ]
    print("==>", " ".join(args))
    return subprocess.call(args)


if __name__ == "__main__":
    raise SystemExit(main())
