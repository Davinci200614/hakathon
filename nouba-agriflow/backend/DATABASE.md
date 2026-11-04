# SQLite persistence: diagnosis, migration and operations

## Confirmed cause

The existing file was inspected before changing schema. Its `conversations` table
had `farmer_phone`, `payload`, `awaiting_confirmation` and `updated_at`, but no
`history`. The production upsert referenced `history`. `CREATE TABLE IF NOT EXISTS`
does not update an existing table. The error was reproduced with the actual handler
against a SQLite backup copied into an isolated temporary database:

```text
Traceback (most recent call last):
  File "<stdin>", line 15, in traced_connect
  File "/home/davinci/hakathon/nouba-agriflow/backend/app/service.py", line 196, in _handle_message
    con.execute('''INSERT INTO conversations(farmer_phone,payload,history,awaiting_confirmation)
sqlite3.OperationalError: table conversations has no column named history
```

That is the **pre-fix** source line. The generic service exception handler then
replaced it with `RuntimeError('message_processing_failed')` and sent no reply.
No Gemini/Ollama failure was involved in this reproduced database error.

## File and initialization

Active database:

```text
/home/davinci/hakathon/nouba-agriflow/backend/nouba.db
```

`backend/.env` setting `DB_PATH` controls its location. The existing value was
`nouba.db`. Relative settings now resolve against the **backend directory**, not
against the shell's working directory. Absolute settings are honored. Neither
`.env` nor its credentials were changed. All API and service connections use the
same `db.database_path()` helper.

FastAPI's lifespan calls `app.db.init_db()` before serving requests. Startup logs
`database.location` with the resolved absolute path and `database.migration` with
the schema version/outcome. Missing parent directories are created. Initialization
takes a real SQLite write lock and validates the schema; an unwritable file or
incompatible database fails startup rather than accepting messages it cannot save.

There is no JSON persistence file. JSON appears as values inside SQLite TEXT
columns, chiefly `payload` and `history`.

## Migration applied

Schema version is tracked with `PRAGMA user_version` (now **1**).

```sql
ALTER TABLE conversations ADD COLUMN history TEXT NOT NULL DEFAULT '[]';
```

This statement runs only when `PRAGMA table_info(conversations)` shows the column
is missing. Existing payload, confirmation state and timestamps are retained.
Repeated initialization leaves the schema compatible. DDL, compatibility checks
and schema version updates share one explicit transaction. Unsupported schema
changes fail and roll back instead of dropping/rebuilding tables.

Before the actual migration a SQLite backup was created at:

```text
/home/davinci/hakathon/nouba-agriflow/backend/nouba.before-history-migration-20261103T151959783735.db
```

All pre-existing record values were fingerprint-checked against the active file
after migration using their original columns: 2 farmer rows and 17 message rows
were preserved; the other five customer tables were empty. No synthetic customer
records were inserted into the active database. The backup is not the active file.
Keep it private; it contains the original customer data.

## Tables

| Table | Purpose / important fields |
| --- | --- |
| `farmers` | WhatsApp number (`phone`, primary key), creation time. |
| `messages` | Unique Meta message ID, farmer reference, kind, text/transcript, media ID, processing status/error and receipt time. |
| `conversations` | One active draft per `farmer_phone` (primary key), JSON `payload`, JSON `history`, `awaiting_confirmation`, `updated_at`. |
| `conversation_turns` | Cached assistant replies; unique `(message_id, role)` prevents duplicate cached replies. |
| `requests` | Explicitly confirmed farmer requests; JSON `payload`, farmer reference, status and timestamps. |
| `machines` | Existing inventory fields; unchanged by this fix. |
| `bookings` | Existing machine-allocation fields (`request_id`, `machine_id`, dates/status); unchanged by this fix. |

SQLite also manages `sqlite_sequence` internally for autoincrement IDs.

Draft facts and recent conversation turns persist after each successfully handled
message. Confirmation inserts a `pending_review` request (or promotes an existing
legacy draft), removes the active conversation, and caches the acknowledgement in
one transaction. The existing `/api/requests` dashboard endpoint reads these rows.
The separate `bookings` table requires an actual machine allocation; this fix does
not invent a machine ID, availability or cooperative approval.

## Transactions, locking and failures

Every `connect()` transaction commits on success and explicitly rolls back on
failure, then closes the connection. SQLite busy/lock waits are bounded at 3 seconds;
whole message transactions are not blindly replayed. Schema migration uses
`BEGIN IMMEDIATE` and avoids `executescript`'s implicit-commit behavior.

Diagnostics contain SQLite error code/name, allowlisted error messages and stack
frame file/line/function metadata. They never include SQL parameters, phone
numbers, message text, locals or arbitrary constraint/trigger strings.

The service catches SQLite errors around the entire message flow, including
initial receipt persistence. It attempts to mark the message retryable (`error`,
`database_persistence_failed`), but that update is optional if the database itself
is unavailable. It then sends this reply independently of SQLite:

> سمح لينا، وقع مشكل تقني وماقدرناش نسجلو الطلب ديالك دابا. عاود حاول من بعد.

A failed confirmation transaction creates no request and leaves its draft intact.
A retry can then save it once. If Meta has already accepted a successful reply and
only the final delivery-status update fails, the service logs that error without
sending a contradictory second message. Normal processed duplicates are skipped.
Remote send timeouts remain inherently ambiguous without a Meta idempotency key;
unit tests cannot prove exactly-once network delivery.

## Inspect on Ubuntu

If needed, install the SQLite CLI locally with `sudo apt install sqlite3`. These
commands inspect schema and metadata without printing message contents or phones:

```bash
sqlite3 /home/davinci/hakathon/nouba-agriflow/backend/nouba.db ".databases"
sqlite3 /home/davinci/hakathon/nouba-agriflow/backend/nouba.db ".tables"
sqlite3 /home/davinci/hakathon/nouba-agriflow/backend/nouba.db ".schema conversations"
sqlite3 /home/davinci/hakathon/nouba-agriflow/backend/nouba.db "PRAGMA user_version; PRAGMA quick_check;"
sqlite3 /home/davinci/hakathon/nouba-agriflow/backend/nouba.db "SELECT status,count(*) FROM messages GROUP BY status;"
sqlite3 /home/davinci/hakathon/nouba-agriflow/backend/nouba.db "SELECT id,status,created_at FROM requests ORDER BY id DESC LIMIT 10;"
```

Do not remove the database or restore the old backup over the migrated file to
apply this fix. For a backup while the app is running, use SQLite's `.backup` or
Python's `Connection.backup`, which produce a consistent snapshot.

## Lightweight diagnostic

```bash
cd /home/davinci/hakathon/nouba-agriflow/backend
.venv/bin/python -m scripts.check_database
```

It reports the path SQLite actually opened, required column/key compatibility,
schema version, `quick_check`, and basic main-database read/write functionality.
The write probe creates a uniquely named diagnostic table, inserts/reads one
integer, then **rolls the transaction back**. No customer table or record is
modified and no probe table remains. The command does not silently migrate a
schema or create a missing database.

## Restart and real WhatsApp acceptance test

Stop the old FastAPI process with Ctrl-C, retaining the working Ollama and ngrok
sessions and all existing Meta settings. Then run:

```bash
cd /home/davinci/hakathon/nouba-agriflow/backend
.venv/bin/python -m scripts.check_database
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --log-level info
```

Use your existing tunnel's backend port if different from 8000. Confirm the startup
path is the absolute path above and schema version is 1. Send new WhatsApp messages:

1. `بغيت تراكتور يحرث ليا جوج هكتارات` → location question.
2. Stop/restart FastAPI with the same command (keep ngrok unchanged).
3. `فالرماني` → date question, with tractor/plowing/2 hectares retained.
4. `الثلاثاء الجاي` → full summary with an explicit calendar date and confirmation question.
5. `نعم` → one `pending_review` request in `/api/requests` and a cooperative-review acknowledgement.

Inspect `conversation.persist` and `reply.delivery` logs and verify each reply on
the actual phone. No request should appear in the dashboard before step 5. A Meta
HTTP 200 proves API acceptance; actual WhatsApp delivery still requires this phone
check. No real WhatsApp delivery was claimed or initiated by this database repair.

**Verification result: 65 tests passed, 0 failed.** The actual migrated database
also passed schema compatibility, quick_check and rolled-back read/write checks.

Automated regression tests include the original missing-history failure, additive
migration and preservation of old conversation/booking data, initialization,
working-directory independence, full conversation continuity across five separate
Python/FastAPI startups, rollback, lock timeout, failure replies, safe retries and
duplicate confirmation handling. AI and Meta are mocked; no API credits are used.
