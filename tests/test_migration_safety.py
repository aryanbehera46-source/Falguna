"""Phase 1 remaining-verification pass: database migration and rollback
safety for StateStore's additive-column mechanism.

Every test in this module builds its own throwaway SQLite file under a
TemporaryDirectory -- never falguna/.falguna/ and never any file under a
connected/mounted folder. Nothing here touches production data.

What this proves, concretely:

1. `_apply_additive_column_migrations()` genuinely adds the 6 new Phase 1
   columns (site_applications.resume_source_url, tally_intake_events
   .consent_status, comm_messages.{send_method,provider_name,
   send_attempts,failure_reason}) to a database that only has the OLD
   table definitions (i.e. a real production database that predates this
   diff) -- WITHOUT losing or altering a single pre-existing row's data.
2. Running `migrate()` twice against the same (now-migrated) database is a
   true no-op the second time: no exception, no duplicate-column error,
   still zero data loss.
3. Rollback safety: if the Python code is reverted to a version that only
   knows about the OLD, smaller set of additive columns (simulating
   `git checkout` of falguna/store.py to before this diff) and is pointed
   at a database that has ALREADY been migrated with the NEW columns,
   nothing breaks -- the old code neither errors nor drops the new
   columns, and pre-existing data in both the old and new columns
   survives untouched. This is the scenario Aryan would hit if he applied
   this diff, ran the app (migrating his real .falguna/state.db in place),
   and then decided to `git checkout` the code back to the previous
   commit without touching the database file.
4. The one real (harmless) asymmetry this investigation found: a BRAND
   NEW database created via `executescript(schema_sqlite.sql)` gets
   comm_messages.send_attempts as `INTEGER NOT NULL DEFAULT 0`, but the
   additive-migration path (used for an EXISTING database) adds it as a
   plain nullable `INTEGER` -- so pre-existing comm_messages rows get
   `send_attempts IS NULL`, not `0`, after migration. This is verified
   here to confirm it is genuinely harmless: every real reader of this
   field already falls back to 0 for a falsy/NULL value (see
   falguna/hq_web.py's `m.send_attempts||0` in the two Communications
   panel templates), and the field is never used in Python-side
   arithmetic before `send_message_via_provider()` explicitly sets it to
   a real integer on first send.
"""
import re
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from falguna.store import StateStore, utcnow

SCHEMA_PATH = Path(__file__).with_name("..") / "falguna" / "schema_sqlite.sql"
# Robust against either layout (tests/ next to falguna/, or this file
# copied beside falguna/ in a scratch sandbox).
if not SCHEMA_PATH.exists():
    SCHEMA_PATH = Path(__file__).resolve().parent.parent / "falguna" / "schema_sqlite.sql"

# The 6 Phase 1 additive columns are uniquely tagged in schema_sqlite.sql
# with "-- Phase 1 R2:" / "-- Phase 1 R3:" comments (verified by grep
# against the real file: exactly these 6 lines carry that tag, nothing
# else does). Stripping any line containing this tag reconstructs the
# OLD (pre-Phase-1) table definitions byte-for-byte from the CURRENT
# schema file, without hand-maintaining a second copy of the schema that
# could silently drift out of sync with the real one.
_PHASE1_TAG = "Phase 1 R"


def _old_schema_sql() -> str:
    full = SCHEMA_PATH.read_text()
    lines = [ln for ln in full.splitlines(keepends=True) if _PHASE1_TAG not in ln]
    return "".join(lines)


class AdditiveColumnMigrationTests(unittest.TestCase):
    """Forward migration: an existing (old-schema) database with real data
    gets the new columns added, and nothing already there is disturbed."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "disposable_state.db"

    def _seed_old_schema_with_data(self):
        """Builds a database using the OLD (pre-Phase-1) table
        definitions and inserts one real-looking row per affected table,
        exactly as a real production database would look before this
        diff is ever applied."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(_old_schema_sql())
            now = utcnow()
            conn.execute(
                "INSERT INTO site_applications "
                "(id, job_id, job_title_snapshot, applicant_name, applicant_email, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("app-1", "job-1", "Backend Engineer", "Priya Shah", "priya@example.com", "new", now, now),
            )
            conn.execute(
                "INSERT INTO tally_intake_events "
                "(id, form_id, form_type, status, tally_submission_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("evt-1", "form-1", "careers", "ingested", "sub-1", now),
            )
            conn.execute(
                "INSERT INTO comm_conversations (id, subject, status, channel, department, priority, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("conv-1", "Test thread", "open", "EMAIL", "general", "normal", now, now),
            )
            conn.execute(
                "INSERT INTO comm_messages "
                "(id, conversation_id, direction, kind, body, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("msg-1", "conv-1", "OUTBOUND", "message", "Hello there", "DRAFT", now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def _column_names(self, conn: sqlite3.Connection, table: str):
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def test_migrate_adds_new_columns_to_a_pre_phase1_database_without_losing_existing_rows(self):
        self._seed_old_schema_with_data()

        # Sanity: confirm the seeded database genuinely lacks the new
        # columns before migrating, so this test cannot pass vacuously.
        conn = sqlite3.connect(str(self.db_path))
        try:
            self.assertNotIn("resume_source_url", self._column_names(conn, "site_applications"))
            self.assertNotIn("consent_status", self._column_names(conn, "tally_intake_events"))
            for col in ("send_method", "provider_name", "send_attempts", "failure_reason"):
                self.assertNotIn(col, self._column_names(conn, "comm_messages"))
        finally:
            conn.close()

        store = StateStore(self.db_path)
        try:
            store.migrate()

            # New columns now exist.
            self.assertIn("resume_source_url", self._column_names(store.db, "site_applications"))
            self.assertIn("consent_status", self._column_names(store.db, "tally_intake_events"))
            for col in ("send_method", "provider_name", "send_attempts", "failure_reason"):
                self.assertIn(col, self._column_names(store.db, "comm_messages"))

            # Pre-existing data in every OLD column is completely
            # unchanged -- this is the actual safety property that
            # matters, not just "the column exists".
            app = store.get("site_applications", "app-1")
            self.assertEqual(app["applicant_name"], "Priya Shah")
            self.assertEqual(app["applicant_email"], "priya@example.com")
            self.assertEqual(app["status"], "new")
            self.assertIsNone(app["resume_source_url"])  # new column: NULL, not fabricated

            evt = store.get("tally_intake_events", "evt-1")
            self.assertEqual(evt["tally_submission_id"], "sub-1")
            self.assertEqual(evt["status"], "ingested")
            self.assertIsNone(evt["consent_status"])

            msg = store.get("comm_messages", "msg-1")
            self.assertEqual(msg["body"], "Hello there")
            self.assertEqual(msg["status"], "DRAFT")
            self.assertIsNone(msg["send_method"])
            self.assertIsNone(msg["provider_name"])
            self.assertIsNone(msg["failure_reason"])
            # The one documented asymmetry (see module docstring, point 4):
            # additive-migrated existing rows get NULL, not 0, for
            # send_attempts, unlike a brand-new database's DEFAULT 0.
            self.assertIsNone(msg["send_attempts"])
        finally:
            store.close()

    def test_migrate_is_idempotent_when_run_twice_in_a_row(self):
        self._seed_old_schema_with_data()
        store = StateStore(self.db_path)
        try:
            store.migrate()
            after_first = store.get("comm_messages", "msg-1")
            # Second call must not raise "duplicate column name" or any
            # other error, and must not touch data already there.
            store.migrate()
            after_second = store.get("comm_messages", "msg-1")
            self.assertEqual(after_first, after_second)
            self.assertEqual(store.get("site_applications", "app-1")["applicant_name"], "Priya Shah")
        finally:
            store.close()

    def test_legacy_comm_participants_added_at_is_preserved_and_backfilled(self):
        store = StateStore(self.db_path)
        store.migrate()
        store.close()

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE comm_participants")
            conn.execute(
                "CREATE TABLE comm_participants ("
                "id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, participant_type TEXT NOT NULL, "
                "contact_id TEXT, agent_role TEXT, added_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO comm_participants VALUES (?, ?, ?, ?, ?, ?)",
                ("participant-1", "conversation-1", "customer", "contact-1", None,
                 "2026-09-26T10:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        migrated = StateStore(self.db_path)
        try:
            migrated.migrate()
            columns = self._column_names(migrated.db, "comm_participants")
            self.assertIn("added_at", columns)
            self.assertIn("created_at", columns)
            row = migrated.get("comm_participants", "participant-1")
            self.assertEqual(row["added_at"], "2026-09-26T10:00:00+00:00")
            self.assertEqual(row["created_at"], row["added_at"])
            from falguna.audit import AuditLog
            from falguna.comms import CommsStore
            from falguna.ttt_hq import NeedsAryanQueue
            audit = AuditLog(Path(self._tmp.name) / "audit.jsonl")
            comms = CommsStore(migrated, audit, NeedsAryanQueue(migrated, audit))
            contact_id = migrated.create("comm_contacts", {
                "organization_id": None, "name": "Legacy Contact", "email": "legacy@example.com",
                "phone": None, "role_title": None, "notes": None,
                "created_at": utcnow(), "updated_at": utcnow(),
            })
            conversation = comms.open_conversation(
                "WEBSITE", "general", contact_id=contact_id, actor="migration_test",
            )
            created = migrated.list(
                "comm_participants", "conversation_id=? AND participant_type=?",
                (conversation["id"], "customer"),
            )[0]
            self.assertEqual(created["added_at"], created["created_at"])
            migrated.migrate()
            self.assertEqual(migrated.get("comm_participants", "participant-1"), row)
        finally:
            migrated.close()

    def test_fresh_database_created_directly_from_current_schema_already_has_all_new_columns(self):
        """Confirms the OTHER migration path: a database that never
        existed before (schema_sqlite.sql's own CREATE TABLE statements,
        not the additive-column mechanism) already has every Phase 1
        column from the start -- so a brand-new install needs no
        additive migration for these columns at all."""
        store = StateStore(self.db_path)
        try:
            store.migrate()
            self.assertIn("resume_source_url", self._column_names(store.db, "site_applications"))
            self.assertIn("consent_status", self._column_names(store.db, "tally_intake_events"))
            for col in ("send_method", "provider_name", "send_attempts", "failure_reason"):
                self.assertIn(col, self._column_names(store.db, "comm_messages"))
        finally:
            store.close()


class RollbackSafetyTests(unittest.TestCase):
    """Simulates reverting the Python code to before this diff, while the
    database file has already been migrated forward with the new
    columns -- the exact scenario a `git checkout` of the code without
    touching .falguna/state.db would produce."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "disposable_state.db"

    def test_old_code_against_an_already_migrated_database_does_not_error_or_drop_new_columns(self):
        # Forward: migrate a fresh database with current (new) code.
        store = StateStore(self.db_path)
        try:
            store.migrate()
            now = utcnow()
            store.create("comm_conversations", {
                "id": "conv-1", "subject": "Test thread", "status": "open",
                "channel": "EMAIL", "department": "general", "priority": "normal",
                "created_at": now, "updated_at": now,
            })
            store.create("comm_messages", {
                "id": "msg-1", "conversation_id": "conv-1", "direction": "OUTBOUND", "kind": "message",
                "body": "Hello there", "status": "FAILED",
                "send_method": "provider", "provider_name": "SMTPEmailProvider",
                "send_attempts": 3, "failure_reason": "SMTP send failed (TimeoutError)",
                "created_at": now, "updated_at": now,
            })
        finally:
            store.close()

        # Rollback: simulate old code that only knows about the additive
        # columns that existed BEFORE this diff (no Phase 1 R2/R3
        # entries) by monkeypatching StateStore's own migration table for
        # the duration of this one call, mirroring a `git checkout` of
        # store.py to the previous commit.
        old_additive_columns = {
            k: v for k, v in StateStore._ADDITIVE_COLUMNS.items()
            if k not in ("site_applications", "tally_intake_events")
        }
        old_additive_columns["comm_messages"] = [("provider_message_id", "TEXT")]

        old_store = StateStore(self.db_path)
        try:
            original = StateStore._ADDITIVE_COLUMNS
            StateStore._ADDITIVE_COLUMNS = old_additive_columns
            try:
                old_store.migrate()  # must not raise
            finally:
                StateStore._ADDITIVE_COLUMNS = original

            # The new columns are still there -- old code neither drops
            # them nor errors on their presence.
            cols = {row[1] for row in old_store.db.execute("PRAGMA table_info(comm_messages)").fetchall()}
            for col in ("send_method", "provider_name", "send_attempts", "failure_reason"):
                self.assertIn(col, cols)

            # The data written by the "new" code before rollback is
            # completely intact and readable by the "old" code's own
            # SELECT * -- based get()/list() (they never enumerate an
            # explicit column list, so extra columns are simply extra,
            # unused dict keys, never an error).
            msg = old_store.get("comm_messages", "msg-1")
            self.assertEqual(msg["body"], "Hello there")
            self.assertEqual(msg["status"], "FAILED")
            self.assertEqual(msg["send_attempts"], 3)
            self.assertEqual(msg["failure_reason"], "SMTP send failed (TimeoutError)")

            # And old code can still create/update rows using only the
            # columns it knows about -- it simply never populates the
            # new ones, which remain NULL, exactly like any other
            # optional column added before this phase.
            now = utcnow()
            old_store.create("comm_messages", {
                "id": "msg-2", "conversation_id": "conv-1", "direction": "OUTBOUND", "kind": "message",
                "body": "Written by rolled-back code", "status": "DRAFT",
                "created_at": now, "updated_at": now,
            })
            msg2 = old_store.get("comm_messages", "msg-2")
            self.assertEqual(msg2["body"], "Written by rolled-back code")
            self.assertIsNone(msg2["send_method"])
            # Unlike a pre-existing row backfilled by the additive-column
            # migration (which has no DEFAULT, so it gets NULL -- see
            # point 4 in the module docstring), this row's underlying
            # table was originally created by *current* code's migrate(),
            # so comm_messages.send_attempts already carries its real
            # `NOT NULL DEFAULT 0` from schema_sqlite.sql. Old code's
            # INSERT simply omits the column and SQLite itself supplies
            # the schema's default -- 0, not NULL.
            self.assertEqual(msg2["send_attempts"], 0)
        finally:
            old_store.close()

    def test_rolled_back_code_migrating_a_fresh_database_never_creates_the_new_columns(self):
        """Confirms the flip side: old code's CREATE TABLE IF NOT EXISTS
        still runs the CURRENT schema_sqlite.sql on disk (rollback here
        means the Python code, not the schema file, in the scenario this
        investigation was asked to check -- a code-only git checkout
        with the working tree's schema file included in that checkout).
        This test isolates just the additive-column table to show its
        old, smaller dict genuinely does not add the new columns when
        they are not already present -- i.e. the additive mechanism
        itself is inert for a table it was not told to touch, which is
        what makes it safe to reason about one table at a time."""
        old_additive_columns = {
            k: v for k, v in StateStore._ADDITIVE_COLUMNS.items()
            if k not in ("site_applications", "tally_intake_events")
        }
        old_additive_columns["comm_messages"] = [("provider_message_id", "TEXT")]

        store = StateStore(self.db_path)
        try:
            original = StateStore._ADDITIVE_COLUMNS
            StateStore._ADDITIVE_COLUMNS = old_additive_columns
            try:
                # Use the OLD schema for the base CREATE TABLE too, so
                # this reproduces "old code, never-migrated database"
                # rather than mixing an old additive-list with a
                # schema file that already declares the columns inline.
                store.db.executescript(_old_schema_sql())
                store._apply_additive_column_migrations()
                store.db.commit()
            finally:
                StateStore._ADDITIVE_COLUMNS = original

            cols = {row[1] for row in store.db.execute("PRAGMA table_info(comm_messages)").fetchall()}
            for col in ("send_method", "provider_name", "send_attempts", "failure_reason"):
                self.assertNotIn(col, cols)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
