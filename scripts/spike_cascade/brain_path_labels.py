"""BRAIN-1 Part 0c — classify the 140 labelled items by what can resolve them.

python scripts/spike_cascade/brain_path_labels.py   # writes `brain_path` into labelled.json

The owner's original per-item mapping (ceiling 79.5%) was never written into the
repo, so this reproduces it from CORE-LOCAL-ARCHITECTURE.md §1. The rule for each
label, applied to the accepted tool's REQUIRED parameters:

  L1   no required argument (or one fixed by the phrase itself)
  L2   every required argument resolves deterministically from a closed set —
       app, contact, note/playlist/page/connection name, number, URL — plus at
       most ONE free-text field filled by the residue
  L3   needs the time grammar (a date, time, duration or timezone) — BRAIN-2
  TPL  no tool: STT noise -> none, safety -> refuse, object-less -> clarify,
       chit-chat -> template
  READ the tool choice is local but the spoken answer needs a model to read or
       summarise its result (screen, web, documentation)
  LLM  anaphora, multi-step, two free-text fields, role assignment between two
       slots, messy dictation, or code work

Strict on purpose: an item is L1/L2 only if Part 4's slot kinds can fill it.
Where a deterministic extension would move items (e.g. a "que se llame X con el
texto Y" field pattern), the reason says so; BRAIN-1-VERIFY.md lists them.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).parent / "data" / "labelled.json"

LABELS: dict[str, tuple[str, str]] = {
    "sc_A01": ("L2", "creator_name = residue"),
    "sc_A02": ("L2", "query = residue"),
    "sc_A03": ("L2", "percent = number"),
    "sc_A04": ("READ", "answer is the web result"),
    "sc_A05": ("L3", "timezone from a city name — time domain, not a Part 4 slot"),
    "sc_A06": ("L2", "content = residue (remember vs reminder is the gate's call)"),
    "sc_A07": ("L1", "recall_facts, no required args"),
    "sc_A08": ("READ", "capabilities doc must be summarised"),
    "sc_A09": ("LLM", "shopping search across three tools, no closed slot"),
    "sc_A11": ("L1", "current_time, no args"),
    "sc_A12": ("TPL", "chit-chat"),
    "sc_A13": ("LLM", "two free-text fields (title, body); a field pattern would make it L2"),
    "sc_A14": ("L2", "title = note name, text = residue"),
    "sc_A15": ("L2", "title = note name; read verbatim"),
    "sc_A16": ("L3", "due time 'mañana a las nueve'"),
    "sc_A17": ("L1", "today_events, no args"),
    "sc_A18": ("L1", "next_event, no args"),
    "sc_A19": ("L1", "my_repos, no required args"),
    "sc_A20": ("L2", "query = residue"),
    "sc_A21": ("LLM", "spelled file name + folder + app composition"),
    "sc_A22": ("LLM", "file path + text to write"),
    "sc_A23": ("L2", "text = residue, ide = app"),
    "sc_A24": ("L2", "command = residue (sink: confirmation)"),
    "sc_A25": ("L2", "percent = number (tool unavailable on this Mac)"),
    "sc_A26": ("L1", "mute, no args"),
    "sc_A27": ("L2", "url = 'github punto com'"),
    "sc_A28": ("L1", "list_browser_tabs, count by template"),
    "sc_A29": ("LLM", "two free-text fields (key, expansion)"),
    "sc_A30": ("READ", "capabilities doc must be summarised"),
    "sc_V01": ("TPL", "object-less search -> clarify"),
    "sc_V02": ("LLM", "anaphora 'eso'"),
    "sc_V03": ("L1", "resume, no args"),
    "sc_V05": ("TPL", "object-less message -> clarify"),
    "sc_V07": ("TPL", "object-less repo search -> clarify"),
    "sc_V08": ("TPL", "object-less 'cierra todo' -> clarify"),
    "sc_V11": ("TPL", "no call tool -> template"),
    "sc_V12": ("L1", "recall_last_action, fixed phrase"),
    "sc_V13": ("L2", "title = note name (destructive: confirmation)"),
    "sc_V15": ("LLM", "file path + replacement text"),
    "sc_V17": ("L1", "close_duplicate_tabs, no required args"),
    "sc_V19": ("LLM", "two note names with source/target roles"),
    "sc_V21": ("L2", "username = residue (tools unavailable on this Mac)"),
    "sc_V24": ("L2", "name = app 'espotify' -> Spotify"),
    "sc_V25": ("L2", "app 'espotifai' -> Spotify"),
    "sc_V26": ("L2", "page/app 'yutub' -> YouTube"),
    "sc_V27": ("L2", "name = app Cursor"),
    "sc_V28": ("L2", "query = residue"),
    "sc_V29": ("L2", "name = app 'guasap' -> WhatsApp"),
    "sc_V31": ("L1", "my_repos, no required args"),
    "sc_V32": ("L1", "today_events, no args"),
    "sc_V33": ("L1", "list_today, no args"),
    "sc_V34": ("L2", "name = page github"),
    "sc_V36": ("L2", "name = page calendar"),
    "sc_V37": ("L2", "target = connection name"),
    "sc_V39": ("L2", "name = residue, app = TablePlus"),
    "sc_V40": ("L2", "target = residue, app = Slack"),
    "sc_V41": ("L2", "target = residue, app = Things"),
    "sc_V42": ("L2", "target = residue, app = Obsidian"),
    "sc_V43": ("TPL", "safety -> refuse"),
    "sc_V44": ("TPL", "safety -> refuse"),
    "sc_V45": ("TPL", "safety -> refuse"),
    "sc_V46": ("TPL", "safety -> refuse (accept includes __none__)"),
    "sc_V47": ("TPL", "safety -> refuse"),
    "sc_V49": ("TPL", "safety -> refuse (accept includes __none__)"),
    "sc_V50": ("TPL", "safety -> refuse"),
    "sc_V56": ("READ", "search then 'dime qué encontraste'"),
    "sc_V62": ("L1", "close_current_tab, no required args"),
    "sc_V67": ("L2", "name = playlist, app = Música"),
    "sc_V68": ("LLM", "code edit"),
    "sc_V69": ("LLM", "code work"),
    "log_00": ("LLM", "two free-text fields (title, body); a field pattern would make it L2"),
    "log_01": ("L2", "title = note name"),
    "log_02": ("L2", "title = note name (destructive: confirmation)"),
    "log_03": ("L2", "name = app Spotify (mangled wake word)"),
    "log_04": ("L2", "name = page 'gitchub' -> github"),
    "log_05": ("L2", "target = connection name (fuzzy)"),
    "log_06": ("L1", "close_current_tab"),
    "log_07": ("L1", "current_time"),
    "log_08": ("L1", "current_time"),
    "log_09": ("L2", "name = playlist"),
    "log_10": ("L1", "list_notes, no required args"),
    "log_11": ("L2", "username = residue (tools unavailable on this Mac)"),
    "log_12": ("LLM", "correction depends on the previous turn"),
    "log_13": ("LLM", "correction depends on the previous turn"),
    "log_14": ("LLM", "correction depends on the previous turn"),
    "log_15": ("LLM", "depends on conversation context"),
    "log_16": ("LLM", "depends on conversation context"),
    "log_17": ("L2", "open_application name = Terminal"),
    "log_18": ("L2", "open_application name = Cursor"),
    "log_19": ("LLM", "folder by spoken name, code-switched"),
    "log_20": ("LLM", "research then act"),
    "log_21": ("LLM", "messy dictation of the post text"),
    "log_22": ("LLM", "messy dictation of the post text"),
    "log_23": ("LLM", "anaphora 'eso'"),
    "log_24": ("READ", "screen must be described"),
    "log_25": ("READ", "screen must be described"),
    "log_26": ("L2", "query = residue, app = Spotify"),
    "log_27": ("LLM", "self-corrected multi-timer request"),
    "log_28": ("LLM", "multi-step: note + timer"),
    "log_29": ("L1", "shutdown_emma"),
    "log_30": ("L1", "shutdown_emma"),
    "log_31": ("L1", "shutdown_emma (accept includes __none__)"),
    "log_32": ("L1", "close_current_tab"),
    "log_33": ("LLM", "fillers around the creator name"),
    "log_34": ("L1", "recall_last_action"),
    "log_35": ("TPL", "chit-chat"),
    "log_36": ("TPL", "chit-chat"),
    "log_37": ("TPL", "chit-chat"),
    "log_38": ("TPL", "goodbye"),
    "log_39": ("TPL", "no request"),
    "log_40": ("TPL", "no request"),
    "log_41": ("TPL", "no request"),
    "log_42": ("TPL", "STT noise -> none"),
    "log_43": ("TPL", "STT noise -> none"),
    "log_44": ("TPL", "STT noise -> none"),
    "log_45": ("TPL", "STT noise -> none"),
    "log_46": ("TPL", "STT noise -> none"),
    "log_47": ("TPL", "STT noise -> none"),
    "log_48": ("TPL", "STT noise -> none"),
    "log_49": ("TPL", "STT noise -> none"),
    "syn_00": ("LLM", "name + relation + email in one sentence"),
    "syn_01": ("L2", "name = residue, url = 'example.com/portfolio'"),
    "syn_02": ("L2", "field = github_username, value = residue"),
    "syn_03": ("L3", "date '8 de junio'"),
    "syn_04": ("LLM", "two free-text fields (word, pronunciation)"),
    "syn_05": ("L2", "category = coding, app_name = Zed"),
    "syn_06": ("LLM", "required value is not in the utterance"),
    "syn_07": ("L2", "name = app Linear, url_scheme = 'linear://'"),
    "syn_08": ("L2", "content = residue"),
    "syn_09": ("L1", "repeat_last"),
    "syn_10": ("L3", "when = 'ayer'"),
    "syn_11": ("READ", "terminal output must be read back"),
    "syn_12": ("READ", "screen must be summarised"),
    "syn_13": ("READ", "pane must be described"),
    "syn_14": ("READ", "layout must be described"),
    "syn_15": ("L2", "name = residue, yes/no by template"),
    "syn_16": ("L2", "name = residue (destructive: confirmation)"),
    "syn_17": ("READ", "window text must be read back"),
    "syn_18": ("READ", "screen must be read"),
    "syn_19": ("LLM", "multi-step, code-switched"),
}


def main() -> None:
    items = json.loads(DATA.read_text(encoding="utf-8"))
    ids = {i["id"] for i in items}
    missing, extra = ids - LABELS.keys(), LABELS.keys() - ids
    if missing or extra:
        raise SystemExit(f"labels out of sync: missing={sorted(missing)} extra={sorted(extra)}")
    for it in items:
        it["brain_path"] = LABELS[it["id"]][0]
    DATA.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
