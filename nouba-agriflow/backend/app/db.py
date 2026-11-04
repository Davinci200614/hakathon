"""One configured SQLite file, explicit transactions and additive migrations."""
import sqlite3
import traceback
from pathlib import Path
from contextlib import contextmanager
from .config import DB_PATH, resolve_db_path
from .diagnostics import event

SCHEMA_VERSION = 5
BUSY_TIMEOUT_MS = 3000
SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS catalog_terms (
            kind TEXT NOT NULL, code TEXT NOT NULL, label TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(kind,code)
        );
        CREATE TABLE IF NOT EXISTS dialogue_audit (
            message_id TEXT PRIMARY KEY, task TEXT, route TEXT,
            error_code TEXT, duration_ms REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS dialogue_outgoing (
            message_id TEXT PRIMARY KEY, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dialogue_context (
            phone TEXT PRIMARY KEY, payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS dialogue_drafts (
            phone TEXT NOT NULL, task TEXT NOT NULL, payload TEXT NOT NULL,
            pending TEXT, PRIMARY KEY(phone,task)
        );
        CREATE TABLE IF NOT EXISTS dialogue_sessions (
            phone TEXT PRIMARY KEY, task TEXT NOT NULL DEFAULT 'none',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_reply TEXT, repeats INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS dialogue_confirmations (
            phone TEXT NOT NULL, task TEXT NOT NULL, payload_hash TEXT NOT NULL,
            armed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(phone,task)
        );
        CREATE TABLE IF NOT EXISTS employment_drafts (
            phone TEXT PRIMARY KEY, payload TEXT NOT NULL,
            pending TEXT NOT NULL, awaiting_confirmation INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS employment_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_phone TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('worker','job')),
            name TEXT NOT NULL, location TEXT NOT NULL, work TEXT NOT NULL,
            details TEXT NOT NULL, contact TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_phone,kind)
        );
        CREATE TABLE IF NOT EXISTS farmers (
            phone TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, farmer_phone TEXT NOT NULL, kind TEXT NOT NULL,
            text TEXT, media_id TEXT, status TEXT DEFAULT 'received',
            error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, farmer_phone TEXT NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS conversations (
            farmer_phone TEXT PRIMARY KEY, payload TEXT NOT NULL DEFAULT '{}',
            history TEXT NOT NULL DEFAULT '[]',
            last_question_field TEXT, last_question TEXT, failed_attempts INTEGER NOT NULL DEFAULT 0,
            awaiting_confirmation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            farmer_phone TEXT NOT NULL, message_id TEXT NOT NULL,
            role TEXT NOT NULL, text TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(message_id, role)
        );
        CREATE INDEX IF NOT EXISTS conversation_turns_phone
            ON conversation_turns(farmer_phone, id);
        CREATE TABLE IF NOT EXISTS machines (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            machine_type TEXT NOT NULL, location TEXT, hourly_rate REAL,
            available INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
            machine_id INTEGER NOT NULL, start_at TEXT, end_at TEXT,
            status TEXT NOT NULL DEFAULT 'proposed'
        );
"""

# Required columns are checked on startup/diagnosis, not on every message.
REQUIRED_COLUMNS = {
    'catalog_terms': {'kind','code','label','aliases'},
    'dialogue_audit': {'message_id','task','route','error_code','duration_ms','created_at'},
    'dialogue_outgoing': {'message_id', 'payload'},
    'dialogue_context': {'phone', 'payload'},
    'dialogue_sessions': {'phone', 'task', 'updated_at', 'last_reply', 'repeats'},
    'dialogue_drafts': {'phone', 'task', 'payload', 'pending'},
    'dialogue_confirmations': {'phone', 'task', 'payload_hash', 'armed', 'created_at'},
    'employment_drafts': {'phone', 'payload', 'pending', 'awaiting_confirmation'},
    'employment_listings': {'id', 'owner_phone', 'kind', 'name', 'location', 'work', 'details', 'contact', 'status', 'created_at'},
    'farmers': {'phone', 'created_at'},
    'messages': {'id','farmer_phone','kind','text','media_id','status','error','created_at'},
    'requests': {'id','farmer_phone','payload','status','created_at','updated_at'},
    'conversations': {'farmer_phone','payload','history','awaiting_confirmation','updated_at','last_question_field','last_question','failed_attempts'},
    'conversation_turns': {'id','farmer_phone','message_id','role','text','created_at'},
    'machines': {'id','name','machine_type','location','hourly_rate','available'},
    'bookings': {'id','request_id','machine_id','start_at','end_at','status'},
}


def database_path():
    # Keep this resolution here too for tests/configured alternate paths.
    return Path(resolve_db_path(DB_PATH))


def log_database_error(exc, stage='database.transaction'):
    # Never log SQL parameters, arbitrary trigger/constraint text or locals.
    known = {
        'database is locked', 'database table is locked',
        'attempt to write a readonly database', 'unable to open database file',
        'disk I/O error', 'database or disk is full',
        'table conversations has no column named history',
        'no such column: history', 'no such table: conversations',
        'no such table: messages', 'no such table: requests',
        'ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint',
    }
    frames = [{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
              for f in traceback.extract_tb(exc.__traceback__)]
    event(stage, outcome='error', exception_type=type(exc).__name__,
          sqlite_error_code=getattr(exc,'sqlite_errorcode',None),
          sqlite_error_name=getattr(exc,'sqlite_errorname',None),
          sqlite_message=str(exc) if str(exc) in known else 'SQLite operation failed (details redacted)',
          stack_frames=frames)


@contextmanager
def connect():
    path = database_path()
    con = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS/1000)
        con.row_factory = sqlite3.Row
        con.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
        yield con
        con.commit()
    except BaseException as exc:
        if con is not None:
            try:
                con.rollback()
            except sqlite3.Error as rollback_exc:
                log_database_error(rollback_exc, 'database.rollback_failed')
        if isinstance(exc, sqlite3.Error):
            log_database_error(exc)
        raise
    finally:
        if con is not None:
            con.close()


def schema_problems(con):
    problems=[]
    for table, required in REQUIRED_COLUMNS.items():
        columns={row['name'] for row in con.execute(f'PRAGMA table_info("{table}")')}
        if not columns:
            problems.append(f'missing table: {table}')
        elif required-columns:
            problems.append(f'missing columns: {table}: '+','.join(sorted(required-columns)))
    # ON CONFLICT requires an actual unique key, not merely a named column.
    indexes=con.execute('PRAGMA index_list(conversations)').fetchall()
    unique_phone=False
    for index in indexes:
        if index['unique']:
            names=[row['name'] for row in con.execute('SELECT name FROM pragma_index_info(?)',(index['name'],))]
            if names == ['farmer_phone']:
                unique_phone=True
    if not unique_phone:
        problems.append('conversations.farmer_phone must be unique')
    return problems


def init_db():
    path=database_path()
    event('database.location', database_path=str(path), outcome='resolved')
    with connect() as con:
        # Acquire a bounded write lock before any schema changes; DDL + version
        # update roll back together. executescript would implicitly COMMIT.
        con.execute('BEGIN IMMEDIATE')
        version=con.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise sqlite3.OperationalError('unsupported database schema version')
        for statement in SCHEMA_SQL.split(';'):
            if statement.strip():
                con.execute(statement)
        columns={row['name'] for row in con.execute('PRAGMA table_info(conversations)')}
        migrated=version < SCHEMA_VERSION
        for table in ('employment_listings', 'machines'):
            existing = {row['name'] for row in con.execute(f'PRAGMA table_info({table})')}
            if 'is_demo' not in existing:
                con.execute(f'ALTER TABLE {table} ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0')
                migrated = True
        from .catalog import install_catalog
        install_catalog(con)
        if 'history' not in columns:
            con.execute("ALTER TABLE conversations ADD COLUMN history TEXT NOT NULL DEFAULT '[]'")
            migrated=True
        for column, definition in [('last_question_field','TEXT'), ('last_question','TEXT'), ('failed_attempts','INTEGER NOT NULL DEFAULT 0')]:
            if column not in columns:
                con.execute(f'ALTER TABLE conversations ADD COLUMN {column} {definition}')
                migrated=True
        problems=schema_problems(con)
        if problems:
            # Only schema identifiers from our allowlist, never customer values.
            event('database.schema', outcome='incompatible', schema_problems=problems)
            raise sqlite3.OperationalError('incompatible database schema; see database.schema diagnostics')
        con.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    event('database.migration', outcome='applied' if migrated else 'compatible', schema_version=SCHEMA_VERSION)


def diagnose():
    """Check existing schema and real main-database write ability, then rollback.

    A uniquely named probe table is created inside a transaction and rolled back;
    no customer table or record is touched, and nothing is retained.
    """
    path=database_path()
    if not path.is_file():
        raise FileNotFoundError(f'Database does not exist: {path}')
    import uuid
    with connect() as con:
        file=con.execute('PRAGMA database_list').fetchone()['file']
        problems=schema_problems(con)
        quick_check=con.execute('PRAGMA quick_check').fetchone()[0]
        probe='nouba_probe_'+uuid.uuid4().hex
        con.execute('BEGIN IMMEDIATE')
        try:
            con.execute(f'CREATE TABLE "{probe}" (value INTEGER)')
            con.execute(f'INSERT INTO "{probe}" VALUES (1)')
            if con.execute(f'SELECT value FROM "{probe}"').fetchone()[0] != 1:
                raise sqlite3.OperationalError('database probe failed')
        finally:
            con.rollback()
        return {'database_path':file,'schema_version':con.execute('PRAGMA user_version').fetchone()[0],
                'schema_problems':problems,'quick_check':quick_check,'read_write':'ok (probe rolled back)',
                'tables':sorted(REQUIRED_COLUMNS)}
