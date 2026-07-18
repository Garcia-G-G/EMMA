"""Emma's native menubar UI (EMMA-APP Parts 2-3).

A SEPARATE process from the daemon (`python -m emma.ui`), so Cocoa's main-thread
run loop never collides with the daemon's asyncio loop — the Ollama pattern:
menubar app + daemon talking over the local dashboard server (127.0.0.1:3200 HTTP,
3201 WS). Zero new IPC beyond the WebSocket that already exists.
"""
