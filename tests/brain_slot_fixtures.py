"""Expected slot values for every L2 item in the labelled set (BRAIN-1 Part 4).

Written from the utterances BEFORE the resolver existed, so they say what a
person meant, not what the code happens to produce. Conventions:

* apps / notes / playlists / pages / connections -> the canonical name in the
  enumeration below ("Spotify", "Compras")
* free text (residue) -> lowercase display form, accents kept ("comprar café")
* numbers -> int; URLs -> bare lowercase domain[/path]

An item with several acceptable tools lists each tool it may resolve to. Every
L2 item must resolve exactly — none is "expected to fail" (BRAIN-1.5 fixed
sc_V26 with a closed site table and reclassified log_26 to LLM).
"""

from __future__ import annotations

# The "machine" the fixtures resolve against: what the adapters would read from
# a Mac with these apps installed and these notes/playlists/connections.
APPS = (
    "Spotify", "Cursor", "WhatsApp", "Terminal", "Slack", "Things", "Obsidian", "TablePlus",
    "Zed", "Linear", "Music", "Safari", "Notes", "Visual Studio Code", "Finder", "Xcode",
    "YouTube",
)
NOTES = ("Compras", "Pendientes", "Ideas")
PLAYLISTS = ("Classical Essentials", "Focus")
CONNECTIONS = ("learning-rots-local", "analytics-prod")
# config/dictionary.toml [pages.*] keys and titles (the shipped template).
PAGES = {
    "github": "Mi GitHub",
    "github_inbox": "GitHub notifications",
    "linear": "Linear",
    "calendar": "Google Calendar",
}

Slots = dict[str, object]

EXPECTED: dict[str, dict[str, Slots]] = {
    "sc_A01": {"latest_video_from_creator": {"creator_name": "nill ojeda"}},
    "sc_A02": {"play_track": {"query": "bad bunny"}},
    "sc_A03": {"set_volume": {"percent": 70}},
    "sc_A06": {"remember_fact": {"content": "soy alérgico a los mariscos"}},
    "sc_A14": {"append_to_note": {"title": "Compras", "text": "comprar café"}},
    "sc_A15": {"read_note": {"title": "Compras"}},
    "sc_A20": {"search_github": {"query": "pytorch"}},
    "sc_A23": {"ide_terminal_send": {"text": "ls", "ide": "Cursor"}},
    "sc_A24": {"run_command": {"command": "uptime"}},
    "sc_A25": {"set_brightness": {"percent": 80}},
    "sc_A27": {"open_url": {"url": "github.com"}},
    "sc_V13": {"delete_note": {"title": "Compras"}},
    "sc_V21": {"search_github": {"query": "examplehandle"}, "my_repos": {}},
    "sc_V24": {"open_application": {"name": "Spotify"}},
    "sc_V25": {"open_application": {"name": "Spotify"}, "resume": {}},
    "sc_V26": {"open_url": {"url": "youtube.com"}},
    "sc_V27": {"open_application": {"name": "Cursor"}},
    "sc_V28": {"play_track": {"query": "bad bani"}},
    "sc_V29": {"open_application": {"name": "WhatsApp"}},
    "sc_V34": {"open_my_page": {"name": "github"}},
    "sc_V36": {"open_my_page": {"name": "calendar"}},
    "sc_V37": {"open_in_app": {"target": "learning-rots-local"}},
    "sc_V39": {"remember_connection": {"name": "analytics-prod", "app": "TablePlus"}},
    "sc_V40": {"open_in_app": {"target": "general", "app": "Slack"}},
    "sc_V41": {"open_in_app": {"target": "comprar pan", "app": "Things"}},
    "sc_V42": {"open_in_app": {"target": "ideas", "app": "Obsidian"}},
    "sc_V67": {"play_playlist": {"name": "Classical Essentials", "app": "Music"}},
    "log_01": {"read_note": {"title": "Compras"}},
    "log_02": {"delete_note": {"title": "Compras"}},
    "log_03": {"open_application": {"name": "Spotify"}},
    "log_04": {"open_my_page": {"name": "github"}},
    "log_05": {"open_in_app": {"target": "learning-rots-local"}},
    "log_09": {"play_playlist": {"name": "Classical Essentials", "app": "Music"}},
    "log_11": {"search_github": {"query": "examplehandle"}},
    "log_17": {"open_application": {"name": "Terminal"}},
    "log_18": {"open_application": {"name": "Cursor"}},
    "syn_01": {"remember_page": {"name": "portafolio", "url": "example.com/portfolio"}},
    "syn_02": {"remember_user_profile": {"field": "github_username", "value": "examplehandle"}},
    "syn_05": {"set_preferred_app": {"category": "ide", "app_name": "Zed"}},
    "syn_07": {"remember_app": {"name": "Linear", "url_scheme": "linear://"}},
    "syn_08": {"remember_fact": {"content": "trabajo desde casa los viernes"}},
    "syn_15": {"find_button": {"name": "aceptar"}},
    "syn_16": {"click_button": {"name": "cancelar"}},
}
