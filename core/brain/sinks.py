"""Sinks: tools whose arguments can carry data out of Emma or change the world.

BRAIN-1 Part 2. While a session is tainted (it has read attacker-reachable
content), every sink needs a spoken two-phase confirmation and every string
argument must trace back to the user's own words or a trusted result.

Enumerated BY NAME on purpose — a flag can be forgotten on a new tool; this list
is checked by ``tests/test_injection.py``: every ``destructive=True`` tool must
be here, and every name here must be a registered tool.
"""

from __future__ import annotations

# All 49 destructive tools (2026-10-13). A new destructive tool fails the test
# until it is added here.
DESTRUCTIVE: frozenset[str] = frozenset(
    {
        "app_keystroke", "app_menu_click", "browser_do", "cancel_my_task", "clone_and_open",
        "click_button", "close_duplicate_tabs", "close_tabs_matching", "complete_reminder",
        "create_event", "create_jira_issue", "create_linear_issue", "delegate_to_claude_code",
        "delegate_to_codex", "delete_event", "delete_note", "delete_reminder",
        "edit_file_append", "edit_file_prepend", "edit_file_replace",
        "edit_file_search_replace", "enroll_my_voice", "forget_my_voice", "forget_secret",
        "free_space_assist", "merge_notes", "move_item", "notion_append", "post_to_linkedin",
        "post_to_x", "remember_secret", "rename_batch", "restart_emma", "run_in_terminal",
        "run_shell_task", "run_workflow", "schedule_conditional", "send_imessage",
        "send_to", "send_to_discord", "send_whatsapp", "set_default_browser",
        "shutdown_emma", "snooze_listening", "snooze_proactivities", "type_in_field",
        "uncomplete_reminder", "undo_action_by_id", "undo_last_action",
    }
)

# Not destructive, but an argument leaves the machine (URL, query, message) or
# executes: the exfiltration and code-execution channels.
EGRESS: frozenset[str] = frozenset(
    {
        # URLs and web queries — a secret in a query string is exfiltration.
        "open_url", "open_url_safari", "browser_navigate", "browser_type", "browser_click",
        "open_in_app", "open_item", "search_web", "web_search_in_browser", "summarize_url",
        "summarize_page", "deep_research", "search_and_open", "search_github",
        "get_repo_url", "latest_video_from_creator", "rss_latest",
        # Messages that open pre-filled (not sent, but one click from it).
        "draft_to",
        # Execution: a shell, a terminal, a SQL console, a request runner.
        "run_command", "ide_terminal_send", "tableplus_query", "postman_run",
        "new_file_in_ide",
    }
)

# Not outward, but persistent: what injected text writes here, Emma later
# believes or does (memory poisoning, a planted URL scheme or AppleScript).
PERSIST: frozenset[str] = frozenset(
    {
        "remember_fact", "remember_contact", "remember_page", "remember_app",
        "remember_connection", "remember_term", "remember_user_profile",
        "remember_stt_correction", "remember_app_preference", "set_preferred_app",
        "add_vocabulary_word", "birthday_remember", "add_reminder", "create_note",
        "append_to_note",
    }
)

SINKS: frozenset[str] = DESTRUCTIVE | EGRESS | PERSIST

# Arguments that pick from a closed set the user never has to say (which
# browser, which IDE, how to sort). Exempt from the provenance check; free text
# is never in this list.
CLOSED_ARGS: frozenset[str] = frozenset(
    {"browser", "ide", "kind", "category", "visibility", "sort", "section", "issue_type",
     "depth", "model", "scope", "status", "separator", "project_mode"}
)


def is_sink(name: str) -> bool:
    return name in SINKS
