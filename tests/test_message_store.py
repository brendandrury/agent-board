"""Synthetic storage tests: temporary state directory, no sessions, models or network."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_board as ab
import message_store as store

CAP = 3  # a small live-feed cap so eviction is cheap to reach


def message(i, sender="a", to="b", topic="plan", text=None, request_id=None):
    return {"id": i, "from": sender, "to": to, "text": text if text is not None else f"message {i}", "topic": topic,
            "reply_to": None, "request_id": request_id or f"req-{i}", "at": store.now(), "origin": "agent", "lane": "normal"}


def append(lane="normal", count=1, cap=CAP, **fields):
    """Do what send() and pairs.publish() do: one transaction per message, allocate the id, keep the live list capped."""
    out = []
    for _ in range(count):
        with store.transaction(lane) as state:
            m = message(state["next_id"], **fields)
            state["next_id"] += 1
            state["messages"] = (state["messages"] + [m])[-cap:]
            out.append(m)
    return out


class MessageStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(ab, "STATE_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)

    def mode(self, name):
        return (self.root / name).stat().st_mode & 0o777

    def test_lane_validation_and_isolation(self):
        for bad in ("", "Normal", "pii_free", None, 3):
            with self.assertRaises(ValueError):
                store.load(bad)
            with self.assertRaises(ValueError):
                with store.transaction(bad):
                    pass
        append("normal", 2)
        self.assertEqual(store.load("pii-free"), store.default_state())
        self.assertEqual(store.history("pii-free")["messages"], [])
        self.assertEqual(store.topics("pii-free"), [])
        self.assertTrue((self.root / "messages-normal.sqlite").exists())
        self.assertTrue((self.root / "messages-pii-free.sqlite").exists())
        append("pii-free", 1, topic="other")
        self.assertEqual([m["id"] for m in store.history("normal")["messages"]], [1, 2])
        self.assertEqual([m["id"] for m in store.history("pii-free")["messages"]], [1])

    def test_default_state_and_file_modes(self):
        self.assertEqual(store.load(), store.default_state())
        self.assertEqual(self.mode("messages-normal.sqlite"), 0o600)
        append()
        self.assertEqual(self.mode("messages-normal.sqlite"), 0o600)
        self.assertEqual(self.mode("messages-normal.sqlite-journal"), 0o600)
        self.assertFalse(list(self.root.glob("*-wal")) + list(self.root.glob("*-shm")))
        self.assertEqual(store.info("normal")["history_starts_at"], 1)

    def test_commit_archives_new_messages_and_preserves_unknown_keys(self):
        with store.transaction() as state:
            state["agents"]["a"] = {"id": "a", "key_hash": "x" * 64}
            state["pairs"] = {"pair-1": {"id": "pair-1", "events": [], "revision": 1}}
            state["future_subscriptions"] = {"a": ["plan"]}
        sent = append(count=2)
        state = store.load()
        self.assertEqual(state["agents"]["a"]["key_hash"], "x" * 64)
        self.assertEqual(state["pairs"]["pair-1"]["revision"], 1)
        self.assertEqual(state["future_subscriptions"], {"a": ["plan"]})
        self.assertEqual(state["next_id"], 3)
        self.assertEqual(state["messages"], sent)
        self.assertEqual(store.history()["messages"], sent)
        self.assertEqual(store.get_message("normal", 2), sent[1])
        self.assertEqual(store.find_request("normal", "a", "req-1"), sent[0])
        self.assertIsNone(store.get_message("normal", 3))
        self.assertIsNone(store.find_request("normal", "b", "req-1"))
        for bad in ("2", 0, -1, True, 2**60):
            with self.assertRaises(ValueError):
                store.get_message("normal", bad)

    def test_rollback_restores_state_and_archive(self):
        append()
        before = store.load()
        with self.assertRaises(RuntimeError):
            with store.transaction() as state:
                state["agents"]["ghost"] = {"id": "ghost"}
                state["messages"].append(message(state["next_id"]))
                state["next_id"] += 1
                raise RuntimeError("caller failed")
        self.assertEqual(store.load(), before)
        self.assertEqual(len(store.history()["messages"]), 1)
        self.assertIsNone(store.get_message("normal", 2))
        # A duplicate id in the live list is a caller bug; it fails the whole commit rather than archiving half.
        with self.assertRaises(Exception):
            with store.transaction() as state:
                state["messages"] += [message(5), message(5)]
        self.assertEqual(store.load(), before)

    def test_nested_transaction_raises_instead_of_deadlocking(self):
        with store.transaction("normal"):
            with self.assertRaises(RuntimeError):
                with store.transaction("normal"):
                    pass
            append("pii-free")  # a different lane is a different file and is fine
        append("normal")  # the lane is usable again afterwards

    def test_eviction_keeps_archive_with_stable_content(self):
        sent = append(count=10)
        live = store.load()["messages"]
        self.assertEqual([m["id"] for m in live], [8, 9, 10])
        archived = store.history(limit=50)["messages"]
        self.assertEqual(archived, sent)
        self.assertEqual(store.get_message("normal", 1), sent[0])
        # Committing again without new messages archives nothing twice.
        with store.transaction() as state:
            state["agents"]["a"] = {"id": "a"}
        with store.transaction() as state:
            state["agents"]["a"]["last_seen"] = store.now()
        self.assertEqual(store.info("normal")["archived"], 10)
        # Replies can target evicted messages through the archive; dedup survives eviction too.
        self.assertEqual(store.find_request("normal", "a", "req-2")["id"], 2)

    def test_migration_from_legacy_json(self):
        legacy = self.root / "messages-normal.json"
        kept = [message(i) for i in (5, 6, 7)]
        state = {"version": 1, "next_id": 8, "agents": {"a": {"id": "a", "key_hash": "k" * 64, "acknowledged_through": 6}},
                 "messages": kept, "pairs": {"pair-1": {"id": "pair-1", "state": "complete"}}}
        legacy.write_text(json.dumps(state))
        self.assertEqual(store.load(), state)
        self.assertFalse(legacy.exists())
        backups = list(self.root.glob("messages-normal.json.migrated-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), state)
        self.assertEqual(store.history()["messages"], kept)
        info = store.info("normal")
        self.assertEqual((info["history_starts_at"], info["migrated_messages"], info["migrated_from"]), (5, 3, "messages-normal.json"))
        # A JSON file appearing later (an older server, or a restored backup) is never re-imported.
        legacy.write_text(json.dumps(state | {"messages": [message(1)], "next_id": 2}))
        append()
        self.assertEqual([m["id"] for m in store.history()["messages"]], [5, 6, 7, 8])
        self.assertTrue(legacy.exists())
        # The pii-free lane was not touched by the normal migration.
        self.assertEqual(store.load("pii-free"), store.default_state())
        self.assertEqual(store.info("pii-free")["migrated_from"], None)

    def test_interrupted_first_initialization_still_migrates(self):
        legacy = self.root / "messages-normal.json"
        state = {"version": 1, "next_id": 4, "agents": {}, "messages": [message(i) for i in (1, 2, 3)]}
        legacy.write_text(json.dumps(state))
        db = self.root / "messages-normal.sqlite"
        # An empty file left by a crash before the schema transaction committed.
        db.touch()
        self.assertEqual(store.load(), state)
        self.assertEqual(store.info()["migrated_messages"], 3)
        self.assertFalse(legacy.exists())
        # Schema present but the version marker never committed: the import runs again, once.
        legacy.write_text(json.dumps(state))
        db.unlink()
        conn = sqlite3.connect(db)
        for sql in store.SCHEMA:
            conn.execute(sql)
        conn.commit()
        conn.close()
        self.assertEqual(store.load(), state)
        self.assertEqual([m["id"] for m in store.history()["messages"]], [1, 2, 3])
        self.assertEqual(len(list(self.root.glob("messages-normal.json.migrated-*"))), 2)
        # A file that is not a database fails loudly and leaves the legacy mailbox in place for recovery.
        legacy.write_text(json.dumps(state))
        db.write_bytes(b"not a database" * 100)
        with self.assertRaises(sqlite3.DatabaseError):
            store.load()
        self.assertTrue(legacy.exists())
        self.assertEqual(len(list(self.root.glob("messages-normal.json.migrated-*"))), 2)

    def test_concurrent_first_access_migrates_once(self):
        legacy = self.root / "messages-normal.json"
        kept = [message(i) for i in (5, 6, 7)]
        legacy.write_text(json.dumps({"version": 1, "next_id": 8, "agents": {"a": {"id": "a"}}, "messages": kept}))
        results, errors, go = [], [], threading.Event()

        def reader():
            go.wait(10)
            try:
                results.append(store.load()["agents"]["a"]["id"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def writer():
            go.wait(10)
            try:
                with store.transaction() as state:
                    state["messages"] = (state["messages"] + [message(state["next_id"])])[-CAP:]
                    state["next_id"] += 1
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)] + [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        go.set()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(results, ["a"] * 4)
        self.assertEqual(len(list(self.root.glob("messages-normal.json.migrated-*"))), 1)
        self.assertEqual([m["id"] for m in store.history()["messages"]], [5, 6, 7, 8, 9, 10, 11])
        self.assertEqual((store.info()["migrated_messages"], store.load()["next_id"]), (3, 12))

    def test_journal_is_private_before_the_first_write_after_removal(self):
        append()
        journal = self.root / "messages-normal.sqlite-journal"
        journal.unlink()
        with patch.object(store, "_restrict", lambda db: None):
            append()
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_reads_see_committed_rows_only_until_commit(self):
        append()
        entered, release = threading.Event(), threading.Event()
        inside = {}

        def writer():
            with store.transaction() as state:
                state["messages"].append(message(state["next_id"], request_id="pending"))
                state["next_id"] += 1
                # Inside our own transaction, earlier commits are visible without deadlocking.
                inside["earlier"] = store.get_message("normal", 1)
                inside["pending"] = store.find_request("normal", "a", "pending")
                entered.set()
                release.wait(10)

        t = threading.Thread(target=writer)
        t.start()
        self.assertTrue(entered.wait(10))
        try:
            self.assertEqual(store.load()["next_id"], 2)
            self.assertIsNone(store.find_request("normal", "a", "pending"))
            self.assertEqual(len(store.history()["messages"]), 1)
        finally:
            release.set()
            t.join(10)
        self.assertEqual(inside["earlier"]["id"], 1)
        self.assertIsNone(inside["pending"])
        self.assertEqual(store.find_request("normal", "a", "pending")["id"], 2)
        self.assertEqual(store.load()["next_id"], 3)

    def test_concurrent_writers_serialize(self):
        errors = []

        def worker(n):
            try:
                for _ in range(5):
                    with store.transaction() as state:
                        state["messages"] = (state["messages"] + [message(state["next_id"], sender=f"w{n}")])[-CAP:]
                        state["next_id"] += 1
            except Exception as e:  # noqa: BLE001 - surfaced through the assertion below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(store.load()["next_id"], 41)
        ids = [m["id"] for m in store.history(limit=100)["messages"]]
        self.assertEqual(ids, list(range(1, 41)))
        self.assertEqual(store.info("normal")["archived"], 40)

    def test_history_pagination_topic_and_search(self):
        append(count=20)
        append(count=5, topic="other", text="Needle 100% found_here")
        page = store.history(limit=10)
        self.assertEqual([m["id"] for m in page["messages"]], list(range(16, 26)))
        self.assertTrue(page["more"])
        self.assertEqual(page["next_before"], 16)
        page = store.history(limit=10, before=page["next_before"])
        self.assertEqual([m["id"] for m in page["messages"]], list(range(6, 16)))
        page = store.history(limit=10, before=page["next_before"])
        self.assertEqual(([m["id"] for m in page["messages"]], page["more"], page["next_before"]), (list(range(1, 6)), False, None))
        self.assertEqual(len(store.history(topic="other")["messages"]), 5)
        self.assertEqual(len(store.history(topic="missing")["messages"]), 0)
        self.assertEqual(len(store.history(query="needle")["messages"]), 5)
        self.assertEqual(len(store.history(query="MESSAGE 1")["messages"]), 11)
        self.assertEqual(len(store.history(topic="plan", query="message 2")["messages"]), 2)
        # Search is a literal substring: SQL wildcards and quotes are data, not syntax.
        self.assertEqual(len(store.history(query="100% found_here")["messages"]), 5)
        self.assertEqual(len(store.history(query="%")["messages"]), 5)
        self.assertEqual(len(store.history(query="_")["messages"]), 5)
        self.assertEqual(len(store.history(query="' OR 1=1 --")["messages"]), 0)
        for bad in ({"limit": 0}, {"limit": 501}, {"limit": True}, {"before": 0}, {"before": "5"}, {"before": 2**60}, {"query": None}, {"topic": 3}):
            with self.assertRaises(ValueError):
                store.history("normal", **bad)

    def test_topics_reads_one_snapshot_while_another_connection_commits(self):
        """A commit landing between the aggregate and participant queries must not surface a half-seen topic."""
        append(count=2, topic="plan")
        reading, attempting, finished = threading.Event(), threading.Event(), threading.Event()
        real_open = store._open

        class Spy:
            """Pause after topics() has materialized its aggregate rows so a writer can try to commit."""
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args):
                cursor = self._conn.execute(sql, *args)
                if "GROUP BY topic" in sql:
                    rows = cursor.fetchall()
                    reading.set()
                    attempting.wait(5)
                    time.sleep(.2)
                    return rows
                return cursor

        def writer():
            reading.wait(10)
            attempting.set()
            append(topic="late", sender="z", to="*")
            finished.set()

        threading.Thread(target=writer).start()
        with patch.object(store, "_open", lambda lane: Spy(real_open(lane))):
            during = store.topics()
        self.assertEqual([r["topic"] for r in during], ["plan"])
        self.assertTrue(finished.wait(10))
        after = store.topics()
        self.assertEqual([(r["topic"], r["participants"]) for r in after], [("late", ["z"]), ("plan", ["a", "b"])])

    def test_permissions_are_enforced_on_open_not_only_after_writes(self):
        append()
        db, journal = self.root / "messages-normal.sqlite", self.root / "messages-normal.sqlite-journal"
        for f in (db, journal):
            f.chmod(0o644)
        store.load()
        self.assertEqual((db.stat().st_mode & 0o777, journal.stat().st_mode & 0o777), (0o600, 0o600))

    def test_fresh_install_creates_no_legacy_lock_file(self):
        append()
        self.assertFalse((self.root / "messages-normal.lock").exists())
        (self.root / "messages-pii-free.json").write_text(json.dumps(store.default_state()))
        store.load("pii-free")
        self.assertTrue((self.root / "messages-pii-free.lock").exists())  # only a JSON-era mailbox needs the old lock

    def test_topics_directory(self):
        append(count=2, topic="plan")
        append(count=1, topic="", text="untopiced")
        append(count=1, topic="other", sender="c", to="*")
        append(count=1, topic="plan", sender="b", to="a")
        rows = store.topics("normal")
        self.assertEqual([r["topic"] for r in rows], ["plan", "other"])
        plan, other = rows
        self.assertEqual((plan["messages"], plan["first_id"], plan["last_id"], plan["participants"]), (3, 1, 5, ["a", "b"]))
        self.assertEqual((other["messages"], other["first_id"], other["last_id"], other["participants"]), (1, 4, 4, ["c"]))
        self.assertLessEqual(plan["first_at"], plan["last_at"])
        self.assertEqual(json.loads(json.dumps(rows)), rows)


if __name__ == "__main__":
    unittest.main()
