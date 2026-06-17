"""Windows first-run wizard (Prompt 30, Part C4) — tkinter, same shape as 29-Part C.

Skeleton for the Windows side: 5 steps (welcome → API keys → services → voice test →
done). Secrets go to the Windows Credential Manager via ``keyring`` (cross-platform,
the Win equivalent of the Mac Keychain). Runs only on Windows; the real wiring
(emma.setup subprocess, sounddevice mic test) lands as the Win side matures (30.x).
"""

from __future__ import annotations

import sys


def _store_key(label: str, value: str) -> None:
    import keyring  # windows extra

    keyring.set_password("com.garcia.emma", label, value)


def run() -> int:
    if sys.platform != "win32":
        print("wizard_win.py runs on Windows only.", file=sys.stderr)
        return 2
    import tkinter as tk
    from tkinter import ttk

    steps = [
        ("Bienvenida", "Hola, soy Emma. En unos pasos quedo lista en tu Windows."),
        ("Tu llave de OpenAI", "Pega tu API key (sk-...). Se guarda en el Administrador de credenciales."),
        ("Servicios", "Conecta lo que uses (opcional)."),
        ("Prueba de voz", "Di «hola Emma» para confirmar el micrófono."),
        ("Listo", "Emma está activa. Di «hola Emma» cuando la necesites."),
    ]
    state = {"i": 0}

    root = tk.Tk()
    root.title("Configurar Emma")
    root.geometry("520x320")
    title = ttk.Label(root, font=("Segoe UI", 16, "bold"))
    body = ttk.Label(root, wraplength=460)
    entry = ttk.Entry(root, width=46, show="")
    title.pack(pady=(28, 6))
    body.pack(pady=6)

    def render() -> None:
        name, desc = steps[state["i"]]
        title.config(text=name)
        body.config(text=desc)
        entry.pack_forget()
        if state["i"] == 1:  # API key step
            entry.pack(pady=10)
        nxt.config(text="Terminar" if state["i"] == len(steps) - 1 else "Siguiente")

    def on_next() -> None:
        if state["i"] == 1 and entry.get().strip():
            _store_key("OPENAI_API_KEY", entry.get().strip())
        if state["i"] == len(steps) - 1:
            root.destroy()
            return
        state["i"] += 1
        render()

    nxt = ttk.Button(root, text="Siguiente", command=on_next)
    nxt.pack(side="bottom", pady=20)
    render()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
