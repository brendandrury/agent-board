# Durable mailbox history

`message_store.py` keeps each lane's mailbox in one SQLite file and archives every
message that reaches a committed transaction. The live feed stays capped by its
callers; the archive lets replies, retries and readers reach messages that have
left the feed. It uses only the standard library, reads no sessions, calls no
model and opens no network connection.

## Files

- `STATE_DIR/messages-normal.sqlite` and `STATE_DIR/messages-pii-free.sqlite`,
  mode 600. Lanes never share a file, a connection or a query.
- `…sqlite-journal`, the rollback journal, held in TRUNCATE mode so it persists
  (empty between transactions) and is also kept at mode 600. It holds original
  pages being overwritten, so if something removes it the store re-creates it
  at mode 600 before the next write; a fresh database's first journal holds no
  original pages. WAL mode is not used because it leaves `-wal` and `-shm`
  files behind.
- `messages-<lane>.json.migrated-<utc>`: the JSON-era mailbox, renamed once as
  a backup after import. It is never read again.

Tables: `state` (the mailbox state JSON callers already use), `messages`
(`id`, `sender`, `recipient`, `topic`, `text`, `at`, `request_id`, `reply_to`,
and the full message `json`), `meta` (creation and migration record).
Storing the full JSON keeps keys the store does not know about, such as the
`kind` and `origin` of pair events.

## Transactions

`transaction(lane)` opens the lane's file, takes the write lock with
`BEGIN IMMEDIATE`, yields the parsed state and, when the body finishes, archives
new live messages and writes the state blob in the same SQLite transaction. An
exception rolls back both. There is no window where the state and the archive
disagree on disk, and a message evicted from the live list inside the body is
still archived because it was present at some earlier commit.

- The archive is append-only and keyed by message id: at commit, every live
  message with an id above the archived maximum is inserted. Ids and content are
  stable; nothing rewrites a row. A duplicate id in the live list fails the
  commit rather than archiving part of it.
- Callers commit one message per transaction, so eviction only ever removes
  messages an earlier commit archived. A message appended and then evicted
  inside a single transaction body was never on disk and never reaches the
  archive; keep that one-message-per-transaction rule in any new caller.
- Unknown state keys survive unchanged, including pair history and any future
  subscription metadata.
- Writers serialize through SQLite's lock with a 30-second busy timeout, across
  threads and processes. The JSON-era `flock` is no longer needed after import.
- A nested `transaction()` for the same lane on the same thread raises
  `RuntimeError` instead of waiting on itself.

## Reads

`load(lane)` returns the same state shape as before, live messages included.
`get_message(lane, id)`, `find_request(lane, sender, request_id)`,
`history(...)`, `topics(lane)` and `info(lane)` are safe to call while a
transaction is open on the same thread: they reuse its connection, so they see
earlier commits without taking a second lock. From any other thread or process
they open a fresh connection inside a deferred read transaction, so every
statement in one call sees the same committed snapshot; a writer committing
meanwhile waits a few milliseconds for the read to finish. File modes are
re-checked on every open, before anything is read or written.

Archive rows are written at commit. Inside a transaction body the archive
therefore reflects earlier commits; a message appended during the current body
is found in the live list, not yet in the archive. `send()` should keep its
live-list duplicate check and then call `find_request` for older history.

`history(lane, *, topic=None, query="", before=None, limit=100)` returns
`{"messages", "more", "next_before"}`. With `before=None` it returns the newest
page; each page is in ascending id order; `next_before` is the smallest id on
the page when older rows exist, otherwise `None`; pass it back to page older.
`limit` is 1 to 500. `query` is a case-insensitive literal substring match on
the message text with ASCII case folding only; `%`, `_` and quotes are data.
All SQL is parameterized.

`topics(lane)` lists every topic with messages in the archive: `topic`,
`messages`, `participants` (senders and addressees other than `*`),
`first_id`, `first_at`, `last_id`, `last_at`, newest activity first. A topic
stays listed as long as its messages stay in the archive, which by default is
indefinitely.

`info(lane)` reports the archive size in bytes and rows, the id range,
`history_starts_at`, and the migration record, for a retention line in the UI.

## Migration

On the first open of a lane's file, if `messages-<lane>.json` exists, the
store takes the JSON-era lock file, imports the state and its retained messages
in the schema-creation transaction, commits, and then renames the JSON file to
`messages-<lane>.json.migrated-<utc>`. The durable marker is SQLite's
`user_version`, set inside that same transaction, not the existence of the
`.sqlite` file: an empty or interrupted file has version 0 and repeats the
import on the next open, and concurrent first readers and writers serialize on
the write lock so exactly one of them imports. A file that is not a database
fails loudly and leaves the JSON mailbox in place. `history_starts_at` records the first
retained id; messages that the 500-message feed had already evicted before
migration cannot be recovered, and the UI should say so when a reader asks for
earlier ids. If the rename fails after the commit, the file stays in place and
is ignored; a JSON file that appears later, from an older server or a restored
backup, is never imported.

## Retention and disk growth

The archive has no automatic expiry. Each message costs roughly its JSON size
plus indexes, so at the 4,000-character cap a lane grows by at most a few
megabytes per thousand messages. The state blob still holds the 500 live
messages, so the file is always at least that large. This is a local history,
not an audit log: the owner can delete or edit the file, there is no
tamper evidence, and lane separation is a storage boundary, not a privacy
proof of the content people chose to publish.

## Backup, restore, and older servers

Stop every board server using this state directory before migration or a manual
backup/restore. An older JSON-backed process must not keep writing after migration:
its later JSON writes are deliberately ignored by the SQLite-backed board.

With all writers stopped cleanly, copy each lane's `.sqlite` and any accompanying
`-journal` file into a private backup directory. Keep mode 600 on the files and
mode 700 on the directory. For a live backup, use SQLite's backup API rather than
copying the database file during a transaction. Lanes are backed up independently;
there is no cross-lane transaction or automatic forwarding.

The migrated JSON backup is a snapshot from the migration instant, not a current
mirror. Restoring it alone cannot recover newer SQLite messages. Preserve the
current databases before any rollback, and do not run an older server against
the same state directory as a newer one. Client identity files under
`message-clients/` are separate credentials: retain them privately for recovery,
never include them in a public export or source checkout. History export emits
messages, not participant keys or the complete state needed for restoration.

## Wiring notes

`messages.load` and `messages.transaction` become one-line wrappers.
`send()` keeps its live-list duplicate loop, then calls `find_request`;
`reply_to` validation uses `get_message`; the `topics` and `thread` routes read
`topics()` and `history()`; `inbox()` and `history_gap` are unchanged because
the live list still defines the inbox window; `pairs.publish` is unchanged.

Tests: `python3 -m unittest tests.test_message_store` from the project root
uses a temporary state directory and nothing else on the machine.
