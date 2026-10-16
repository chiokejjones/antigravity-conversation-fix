Antigravity Conversation Fix v1.08

🌐 Antigravity CLI & Remote Control Support

Full support for rebuilding `~/.gemini/antigravity-cli/conversation_summaries.db`. Headless Linux servers and Remote Control clients now maintain their conversation index in sync with all conversation databases.

⚙️ Systemd Daemon Lifecycle Management

Cleanly pauses `antigravity-cli-daemon.service` before modifying the CLI index database and restarts it in a `finally` block, avoiding WAL lock collisions and corruption during rebuilds.

🛡️ Automatic SQLite Integrity Healing

Integrity pre-checks run `PRAGMA integrity_check` before modifying databases. If corruption or lock collisions are detected, the tool automatically invokes `sqlite3 .recover` to restore valid SQL and recovers into a fresh database.

🖥️ Headless Linux Server Compatibility

Interactive workspace prompts handle `EOFError` and `OSError` gracefully when run without a TTY or in scripted background tasks, defaulting safely to automatic workspace assignment.

What it fixes

• Conversations disappeared from the sidebar or Remote Control clients
• Antigravity CLI / Remote Control daemon summaries missing or corrupt
• Conversations in the wrong order
• Date-and-ID placeholder titles when a usable first prompt exists
• Recent `.db` messages disappearing from the sidebar again later
• Workspace assignments and metadata lost during rebuilds
• Conversations split across old, new, CLI, and backup data folders

How to use

1. Close Antigravity completely (File → Exit or kill it from Task Manager)
2. Download and double-click `Antigravity_Conversation_Fix.exe`
3. When prompted for workspace assignment, press Enter or 1 for automatic assignment, or press 2 to review any remaining conversations manually
4. Open Antigravity — a PC restart is normally not required; reboot only if the changes do not appear after reopening it

Notes

• No Python or developer tools needed — just download and run
• Automatically backs up your current index before making changes
• Opens conversation files read-only and never modifies them
• Preserves existing titles, created time, workspace assignments, and unknown metadata except for the targeted `.db` updated-time refresh
• Keeps the result window open after success, an ordinary stop, or an unexpected error until Enter is pressed
• Safe to run multiple times
• Windows users can run the executable; macOS and Linux users can run the Python source

⭐ If this helped you, please star the repo so others can find it!
