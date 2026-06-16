"""py2app build script for Emma.app (Prompt 29, Part A).

Build-time ONLY — not part of pyproject's runtime build system. Produces a
self-contained ``dist/Emma.app`` bundling a Python framework + all deps + Emma's
code, so a user needs zero setup.

    .venv/bin/python -m pip install py2app   # (or: uv pip install py2app)
    .venv/bin/python setup_py2app.py py2app

Notes:
- LSUIElement=True → background/menubar app, no Dock icon.
- Bundle id com.garcia.emma matches the LaunchAgent (installer/com.garcia.emma.plist).
- Secrets NEVER live in Info.plist — they go to the Keychain via the first-run
  wizard / emma.setup. The plist only carries the Sparkle update config.
- Emma discovers tools dynamically (importlib over tools/*), so the whole `tools`
  package (and the other first-party packages) is force-included via `packages`.
"""

from __future__ import annotations

from setuptools import setup

APP = ["emma/__main__.py"]

# First-party packages discovered dynamically at runtime — must be whole-included.
PACKAGES = [
    "emma", "core", "tools", "memory", "config", "actions", "dashboard",
    # Heavy third-party with dynamic/native bits modulegraph can miss:
    "openai", "httpx", "pydantic", "pydantic_settings", "structlog", "certifi",
    "numpy", "sounddevice", "onnxruntime", "openwakeword", "trafilatura", "croniter",
]

# Modules imported lazily/by-string that the dependency graph may not reach.
INCLUDES = ["objc", "Foundation", "AppKit", "ApplicationServices", "sqlite3", "json"]

PLIST = {
    "CFBundleName": "Emma",
    "CFBundleDisplayName": "Emma",
    "CFBundleIdentifier": "com.garcia.emma",
    "CFBundleVersion": "1.0.0",
    "CFBundleShortVersionString": "1.0.0",
    "LSMinimumSystemVersion": "14.0",
    "LSUIElement": True,  # background app — no Dock icon
    "NSHighResolutionCapable": True,
    "NSMicrophoneUsageDescription": "Emma escucha tu voz para asistirte.",
    "NSAppleEventsUsageDescription": "Emma controla apps como Notas y Calendario por ti.",
    # ---- Sparkle auto-update (Part D). SUPublicEDKey is filled in by release.sh
    #      after Garcia generates the EdDSA key pair (see installer/README.md).
    "SUFeedURL": "https://garcia.github.io/emma/appcast.xml",
    "SUEnableAutomaticChecks": True,
    "SUScheduledCheckInterval": 86400,
    "SUPublicEDKey": "@SU_PUBLIC_ED_KEY@",
}

OPTIONS = {
    "packages": PACKAGES,
    "includes": INCLUDES,
    "iconfile": "installer/assets/Emma.icns",
    "plist": PLIST,
    "strip": True,
    "optimize": 1,
    # onnxruntime / sounddevice ship dylibs py2app must keep; don't let it prune them.
    "frameworks": [],
    "resources": [],
}

setup(
    name="Emma",
    app=APP,
    options={"py2app": OPTIONS},
)
