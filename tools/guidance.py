"""Per-tool routing guidance, appended to each tool's description (LAUNCH-12).

These rules used to live in the always-on system prompt
(``core/conversation.py:_build_instructions``), where every turn paid for them
— ~4.5k tokens — whether or not the tool was relevant, and even when the tool
was unavailable on this Mac and never offered. A rule like "'busca un repo' →
search_github, read the top 1-3 by name + stars" describes ``search_github``,
not Emma, so it now travels with ``search_github``: the model reads it exactly
when the tool is offered, and it vanishes with the tool.

``registry._spec()`` appends ``GUIDANCE[name]`` to the description the model
receives. It is a separate map rather than a third docstring paragraph because
``tools/base.py:_docstring_summary`` keeps only the first two paragraphs — a
third would be silently truncated.

Rules that are true of every turn (language, confirmation flow, the untrusted
content fence, corrections, anaphora) stay in the system prompt.

Section tags (``# from: …``) name the prompt section each rule came from.
"""

from __future__ import annotations

GUIDANCE: dict[str, str] = {
    # ---- from: Screen vision -------------------------------------------------
    "describe_screen": (
        "Screen vision has two layers; use them IN ORDER: AX (this tool — "
        "fast + exact, like reading the page's source) first, look_at_screen "
        "(a screenshot, like looking at it) only as the fallback. "
        "'¿Qué veo?' / 'describe la pantalla' / 'léeme la pantalla' / '¿qué dice "
        "esa ventana?' → describe_screen (or summarize_screen for a synthesized "
        "answer). AUTOMATIC FALLBACK: every AX read returns a `density` block. If "
        "`ax_appears_thin` is true AND `thin_by_design` is false (or the read "
        "plainly didn't answer what the user asked), automatically call "
        "look_at_screen with the same question. When `thin_by_design` is true (a "
        "terminal, a blank doc), the thin read is EXPECTED — trust it, don't waste "
        "a screenshot. When you do fall back, SAY SO briefly first: 'el árbol de "
        "accesibilidad no me dijo mucho, déjame mirar la pantalla.' If "
        "`density.web_content_visible` is true AND `density.ax_chars` is under "
        "200, the AX tree shows the chrome but not the real page (Notion, Linear, "
        "Slack, a browser that didn't expose its content) — go straight to "
        "look_at_screen with the user's question."
    ),
    "summarize_screen": (
        "AX-first screen reading, synthesized. Same fallback rule as "
        "describe_screen: if the `density` block says `ax_appears_thin` and not "
        "`thin_by_design`, or `density.web_content_visible` with "
        "`density.ax_chars` under 200, call look_at_screen with the same question "
        "(say so briefly first)."
    ),
    "read_pane_text": (
        "'Léeme este panel' / 'léeme la terminal' / '¿qué dice este panel?' → "
        "read_pane_text (SOLO el panel enfocado, no toda la ventana). Same AX "
        "`density` fallback to look_at_screen as describe_screen."
    ),
    "summarize_pane": (
        "'resúmeme el panel' → summarize_pane (SOLO el panel enfocado). When the "
        "user asks about the CONTENT of a web page or an editor's file in any "
        "browser or coding tool ('¿qué dice esta página?', '¿de qué trata el "
        "artículo?', '¿qué hace este código?'), prefer summarize_pane so the answer "
        "is scoped to the page they're looking at, not the chrome (tabs, sidebar). "
        "If a result's web_content is false, the app didn't expose its page — "
        "fall back to look_at_screen rather than pretending to have read it."
    ),
    "where_am_i": (
        "'¿Dónde estoy?' / '¿en qué panel estoy?' → where_am_i. Name the region "
        "from what AX exposes (its label/position); don't guess a generic type."
    ),
    "window_layout": (
        "'¿Qué tengo en esta ventana?' / '¿qué paneles hay?' → window_layout. Name "
        "regions from what AX exposes; don't guess a generic type."
    ),
    "find_button": (
        "'¿Hay un botón X?' / 'encuentra el botón Cancelar' → find_button. For 'el "
        'botón X de este panel\', pass scope="focus".'
    ),
    "click_button": (
        "'Cierra ese diálogo' / 'haz click en Aceptar' → click_button (it confirms "
        "first; the runtime gate also fires). NEVER click a financial / banking / "
        "payment confirmation button without an extra explicit 'sí, hazlo' from "
        "the user — re-ask once more even if already confirmed."
    ),
    "type_in_field": (
        "'Escribe mi password en el campo' → type_in_field. If the value is "
        "secret, fetch it with your secrets tool; NEVER say a secret value out loud."
    ),
    "look_at_screen": (
        "Layer 2 of screen vision (screenshot + on-device OCR) — the fallback when "
        "an AX read (describe_screen, read_pane_text, summarize_pane…) comes back "
        "thin: its `density` block has `ax_appears_thin` true and "
        "`thin_by_design` false, or `web_content_visible` true with `ax_chars` "
        "under 200. Go STRAIGHT here (skip AX) when the user says 'mira la "
        "pantalla' / 'toma una captura' / '¿qué dice esa imagen?' / 'léeme ese PDF' "
        "/ 'describe lo que ves' (an open image), or the app has no real "
        "accessibility tree (old apps, thin Electron). Pass `question` for a "
        "pointed answer. Todo on-device — la captura se borra, no sube a la nube."
    ),
    # ---- from: Action history + undo ----------------------------------------
    "what_did_you_do": (
        "'¿Qué hiciste ayer?' / '¿qué hiciste el martes?' / '¿qué hiciste hoy?' → "
        "what_did_you_do (pass the day phrase verbatim). Also the first step for "
        "'deshaz la de las 3' — find the action id, then undo_action_by_id."
    ),
    "undo_last_action": (
        "'Deshaz lo último' / 'deshazlo' / 'echa para atrás' → undo_last_action "
        "(confirm before reversing). If the action can't be reversed (a sent "
        "message, a posted tweet, a played song), say so honestly and offer the "
        "manual step if there is one."
    ),
    "undo_action_by_id": (
        "'Deshaz la de las 3' → first what_did_you_do to find the action id, then "
        "undo_action_by_id. If it can't be reversed, say so honestly and offer "
        "the manual step."
    ),
    # ---- from: Self-diagnostics ---------------------------------------------
    "diagnose_self": (
        "'¿Cómo estás?' / '¿funcionas bien?' / 'diagnóstico' → diagnose_self (speak the real "
        "metrics; if something is off, say so honestly)."
    ),
    "reload_tools": "'Recarga las herramientas' / 'recárgate' → reload_tools.",
    "shutdown_emma": (
        "'Apágate' / 'shut down' / 'deja de escuchar del todo' → shutdown_emma "
        "(she stops until a manual restart — say goodbye, then it ends the session)."
    ),
    "restart_emma": "'Reiníciate' / 'restart' / 'vuelve a arrancar' → restart_emma.",
    "snooze_listening": (
        "'Duérmete' / 'descansa N minutos' / 'no escuches un rato' → "
        "snooze_listening (pass minutes if given; she stops hearing 'Hey Emma' "
        "until it expires)."
    ),
    "snooze_proactivities": (
        "'No me interrumpas N min' / 'silencio' → snooze_proactivities (mutes "
        "proactive notifications only — she still answers when you call her)."
    ),
    "telemetry_summary": "'Resumen de la semana / del día / del mes' → telemetry_summary.",
    # ---- from: Life utilities -----------------------------------------------
    "current_datetime_speak": "'¿Qué hora es?' / '¿qué día es?' → current_datetime_speak.",
    "start_timer": "'Timer de N minutos' / 'ponme 10 minutos' → start_timer.",
    "list_timers": "'¿Qué timers tengo?' → list_timers.",
    "coin_flip": "'Tira una moneda' → coin_flip.",
    "roll_dice": "'Saca un dado' → roll_dice.",
    "pick_random": "'Elige por mí' → pick_random.",
    "generate_password": (
        "'dame una contraseña' → generate_password (va al portapapeles; NUNCA la "
        "digas en voz alta)."
    ),
    "birthday_remember": "'Guarda que el cumpleaños de X es Y' → birthday_remember.",
    "birthdays_today": "'¿quién cumple hoy / esta semana?' → birthdays_today / birthdays_this_week.",
    "birthdays_this_week": "'¿quién cumple hoy / esta semana?' → birthdays_today / birthdays_this_week.",
    "rss_latest": "'Últimas de <feed>' → rss_latest.",
    "summarize_url": "'¿De qué va esta URL?' → summarize_url.",
    "convert": "'Cuánto es 100 USD en MXN' / '5 km en millas' / '20°C en F' → convert.",
    # ---- from: File operations ----------------------------------------------
    "find_file": "'¿Dónde está X?' / 'busca el PDF de Y' / '¿dónde guardé Z?' → find_file.",
    "analyze_disk_usage": (
        "'¿Qué está ocupando lugar?' / '¿por qué está lleno el disco?' → analyze_disk_usage."
    ),
    "free_space_assist": (
        "'Libera espacio' → free_space_assist (confirma; mueve a la Papelera, reversible)."
    ),
    "rename_batch": "'Renombra los X a Y' → rename_batch (muestra vista previa, luego confirma).",
    # ---- from: Workflows + conditionals -------------------------------------
    "run_workflow": (
        "Si the user pide varias cosas en una frase ('haz X y Y y agrega Z'), arma "
        "un workflow: run_workflow con la lista de pasos (cada uno {tool, args, "
        "depends_on, desc en español}). Las destructivas se confirman UNA sola vez "
        "al inicio describiendo el plan completo, no por paso. Lee el plan como "
        "lista y, con un sí, córrelo."
    ),
    "schedule_conditional": (
        "'Si X pasa, haz Y' → schedule_conditional. Arma el trigger con el DSL "
        '(email_from("a@x.com", contains="...") / calendar_event("...") created / '
        'time_at("ISO")) y confirma la semántica del trigger antes de guardar.'
    ),
    "list_conditionals": "'¿Qué tienes pendiente?' / '¿qué quedó condicional?' → list_conditionals.",
    # ---- from: Investigación + Vague search guard + Knowledge dictionary -----
    "deep_research": (
        "'¿Qué pasó con X?' / 'investígame Y' / 'resume lo último de Z' → "
        "deep_research (lee las fuentes y sintetiza), NO search_web. Después de "
        "deep_research, menciona las fuentes por nombre brevemente: «Según OpenAI "
        "Blog y The Verge, …»."
    ),
    "search_web": (
        "search_web se queda para 'abre google y busca' o cuando solo quieres los "
        "enlaces; '¿qué pasó con X?' / 'investígame Y' is deep_research. "
        "Before searching, check whether he means one of his saved pages "
        "(open_my_page) or a glossary term he already taught you."
    ),
    "search_github": (
        "If the query is vague, ask first ('¿de qué quieres el repo?', '¿de "
        "quién?', '¿qué lenguaje?'). Repo cloning flow: when the "
        "user asks to 'buscar un repo', read the top 1-3 matches by name + star "
        "count. If he names one (a number or owner), pick it; otherwise present "
        "the top match and ask '¿clono el de X?'. 'El repo de <alguien-más>' that "
        "sounds like a handle (one word, no spaces) → search_github with "
        "user:<handle>, not free text. If search_github finds nothing AND the "
        "query looks like a handle, DON'T accept the empty result: offer my_repos "
        "(if he meant himself) or ask him to confirm the user — it may have been "
        "mistranscribed."
    ),
    "my_repos": (
        "'Mis repos' / 'mi github' / 'los repos que tengo' / 'el repo que hice de "
        "X' → my_repos. NUNCA pongas el nombre de the user como query de "
        "búsqueda. Si menciona un tema, filtra los resultados tú. Si aún no sabes "
        "su usuario de GitHub, pregúntale UNA vez ('¿cuál es tu usuario de "
        "GitHub?') y llama remember_user_profile. No adivines su usuario a partir "
        "de su nombre."
    ),
    "clone_and_open": (
        "When he says 'clónalo en mi IDE' or chains 'busca X y clónalo', call "
        "clone_and_open with the resolved repo (use get_repo_url first if you "
        "only have a name). It returns requires_confirmation the first time — "
        "speak the question, wait for sí/no, then re-call with confirmed: true. "
        "Once the clone is spawned, do NOT narrate. Say one short line ('listo, "
        "clonando X') and stop; the notification + the IDE opening are enough."
    ),
    "open_my_page": (
        "'Mi <thing>' or 'mi <name>' usually means a dictionary page — check "
        "here BEFORE search_web or search_github; it's instant and grounded. "
        "Short acronyms (MCP, OWASP, MVP) usually have a dictionary expansion; if "
        "found, use it in your reply without explaining unless the user asks."
    ),
    "remember_page": "If the user teaches you something ('recuerda que...'), use remember_page / remember_contact / remember_term as appropriate.",
    "remember_contact": "If the user teaches you something ('recuerda que...'), use remember_page / remember_contact / remember_term as appropriate.",
    "remember_term": (
        "If the user teaches you something ('recuerda que...'), use remember_page / "
        "remember_contact / remember_term as appropriate."
    ),
    "remember_user_profile": (
        "Identidad (yo/mi/mío/mis): resolve against the user's profile BEFORE any "
        "external search. When you learn his handle for a service (e.g. GitHub, "
        "asked ONCE), save it here. Never guess a handle from his name."
    ),
    # ---- from: Integraciones -------------------------------------------------
    "tableplus_query": (
        "'Ejecuta select … en mi base X' → tableplus_query (resuelve la conexión; "
        "los SELECT corren directo, las escrituras INSERT/UPDATE/DELETE "
        "confirman). Si falta el token, di que se configura con «python -m "
        "emma.setup --only <servicio>»; no inventes que ya está hecho."
    ),
    "postman_run": (
        "'Corre el collection de health en Postman' → postman_run. Si falta el "
        "token, di que se configura con «python -m emma.setup --only <servicio>»."
    ),
    "create_linear_issue": (
        "'Crea issue en Linear: «X»' → create_linear_issue (resuelve el equipo; si "
        "hay varios y no lo dices, pregunta cuál). Confirma antes de crear. Si "
        "falta el token, di que se configura con «python -m emma.setup --only "
        "<servicio>»."
    ),
    "create_jira_issue": (
        "'Crea issue en Jira en el proyecto ENG: «X»' → create_jira_issue. "
        "Confirma. Si falta el token, di que se configura con «python -m "
        "emma.setup --only <servicio>»."
    ),
    "notion_append": (
        "'Agrega a mi página de ideas: «X»' → notion_append (busca la página; si "
        "hay varias, pregunta cuál). Confirma antes de escribir. Si falta el "
        "token, di que se configura con «python -m emma.setup --only <servicio>»."
    ),
    # ---- from: Speaker ID ---------------------------------------------------
    "enroll_my_voice": (
        "«Esta es mi voz» / «enrolla mi voz» / «aprende mi voz» → enroll_my_voice. "
        "Si una acción destructiva se rechaza porque no reconoces la voz, explica: "
        "«no te reconozco bien la voz; di 'Emma, esta es mi voz' para enrollarla, "
        "o pásale el dispositivo a the user para que confirme»."
    ),
    "who_is_speaking": "«¿Quién está hablando?» / «¿soy yo?» → who_is_speaking.",
    "forget_my_voice": "«Olvida la voz de X» → forget_my_voice (confirma primero).",
    # ---- from: Smart note append --------------------------------------------
    "append_to_note": (
        "'Agrega X a <título>' → append_to_note(title=<título>, text=X). NO "
        "desambigües tú antes; la herramienta devuelve requires_confirmation "
        "cuando necesita tu ayuda. Si responde con '¿para cuándo?' (o te pide "
        "elegir un sufijo), repite la pregunta tal cual; cuando conteste "
        "('miércoles'), re-llama con suffix=<respuesta> y confirmed=true. Si "
        "responde 'no encontré… ¿la creo nueva?', transmítelo; con el sí re-llama "
        "con create_if_missing=true y confirmed=true. Si pide explícitamente 'crea "
        "una nueva <título>', llama con el título completo, create_if_missing=true "
        "y confirmed=true desde la primera llamada. 'La última nota' / 'mi última "
        "nota' / 'esa nota que acabo de crear' → recent=true; NUNCA busques "
        "'última' como título literal. Si no estás seguro ('apunta esto en la "
        "nota de antes'), llama resolve_recent_note PRIMERO y confirma ('¿la de "
        "\\'Pendientes para mañana\\'?') antes de modificar nada."
    ),
    "resolve_recent_note": (
        "'La nota de antes' / 'esa nota' → resolve_recent_note FIRST, confirm "
        "with the user which note, then modify it."
    ),
    "read_note": (
        "'La última nota' / 'mi última nota' / 'the last note' → recent=true; "
        "NUNCA busques 'última' como título literal."
    ),
    # ---- from: App control layering + App URL schemes + In-app resources ----
    "open_in_ide": (
        "For IDE actions prefer the specialized tools (open_in_ide, "
        "new_file_in_ide, search_in_ide); don't hand-roll AppleScript or "
        "keystrokes when these exist."
    ),
    "open_url": (
        "To open URLs use open_url (the user's normal browser). Do NOT use "
        "browser_navigate — that's headless Playwright, a different flow."
    ),
    "browser_navigate": (
        "Headless Playwright browsing only. To open a URL for the user to see, "
        "use open_url instead."
    ),
    "run_in_terminal": (
        "For shell commands the user wants to watch, use run_in_terminal; for "
        "background work he won't watch, use run_shell_task."
    ),
    "run_shell_task": (
        "Background shell work the user won't watch → run_shell_task; if he wants "
        "to watch it, use run_in_terminal."
    ),
    "run_command": (
        "Use ONE simple command. Never chain with && or write inline scripts. "
        "Call multiple times if needed."
    ),
    "play_track": (
        "For music use play_track / play_playlist / pause / resume — don't send "
        "keystrokes for play/pause."
    ),
    "app_keystroke": (
        "Last resort: only when no specialized action exists for what the user "
        "asked (IDE tools, open_url, music tools, open_in_app deep links)."
    ),
    "app_menu_click": (
        "Last resort: only when no specialized action exists for what the user asked."
    ),
    "app_focus": (
        "Last resort: only when no specialized action exists. For chat apps "
        "(Slack/Discord/WhatsApp) prefer channel deep-linking via open_in_app."
    ),
    "open_in_app": (
        "When the user names an app + an action (Slack, Figma, Linear, Notion, "
        "Things, Obsidian, Discord, WhatsApp...), use open_in_app — it builds the "
        "app's deep-link URL from the capabilities registry. Don't fall back to "
        "app_keystroke unless the app has no URL scheme. For chat apps prefer "
        "channel deep-linking here over app_focus. 'Emma, abre la conexión X' "
        "(TablePlus, bases de datos) → open_in_app(target=X, kind='connection'); "
        "'Abre el canal Y en Slack' → open_in_app(target=Y, app='slack', "
        "kind='channel'). If the tool doesn't know the resource, offer to save "
        "it: ask for the exact name and call remember_connection(name, app, kind)."
    ),
    "open_application": (
        "For plain 'abre <app>' with no further intent, just use "
        "open_application. Only ever open an app the user actually has installed; "
        "if not sure, check first (never open Firefox if he only has Chrome). If "
        "he wants an app he doesn't have, say so and offer to install it."
    ),
    "remember_app": (
        "If the user teaches you a new app ('recuerda que X usa el esquema Y') → remember_app."
    ),
    "remember_connection": (
        "Save an in-app resource (a DB connection, a Slack channel) the user named "
        "when open_in_app didn't know it: ask for the exact name first."
    ),
    # ---- from: Defaults & apps ----------------------------------------------
    "set_preferred_app": (
        "You CAN set and change the user's preferred app per category "
        "(editor/ide, terminal, music, browser). When he asks ('usa VS Code', "
        "'hazme default Chrome', 'prefiero Zed'), just do it — never refuse. "
        "Only pick an app he actually has installed."
    ),
    "remember_app_preference": (
        "'Usa VS Code' / 'prefiero Zed' → just do it, never refuse. Only pick an "
        "app he actually has installed. Also the step after an edit tool returns "
        "data.editor_unset: remember_app_preference('editor', <su elección>), then "
        "re-call the SAME edit tool with confirmed=true."
    ),
    "get_preferred_app": "Read back the user's preferred app for a category.",
    # ---- from: Browser tabs --------------------------------------------------
    "list_browser_tabs": "'¿Cuántas pestañas tengo?' → list_browser_tabs.",
    "close_duplicate_tabs": (
        "'Cierra las duplicadas' → close_duplicate_tabs (asks first; google.com is "
        "protected by default). Only if the user EXPLICITLY says to include "
        "Google ('incluyendo Google'), pass protect_domains=[] on that single "
        "call — never make it the default."
    ),
    "close_tabs_matching": (
        "'Cierra las de YouTube' → close_tabs_matching('youtube'). Only if the user "
        "EXPLICITLY says to include Google, pass protect_domains=[] on that call."
    ),
    # ---- from: Terminal in IDE -----------------------------------------------
    "ide_terminal_send": (
        "'Emma, en la terminal de Cursor corre X' / 'escribe X en la terminal' → "
        "ide_terminal_send(text=X). It opens the terminal if needed, pastes, and "
        "presses Enter. For interactive TUI prompts (Claude Code etc.) "
        "Enter-by-script does NOT submit; call with enter=false and tell the user "
        "to press Enter himself."
    ),
    # ---- from: Editing files + Coding agent delegation ----------------------
    "edit_file_append": (
        "'Emma, en mi archivo X agrega Y al final' → edit_file_append. Small "
        "in-place edits only (≤ 3 lines, 1 file, no logic to figure out); larger "
        "work → delegate_to_codex. Asks for confirmation: SPEAK the diff summary "
        "the tool returns ('voy a agregar 3 líneas al final de utils.py — "
        "¿confirmas?'), wait for sí, re-call with confirmed=true. After editing, "
        "confirm briefly ('listo, lo cambié y lo abrí en Cursor'); do NOT read the "
        "file back — the runtime opens it at the changed line. If a result has "
        "data.editor_unset = true, ASK which IDE using data.candidates ('¿Cursor, "
        "VS Code o Zed?'), call remember_app_preference('editor', <su elección>), "
        "then re-call the SAME edit tool with confirmed=true. Don't apologize for "
        "the question. The reveal may lag 1-2 s the first time — don't apologize "
        "or repeat the edit."
    ),
    "edit_file_prepend": (
        "'Emma, agrega Y al inicio de X' → edit_file_prepend. Same flow as "
        "edit_file_append: speak the diff summary, wait for sí, re-call with "
        "confirmed=true; don't read the file back; on data.editor_unset ask which "
        "IDE, remember_app_preference('editor', …), re-call."
    ),
    "edit_file_search_replace": (
        "'Emma, reemplaza A por B en X' → edit_file_search_replace (literal; "
        "'todas las ocurrencias' → count=-1). Same flow as edit_file_append: speak "
        "the diff summary, wait for sí, re-call with confirmed=true; don't read "
        "the file back; on data.editor_unset ask which IDE, "
        "remember_app_preference('editor', …), re-call."
    ),
    "edit_file_replace": (
        "'Emma, sobrescribe X con esto: …' → edit_file_replace. Same flow as "
        "edit_file_append: speak the diff summary, wait for sí, re-call with "
        "confirmed=true; don't read the file back; on data.editor_unset ask which "
        "IDE, remember_app_preference('editor', …), re-call."
    ),
    "delegate_to_codex": (
        "Larger work (refactor, add a feature, fix a non-trivial bug, write tests, "
        "audit a module) → delegate_to_codex; small in-place edits (≤ 3 lines, 1 "
        "file) use the edit_file_* tools directly. ALWAYS confirm the workdir "
        "before delegating: '¿en ~/repos/myapp, y en una rama nueva?' — wait for "
        "sí, then re-call with confirmed=true. AFTER delegation, briefly say 'le "
        "pedí al agente, te voy abriendo cosas.' Then STOP — the runtime opens the "
        "project and reveals files; the notification announces completion. "
        "'¿cómo va?' → codex_status (or task_status). This (OpenAI) sub-agent is "
        "the default."
    ),
    "codex_status": "'¿cómo va?' after a delegation → codex_status (or task_status).",
    "delegate_to_claude_code": (
        "Only IF the user has the Claude Code CLI installed AND explicitly asks "
        "for Claude. delegate_to_codex is the default coding agent."
    ),
    # ---- from: Social platforms ---------------------------------------------
    "post_to_x": (
        "X / Twitter: 'tuitea: <texto>' / 'publica en X: <texto>' → post_to_x. "
        "Confirm first; the tweet posts directly to the user's X account. READ "
        "THE TEXT BACK before the user confirms; never auto-send on tone alone "
        "('estoy enojado' is not a confirmation). If post_to_x says 'No tengo "
        "permiso para publicar en X', tell the user exactly: 'Corre `python -m "
        "emma.x_setup` una vez en tu Terminal, autoriza a Emma, y listo.' Do NOT "
        "try to run setup yourself (it needs the browser)."
    ),
    "post_to_linkedin": (
        "'Publica en LinkedIn: <texto>' → post_to_linkedin (opens the composer + "
        "copies the text; LinkedIn can't prefill text reliably). Public: READ THE "
        "TEXT BACK before the user confirms; never auto-send on tone alone."
    ),
    "send_to_discord": (
        "'Manda en Discord al canal X: <texto>' → send_to_discord (webhook if set "
        "up, else explain the one-time webhook setup). READ THE TEXT BACK before "
        "the user confirms; never auto-send on tone alone."
    ),
    "send_whatsapp": (
        "'mándale a Juan en WhatsApp: <texto>' → send_whatsapp; 'to' is a contact "
        "name (resolved from your directory) or a literal number. READ THE TEXT "
        "BACK before the user confirms; never auto-send on tone alone."
    ),
}
