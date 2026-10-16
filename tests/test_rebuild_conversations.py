import base64
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rebuild_conversations as fix


def make_conversation_db(path, payloads):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE conversation_steps "
        "(idx INTEGER NOT NULL, step_type INTEGER NOT NULL, "
        "step_payload BLOB NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO conversation_steps (idx, step_type, step_payload) "
        "VALUES (?, ?, ?)",
        payloads,
    )
    conn.commit()
    conn.close()


def title_payload(title):
    nested = fix.encode_string_field(2, title)
    return fix.encode_length_delimited(19, nested)


def entry_inner_blob(entry):
    sub_message = next(fix.iter_length_delimited_fields(entry, 2))
    info_b64 = next(fix.iter_length_delimited_fields(sub_message, 1))
    return base64.b64decode(info_b64)


class ConversationTitleTests(unittest.TestCase):
    def test_formats_prompt_titles_concisely_and_deterministically(self):
        self.assertEqual(
            fix.format_prompt_title(
                "  see this link\nhttps://content.openx.xyz/api/export?videoId=abc  "
            ),
            "see this link content.openx.xyz",
        )
        self.assertEqual(
            fix.format_prompt_title("is our cli latest? i mean antigravity cli"),
            "is our cli latest?",
        )
        self.assertEqual(
            fix.format_prompt_title("# Agent Broker Request\nTopic: strict route test"),
            "Agent Broker Request Topic: strict route test",
        )
        title = fix.format_prompt_title(
            "go check the switchboard mcp request codex sent a simple "
            "translation request to another agent and report everything"
        )
        self.assertLessEqual(len(title), 60)
        self.assertLessEqual(len(title.rstrip("…").split()), 10)
        self.assertTrue(title.endswith("…"))

    def test_scans_past_invalid_type14_rows_and_keeps_database_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "conversation.db")
            make_conversation_db(
                path,
                [
                    (5, 7, title_payload("ignored")),
                    (10, 14, b"\x9a\x01\xff"),
                    (20, 14, title_payload("  Useful\n\tprompt title  ")),
                ],
            )
            with open(path, "rb") as fixture:
                before_bytes = fixture.read()
            before_mtime = os.stat(path).st_mtime_ns

            title = fix.extract_title_from_conversation_db(path)

            self.assertEqual(title, "Useful prompt title")
            with open(path, "rb") as fixture:
                self.assertEqual(fixture.read(), before_bytes)
            self.assertEqual(os.stat(path).st_mtime_ns, before_mtime)

    def test_malformed_database_or_payload_returns_none_without_creating_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = os.path.join(temp_dir, "missing.db")
            self.assertIsNone(fix.extract_title_from_conversation_db(missing))
            self.assertFalse(os.path.exists(missing))

            malformed = os.path.join(temp_dir, "malformed.db")
            make_conversation_db(malformed, [(1, 14, b"\x9a\x01\xff")])
            self.assertIsNone(fix.extract_title_from_conversation_db(malformed))

    def test_title_priority_existing_then_brain_then_conversation_then_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "abc.db")
            make_conversation_db(
                db_path, [(1, 14, title_payload("Conversation title"))]
            )

            with mock.patch.object(fix, "get_title_from_brain", return_value="Brain title"):
                self.assertEqual(
                    fix.resolve_title("abc", {"abc": "Existing title"}, db_path),
                    ("Existing title", "preserved"),
                )
                self.assertEqual(
                    fix.resolve_title("abc", {}, db_path),
                    ("Brain title", "brain"),
                )

            with mock.patch.object(fix, "get_title_from_brain", return_value=None):
                self.assertEqual(
                    fix.resolve_title("abc", {}, db_path),
                    ("Conversation title", "conversation"),
                )
                pb_path = os.path.join(temp_dir, "fallback-id.pb")
                with open(pb_path, "wb") as fixture:
                    fixture.write(b"fixture")
                fallback, source = fix.resolve_title("fallback-id", {}, pb_path)
                self.assertTrue(fallback.startswith("Conversation ("))
                self.assertEqual(source, "fallback")

    def test_migrates_only_the_exact_earlier_v107_raw_prompt_title(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "abc.db")
            prompt = (
                "go check the switchboard mcp request codex sent a simple "
                "translation request to another agent and report everything"
            )
            make_conversation_db(db_path, [(1, 14, title_payload(prompt))])
            old_v107_title = fix.normalize_prompt_text(prompt)[:80]

            with mock.patch.object(fix, "get_title_from_brain", return_value=None):
                migrated, source = fix.resolve_title(
                    "abc", {"abc": old_v107_title}, db_path
                )
                preserved, preserved_source = fix.resolve_title(
                    "abc", {"abc": old_v107_title + "!"}, db_path
                )

            self.assertNotEqual(migrated, old_v107_title)
            self.assertLessEqual(len(migrated), 60)
            self.assertEqual(source, "conversation")
            self.assertEqual(preserved, old_v107_title + "!")
            self.assertEqual(preserved_source, "preserved")
            self.assertFalse(
                fix._is_generated_fallback_title(
                    "Conversation Planning Review", "abc12345-full-id"
                )
            )
            self.assertTrue(
                fix._is_generated_fallback_title(
                    "Conversation (Aug 16) abc12345", "abc12345-full-id"
                )
            )

    def test_recovers_legacy_pb_title_from_first_explicit_user_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conversation_id = "legacy-id"
            brain_path = os.path.join(temp_dir, conversation_id)
            logs_path = os.path.join(brain_path, ".system_generated", "logs")
            os.makedirs(logs_path)
            transcript = os.path.join(logs_path, "transcript.jsonl")
            with open(transcript, "w", encoding="utf-8") as fixture:
                fixture.write("{truncated json\n")
                fixture.write('{"type":"USER_INPUT","content":"partial"\n')
                fixture.write('{"type":"TOOL_OUTPUT","content":"ignore me"}\n')
                fixture.write(
                    '{"event_type":"USER_EXPLICIT","payload":'
                    '{"content":"<USER_REQUEST>\\nFirst legacy request. More text'
                    '\\n</USER_REQUEST>\\n<ADDITIONAL_METADATA>ignore</ADDITIONAL_METADATA>"}}\n'
                )
                fixture.write(
                    '{"type":"USER_INPUT","content":"Second request"}\n'
                )
            pb_path = os.path.join(temp_dir, conversation_id + ".pb")
            with open(pb_path, "wb") as fixture:
                fixture.write(b"opaque")

            with mock.patch.object(fix, "_ALL_BRAIN_DIRS", [temp_dir]), \
                    mock.patch.object(fix, "get_title_from_brain", return_value=None):
                title, source = fix.resolve_title(conversation_id, {}, pb_path)

            self.assertEqual(title, "First legacy request.")
            self.assertEqual(source, "conversation")

    def test_legacy_overview_is_tolerant_and_unreadable_pb_falls_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conversation_id = "overview-id"
            logs_path = os.path.join(
                temp_dir, conversation_id, ".system_generated", "logs"
            )
            os.makedirs(logs_path)
            with open(os.path.join(logs_path, "overview.txt"), "w", encoding="utf-8") as fixture:
                fixture.write(
                    '{"source":"USER_EXPLICIT","type":"USER_INPUT",'
                    '"content":"<USER_REQUEST>\\nset a shut down in 30 minutes'
                    '\\n<ADDITIONAL_METADATA>truncated"}\n'
                )
            pb_path = os.path.join(temp_dir, conversation_id + ".pb")
            with open(pb_path, "wb") as fixture:
                fixture.write(b"encrypted")

            with mock.patch.object(fix, "_ALL_BRAIN_DIRS", [temp_dir]), \
                    mock.patch.object(fix, "get_title_from_brain", return_value=None):
                self.assertEqual(
                    fix.resolve_title(conversation_id, {}, pb_path),
                    ("set a shut down in 30 minutes", "conversation"),
                )
                fallback, source = fix.resolve_title("missing-artifact", {}, pb_path)

            self.assertTrue(fallback.startswith("Conversation ("))
            self.assertEqual(source, "fallback")

    def test_brain_heading_can_follow_leading_blank_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conversation_id = "heading-id"
            brain_path = os.path.join(temp_dir, conversation_id)
            os.makedirs(brain_path)
            with open(os.path.join(brain_path, "plan.md"), "w", encoding="utf-8") as fixture:
                fixture.write("\n\n# Recovered Plan Title\n\nDetails")

            with mock.patch.object(fix, "_ALL_BRAIN_DIRS", [temp_dir]):
                self.assertEqual(
                    fix.get_title_from_brain(conversation_id),
                    "Recovered Plan Title",
                )


class ConversationCatalogTests(unittest.TestCase):
    def test_prefers_db_within_directory_but_preserves_directory_priority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = os.path.join(temp_dir, "first")
            second = os.path.join(temp_dir, "second")
            os.makedirs(first)
            os.makedirs(second)
            for path in (
                os.path.join(first, "same.pb"),
                os.path.join(first, "same.db"),
                os.path.join(first, "priority.pb"),
                os.path.join(second, "priority.db"),
            ):
                with open(path, "wb") as fixture:
                    fixture.write(b"fixture")

            with mock.patch.object(fix, "_ALL_CONV_DIRS", [first, second]):
                catalog = fix._collect_all_conversations()

            self.assertEqual(catalog["same"], os.path.join(first, "same.db"))
            self.assertEqual(catalog["priority"], os.path.join(first, "priority.pb"))


class TimestampRefreshTests(unittest.TestCase):
    def setUp(self):
        self.field7 = fix.build_timestamp_field(7, 70)
        self.field10 = fix.build_timestamp_field(10, 80)
        self.workspace = fix.encode_length_delimited(9, b"\x0a\x03abc")
        self.unknown = fix.encode_string_field(12, "opaque-state")
        self.existing = (
            fix.encode_string_field(1, "Old title")
            + fix.build_timestamp_field(3, 100)
            + self.field7
            + self.field10
            + self.workspace
            + self.unknown
            + fix.build_timestamp_field(3, 110)
        )

    def build_inner(self, mtime, source_is_db=True, existing=None):
        entry = fix.build_trajectory_entry(
            "conversation-id",
            "New title",
            self.existing if existing is None else existing,
            conversation_mtime=mtime,
            source_is_db=source_is_db,
        )
        return entry_inner_blob(entry)

    def test_db_timestamp_noop_when_file_is_not_newer(self):
        inner = self.build_inner(109)
        self.assertEqual(
            list(fix.iter_length_delimited_fields(inner, 3)),
            list(fix.iter_length_delimited_fields(self.existing, 3)),
        )

    def test_db_refresh_removes_duplicate_field3_and_preserves_other_bytes(self):
        inner = self.build_inner(120)
        field3_values = list(fix.iter_length_delimited_fields(inner, 3))
        self.assertEqual(len(field3_values), 1)
        self.assertEqual(fix.extract_timestamp_seconds(inner, 3), 120)
        self.assertEqual(list(fix.iter_length_delimited_fields(inner, 7)), [b"\x08F"])
        self.assertEqual(list(fix.iter_length_delimited_fields(inner, 10)), [b"\x08P"])
        self.assertEqual(list(fix.iter_length_delimited_fields(inner, 9)), [b"\x0a\x03abc"])
        self.assertEqual(
            list(fix.iter_length_delimited_fields(inner, 12)), [b"opaque-state"]
        )

    def test_db_missing_field3_adds_only_field3(self):
        existing = (
            fix.encode_string_field(1, "Old title")
            + self.field7
            + self.field10
            + self.workspace
            + self.unknown
        )
        inner = self.build_inner(120, existing=existing)
        self.assertEqual(fix.extract_timestamp_seconds(inner, 3), 120)
        self.assertEqual(len(list(fix.iter_length_delimited_fields(inner, 7))), 1)
        self.assertEqual(len(list(fix.iter_length_delimited_fields(inner, 10))), 1)

    def test_pb_timestamp_is_not_refreshed(self):
        inner = self.build_inner(999, source_is_db=False)
        self.assertEqual(fix.extract_timestamp_seconds(inner, 3), 110)
        self.assertEqual(len(list(fix.iter_length_delimited_fields(inner, 3))), 2)


class ActiveProcessTests(unittest.TestCase):
    def test_posix_check_cannot_match_script_or_repository_path(self):
        result = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(fix, "_SYSTEM", "Linux"), \
                mock.patch.object(fix, "_IS_WSL", False), \
                mock.patch.object(fix.subprocess, "run", return_value=result) as run:
            self.assertFalse(fix.is_antigravity_running())

        self.assertTrue(run.call_args_list)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][0:2], ['pgrep', '-x'])
            self.assertNotIn('-f', call.args[0])

    def test_active_process_aborts_before_update_discovery_or_write(self):
        with mock.patch.object(fix, "is_antigravity_running", return_value=True), \
                mock.patch.object(fix, "check_for_updates") as update, \
                mock.patch.object(fix, "_collect_all_conversations") as discover, \
                mock.patch.object(fix, "write_index_to_database") as write:
            result = fix.main()

        self.assertEqual(result, 1)
        update.assert_not_called()
        discover.assert_not_called()
        write.assert_not_called()


class CliWrapperTests(unittest.TestCase):
    def test_cli_wrapper_pauses_once_after_an_ordinary_return(self):
        with mock.patch.object(fix, "main", return_value=0), \
                mock.patch("builtins.input", return_value="") as prompt:
            result = fix.run_cli()

        self.assertEqual(result, 0)
        prompt.assert_called_once_with("\n  Finished. Press Enter to close...")

    def test_cli_wrapper_pauses_once_after_an_unexpected_exception(self):
        with mock.patch.object(fix, "main", side_effect=RuntimeError("boom")), \
                mock.patch("builtins.input", return_value="") as prompt:
            result = fix.run_cli()

        self.assertEqual(result, 1)
        prompt.assert_called_once_with("\n  Finished. Press Enter to close...")

    def test_direct_main_active_process_return_does_not_pause(self):
        with mock.patch.object(fix, "is_antigravity_running", return_value=True), \
                mock.patch("builtins.input") as prompt:
            result = fix.main()

        self.assertEqual(result, 1)
        prompt.assert_not_called()


class CliSummariesDbTests(unittest.TestCase):
    def test_find_cli_summaries_db(self):
        expected = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
        self.assertEqual(fix.find_cli_summaries_db(), expected)

    def test_inspect_cli_conversation_db_non_existent(self):
        res = fix.inspect_cli_conversation_db(Path("/non/existent/db.db"))
        self.assertEqual(res, {"step_count": 0, "title": None, "workspace_uri": None})

    def test_inspect_cli_conversation_db_with_steps_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test_conv.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE steps (id INTEGER PRIMARY KEY, content TEXT)")
            conn.execute("INSERT INTO steps VALUES (1, 'step1'), (2, 'step2')")
            conn.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, blob BLOB)")
            metadata_blob = b"prefix file:///home/user/project\x00suffix"
            conn.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (metadata_blob,))
            conn.commit()
            conn.close()

            res = fix.inspect_cli_conversation_db(db_path)
            self.assertEqual(res["step_count"], 2)
            self.assertEqual(res["workspace_uri"], "file:///home/user/project")

    def test_ensure_db_integrity_clean_and_non_existent(self):
        fix.ensure_db_integrity(Path("/non/existent/db.db"))

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "clean.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.commit()
            conn.close()

            fix.ensure_db_integrity(db_path)
            self.assertTrue(db_path.exists())
            self.assertFalse(db_path.with_name("clean.db.corrupt").exists())

    def test_ensure_db_integrity_corrupt_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "corrupt.db"
            with open(db_path, "wb") as f:
                f.write(b"NOT A SQLITE DATABASE FILE")
            wal_path = Path(str(db_path) + "-wal")
            shm_path = Path(str(db_path) + "-shm")
            wal_path.write_text("wal")
            shm_path.write_text("shm")

            mock_proc = mock.Mock(returncode=0, stdout="CREATE TABLE t (y INTEGER);")
            with mock.patch("subprocess.run", return_value=mock_proc):
                fix.ensure_db_integrity(db_path)

            self.assertTrue(db_path.with_name("corrupt.db.corrupt").exists())
            self.assertFalse(wal_path.exists())
            self.assertFalse(shm_path.exists())
            self.assertTrue(db_path.exists())

            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cur.fetchall()]
            conn.close()
            self.assertIn("t", tables)

    def test_rebuild_cli_summaries_db_creation_and_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "conversation_summaries.db"
            convs = [{
                "id": "cid-1",
                "title": "First Conversation",
                "preview": "Preview text",
                "step_count": 5,
                "mtime": 1700000000.0,
                "workspace_uri": "file:///workspace/project",
                "raw_summary_bytes": b"proto_blob_1",
                "project_id": "proj-1",
            }]
            count = fix.rebuild_cli_summaries_db(db_path, convs)
            self.assertEqual(count, 1)

            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT conversation_id, title, step_count, raw_summary FROM conversation_summaries")
            row = cur.fetchone()
            conn.close()
            self.assertEqual(row[0], "cid-1")
            self.assertEqual(row[1], "First Conversation")
            self.assertEqual(row[2], 5)
            self.assertEqual(row[3], b"proto_blob_1")

            count2 = fix.rebuild_cli_summaries_db(db_path, convs)
            self.assertEqual(count2, 1)
            self.assertTrue(db_path.with_suffix(".db.bak").exists())

    def test_rebuild_cli_summaries_db_conflict_updates_and_extra_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "conversation_summaries.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT "",
                    preview TEXT NOT NULL DEFAULT "",
                    step_count INTEGER NOT NULL DEFAULT 0,
                    last_modified_time DATETIME NOT NULL,
                    workspace_uris TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT "",
                    source TEXT NOT NULL DEFAULT "",
                    project_id TEXT NOT NULL DEFAULT "",
                    agent_name TEXT NOT NULL DEFAULT "",
                    parent_conversation_id TEXT NOT NULL DEFAULT "",
                    raw_summary BLOB,
                    group_id TEXT NOT NULL DEFAULT '',
                    last_user_input_time DATETIME NOT NULL
                );
            """)
            conn.execute("""
                INSERT INTO conversation_summaries VALUES (
                    'cid-1', 'Conversation 12345678', '', 2, '2025-01-01 00:00:00',
                    '[]', '', '', '', '', '', NULL, '', '2025-01-01 00:00:00'
                );
            """)
            conn.commit()
            conn.close()

            convs = [{
                "id": "cid-1",
                "title": "Better Real Title",
                "preview": "New preview",
                "step_count": 10,
                "mtime": 1710000000.0,
                "workspace_uri": "file:///updated/ws",
                "raw_summary_bytes": b"new_raw_blob",
                "project_id": "proj-1",
            }]
            fix.rebuild_cli_summaries_db(db_path, convs)

            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT title, preview, step_count, workspace_uris, raw_summary FROM conversation_summaries WHERE conversation_id='cid-1'")
            row = cur.fetchone()
            conn.close()
            self.assertEqual(row[0], "Better Real Title")
            self.assertEqual(row[1], "New preview")
            self.assertEqual(row[2], 10)
            self.assertIn("file:///updated/ws", row[3])
            self.assertEqual(row[4], b"new_raw_blob")


class DaemonLifecycleTests(unittest.TestCase):
    def test_is_remote_control_daemon_active(self):
        active_proc = mock.Mock(returncode=0, stdout="active\n")
        inactive_proc = mock.Mock(returncode=1, stdout="inactive\n")

        with mock.patch("subprocess.run", return_value=active_proc):
            self.assertTrue(fix.is_remote_control_daemon_active())

        with mock.patch("subprocess.run", return_value=inactive_proc):
            self.assertFalse(fix.is_remote_control_daemon_active())

    def test_daemon_stop_and_start(self):
        success = mock.Mock(returncode=0)
        with mock.patch("subprocess.run", return_value=success) as mock_run:
            fix.stop_remote_control_daemon()
            fix.start_remote_control_daemon()
            self.assertEqual(mock_run.call_count, 2)


class HeadlessMainTests(unittest.TestCase):
    def test_headless_auto_assigns_on_eof_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cli_db = Path(temp_dir) / "conversation_summaries.db"
            conv_file = Path(temp_dir) / "test-conv.db"
            make_conversation_db(str(conv_file), [(1, 14, title_payload("Prompt title"))])

            with mock.patch.object(fix, "find_cli_summaries_db", return_value=cli_db), \
                 mock.patch.object(fix, "DB_PATHS", []), \
                 mock.patch.object(fix, "is_antigravity_running", return_value=False), \
                 mock.patch.object(fix, "check_for_updates"), \
                 mock.patch.object(fix, "_collect_all_conversations", return_value={"test-conv": str(conv_file)}), \
                 mock.patch.object(fix, "is_remote_control_daemon_active", return_value=False), \
                 mock.patch("builtins.input", side_effect=EOFError):
                exit_code = fix.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue(cli_db.exists())


if __name__ == "__main__":
    unittest.main()
