"""Durable mailbox storage: one SQLite file per lane holding the live state and an archive.

Callers keep the live ``messages`` list capped. Every message that is in that list when a
transaction commits is archived once, keyed by its id, and stays queryable after it has
left the live feed. Lanes never share a file. This module reads no sessions, calls no
model and opens no network connection.
"""
import contextlib
import datetime
import fcntl
import json
import os
import sqlite3
import threading

import agent_board as ab

LANES = ("normal", "pii-free")
SCHEMA_VERSION = 1
BUSY_SECONDS = 30
MAX_PAGE = 500
MAX_ID = 2**53 - 1  # the same bound messages.number() applies to ids
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, json TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,"
    " topic TEXT NOT NULL, text TEXT NOT NULL, at TEXT NOT NULL, request_id TEXT NOT NULL, reply_to INTEGER,"
    " json TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS messages_topic ON messages (topic, id)",
    "CREATE INDEX IF NOT EXISTS messages_request ON messages (sender, request_id)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
_local = threading.local()


def now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="microseconds")


def lane_name(value):
    if value not in LANES:
        raise ValueError("lane must be normal or pii-free")
    return value


def path(lane):
    return ab.STATE_DIR / f"messages-{lane_name(lane)}.sqlite"


def default_state():
    return {"version": 1, "next_id": 1, "agents": {}, "messages": []}


def _restrict(db):
    """Hold the database and its persistent journal at mode 600."""
    for p in (db, db.with_name(db.name + "-journal")):
        with contextlib.suppress(FileNotFoundError):
            if p.stat().st_mode & 0o777 != 0o600:
                os.chmod(p, 0o600)


def _open_transactions():
    try:
        return _local.open
    except AttributeError:
        _local.open = {}
        return _local.open


@contextlib.contextmanager
def _legacy_lock(lane):
    """The lock the JSON-era transaction() held, so a server still on that code cannot write mid-import."""
    fd = os.open(ab.STATE_DIR / f"messages-{lane}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _open(lane):
    """Connect in autocommit mode, creating the schema (and importing the legacy file) on first use."""
    db = path(lane)
    ab.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not db.exists():
        os.close(os.open(db, os.O_CREAT | os.O_RDWR, 0o600))
    _restrict(db)  # enforce private modes before any read or write, not only after our own commits
    journal = db.with_name(db.name + "-journal")
    if db.stat().st_size and not journal.exists():
        # The rollback journal holds original pages being overwritten, so it must be private before
        # the first write that follows its removal. SQLite reuses an existing empty journal and its mode.
        # A zero-page (fresh) database is the exception: SQLite deletes a pre-made journal there, and
        # the first write journals no original pages, so nothing sensitive is exposed before _restrict().
        os.close(os.open(journal, os.O_CREAT | os.O_RDWR, 0o600))
    conn = sqlite3.connect(db, timeout=BUSY_SECONDS, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        # Per-connection setting. TRUNCATE keeps the journal file in place so its mode can be held;
        # WAL would add -wal and -shm files that outlive the connection.
        conn.execute("PRAGMA journal_mode=TRUNCATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            _create(conn, lane)
    except BaseException:
        conn.close()  # a file that is not a database, or an interrupted import, must not leak a handle
        raise
    return conn


def _create(conn, lane):
    """Create the schema and import the legacy JSON mailbox once.

    PRAGMA user_version is the durable migration marker: it is set in the same transaction as the
    import, so an empty or interrupted database file (user_version 0) simply repeats the import on the
    next open, and the legacy file is only renamed after that transaction has committed.
    """
    legacy = ab.STATE_DIR / f"messages-{lane}.json"
    with _legacy_lock(lane):
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
                conn.execute("COMMIT")  # another connection created it while we waited
                return
            for sql in SCHEMA:
                conn.execute(sql)
            meta = {"created_at": now(), "history_starts_at": "1"}
            migrated = legacy.exists()
            if migrated:
                state = json.loads(legacy.read_text())
                live = state.get("messages", [])
                _archive(conn, live)
                _put_state(conn, state)
                meta.update(migrated_from=legacy.name, migrated_at=meta["created_at"], migrated_messages=str(len(live)),
                            history_starts_at=str(live[0]["id"] if live else state.get("next_id", 1)))
            conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", list(meta.items()))
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        if migrated:
            # Only after the commit, so a failure here leaves the import intact and the file ignored, never re-imported.
            stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = legacy.with_name(f"{legacy.name}.migrated-{stamp}")
            for n in range(1, 1000):
                if not backup.exists():
                    break
                backup = legacy.with_name(f"{legacy.name}.migrated-{stamp}-{n}")  # never overwrite an earlier backup
            os.rename(legacy, backup)
    _restrict(path(lane))


def _get_state(conn):
    row = conn.execute("SELECT json FROM state WHERE key = 'mailbox'").fetchone()
    return json.loads(row["json"]) if row else default_state()


def _put_state(conn, state):
    conn.execute("INSERT OR REPLACE INTO state (key, json, updated_at) VALUES ('mailbox', ?, ?)", (json.dumps(state), now()))


def _archive(conn, live):
    """Append live messages above the archived high-water mark. Ids are stable; rows are never rewritten."""
    top = conn.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
    rows = [(m["id"], m["from"], m["to"], m.get("topic") or "", m.get("text") or "", m["at"],
             m.get("request_id") or "", m.get("reply_to"), json.dumps(m)) for m in live if m["id"] > top]
    conn.executemany("INSERT INTO messages (id, sender, recipient, topic, text, at, request_id, reply_to, json)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


@contextlib.contextmanager
def _read(lane):
    """One consistent snapshot per read context.

    Inside this thread's open transaction, reuse its connection. Otherwise open a deferred read
    transaction so every statement in the body sees the same committed state; without it each
    SELECT would be its own snapshot and a commit between two of them could show a topic in one
    result and not the other.
    """
    conn = _open_transactions().get(lane_name(lane))
    if conn is not None:
        yield conn
        return
    conn = _open(lane)
    try:
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("COMMIT")  # a read transaction has nothing to roll back; this releases the shared lock
    finally:
        conn.close()


def load(lane="normal"):
    """The mailbox state in the shape callers already use; the live messages list included."""
    with _read(lane) as conn:
        return _get_state(conn)


@contextlib.contextmanager
def transaction(lane="normal"):
    """Yield mutable state; commit it and archive new live messages atomically, or roll back both.

    Archive rows are written at commit, so inside the body the archive reflects earlier commits
    only; messages appended during this transaction are found in the live list, not the archive.
    """
    lane_name(lane)
    open_ = _open_transactions()
    if lane in open_:
        raise RuntimeError(f"A {lane} transaction is already open on this thread")
    conn = _open(lane)
    try:
        conn.execute("BEGIN IMMEDIATE")
        open_[lane] = conn
        try:
            state = _get_state(conn)
            yield state
            _archive(conn, state.get("messages", []))
            _put_state(conn, state)
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
    finally:
        open_.pop(lane, None)
        conn.close()
        _restrict(path(lane))


def get_message(lane, message_id):
    """One archived message by id, or None. Safe inside an open transaction."""
    if type(message_id) is not int or not 1 <= message_id <= MAX_ID:
        raise ValueError("message_id must be a positive integer message id")
    with _read(lane) as conn:
        row = conn.execute("SELECT json FROM messages WHERE id = ?", (message_id,)).fetchone()
    return json.loads(row["json"]) if row else None


def find_request(lane, sender, request_id):
    """The earliest archived message a sender published under this request id, or None."""
    if not isinstance(sender, str) or not isinstance(request_id, str):
        raise ValueError("sender and request_id must be strings")
    with _read(lane) as conn:
        row = conn.execute("SELECT json FROM messages WHERE sender = ? AND request_id = ? ORDER BY id LIMIT 1",
                           (sender, request_id)).fetchone()
    return json.loads(row["json"]) if row else None


def history(lane="normal", *, topic=None, query="", before=None, limit=100):
    """A page of archived messages, oldest first within the page, newest page first.

    ``before`` is an exclusive upper id bound; pass the returned ``next_before`` to page back.
    ``query`` is a case-insensitive substring match on message text (ASCII folding only).
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit must be an integer from 1 to {MAX_PAGE}")
    if before is not None and (type(before) is not int or not 1 <= before <= MAX_ID):
        raise ValueError("before must be a positive integer message id")
    if topic is not None and not isinstance(topic, str):
        raise ValueError("topic must be a string")
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    where, args = [], []
    if topic is not None:
        where.append("topic = ?")
        args.append(topic)
    if query:
        where.append("instr(lower(text), lower(?)) > 0")
        args.append(query)
    if before is not None:
        where.append("id < ?")
        args.append(before)
    sql = "SELECT json FROM messages" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    with _read(lane) as conn:
        rows = [json.loads(r["json"]) for r in conn.execute(sql, args + [limit + 1])]
    more = len(rows) > limit
    page = rows[:limit][::-1]
    return {"messages": page, "more": more, "next_before": page[0]["id"] if page and more else None}


def topics(lane="normal"):
    """Every retained topic: counts, participants (addressees other than '*'), first and last ids; newest first."""
    with _read(lane) as conn:
        rows = {r["topic"]: {"topic": r["topic"], "messages": r["n"], "participants": set(), "first_id": r["first_id"],
                             "last_id": r["last_id"], "first_at": r["first_at"], "last_at": r["last_at"]}
                for r in conn.execute("SELECT topic, count(*) AS n, min(id) AS first_id, max(id) AS last_id,"
                                      " min(at) AS first_at, max(at) AS last_at FROM messages WHERE topic != '' GROUP BY topic")}
        for r in conn.execute("SELECT DISTINCT topic, sender AS who FROM messages WHERE topic != ''"
                              " UNION SELECT DISTINCT topic, recipient FROM messages WHERE topic != '' AND recipient != '*'"):
            rows[r["topic"]]["participants"].add(r["who"])
    return [{**r, "participants": sorted(r["participants"])} for r in sorted(rows.values(), key=lambda r: r["last_id"], reverse=True)]


def info(lane="normal"):
    """Retention facts for display: archive size, id range, where recoverable history starts, migration record."""
    with _read(lane) as conn:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        count, first, last = conn.execute("SELECT count(*), min(id), max(id) FROM messages").fetchone()
    return {"lane": lane, "file": path(lane).name, "bytes": path(lane).stat().st_size, "archived": count,
            "first_id": first, "last_id": last, "history_starts_at": int(meta.get("history_starts_at", 1)),
            "created_at": meta.get("created_at"), "migrated_from": meta.get("migrated_from"),
            "migrated_at": meta.get("migrated_at"),
            "migrated_messages": int(meta["migrated_messages"]) if "migrated_messages" in meta else None}
