"""
Database connection helpers and schema for smart.park.

Provides:
  - get_connection()  : opens a sqlite3 connection with sane defaults
  - get_cursor()      : context manager for safe read or write transactions
  - Database          : convenience class (execute, fetch_one, fetch_all, count)
  - init_db()         : creates all tables on first run (call once at startup)

Thread safety: writes are serialised with a process-wide lock; reads
run concurrently (SQLite WAL mode).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from config import Config

# A process-wide lock serialises writes. SQLite allows concurrent readers
# but only one writer; without this, two simultaneous check-ins could hit
# "database is locked" errors.
_write_lock = threading.Lock()
# Create a thread lock to prevent multiple threads from writing to a shared resource at the same time

#Converts SQLite query results into your custom Row objects.
def _row_factory(cursor: sqlite3.Cursor, row: Sequence[Any]) -> "Row":
    """Return query results as Row objects
    so model code can do row.name instead of row[2].
    """
    # Extract the names of the columns returned by the database query
    # cursor.description contains information about each returned column,
    # and col[0] gives the name of each column.
    fields = [col[0] for col in cursor.description]
    # Pair each column name with its corresponding value and create a Row object
    # Row() uses these pairs to create an object that allows easier
    # access to database values by their field names.
    return Row(zip(fields, row))

#Makes database results easier to access using field names.
class Row(dict):
    """A dict that also supports attribute access (row.name == row["name"]).

    Templates and model code can use dotted access (slot.status) or
    bracket access (slot["status"]) interchangeably.
    """

    # Allow database fields to be accessed using dot notation,
    # such as row.name, by treating the attribute name as a dictionary key.
    def __getattr__(self, item: str) -> Any:
        try:
            # Retrieve and return the value associated with the requested field.
            return self[item]
        except KeyError:
            # Raise AttributeError when the requested field does not exist.
            raise AttributeError(item)

    # Allow values to be assigned using dot notation, such as row.name = value,
    # while storing the value internally as a dictionary key-value pair.
    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


#Creates and configures the SQLite connection.
def get_connection() -> sqlite3.Connection:
    """Open a new connection to the SQLite database file.

    Every caller is expected to close the connection when done.
    The Database helper and get_cursor() context manager handle that.
    """
    # Get the configured database file path and convert it into
    # a Path object for easier file and directory management.
    db_path = Path(Config.DATABASE_PATH)
    # Make sure the directory containing the database exists.
    # Missing parent directories are created, while existing directories
    # are ignored without raising an error.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Open a connection to the SQLite database and enable type detection
    # so SQLite can correctly convert declared database types when reading data.
    conn = sqlite3.connect(str(db_path), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = _row_factory
    # Use the custom row factory so query results are returned as
    # Row objects that support convenient field-based access.

    conn.execute("PRAGMA foreign_keys = ON")
    # Enable foreign key enforcement to maintain valid relationships
    # between related database records.
    conn.execute("PRAGMA journal_mode = WAL")
    # Enable Write-Ahead Logging to improve SQLite concurrency
    # when the application performs database reads and writes.
    return conn
    # Return the fully configured database connection to the caller.

#Uses that connection and manages the entire database operation safely.
@contextmanager
def get_cursor(write: bool = False):
    """Context manager yielding a cursor; commits and closes automatically.

    Usage:
        with get_cursor(write=True) as cur:
            cur.execute("INSERT INTO slots (...) VALUES (...)", (...))
            new_id = cur.lastrowid

    write=True acquires the process-wide write lock for the duration of
    the block so two threads can never interleave writes.
    """
    # For write operations, acquire the shared lock so that only one
    # thread can perform a database write at a time.
    if write:
        with _write_lock:
            # Open a new, fully configured SQLite database connection.
            conn = get_connection()
            try:
                # Create a cursor that will be used to execute SQL commands.
                cur = conn.cursor()
                # Provide the cursor to the calling code and temporarily
                # pause here until the database operation is completed.

                yield cur
                # If the operation succeeds, permanently save the changes.
                conn.commit()
            except Exception:
                # If an error occurs, undo any uncommitted database changes
                # so that a failed transaction does not leave partial data.
                conn.rollback()
                # Re-raise the original error so the application can handle it.
                raise
            finally:
                # Always close the connection to release database resources,
                # whether the operation succeeds or fails.
                conn.close()
    else:
        # For read operations, no write lock is required, so simply
        # open a configured database connection.
        conn = get_connection()
        try:
            # Create a cursor for executing the database query.
            cur = conn.cursor()
            # Give the cursor to the calling code while keeping the
            # connection open for the duration of the operation.
            yield cur
        finally:
            # Always close the database connection after the read operation.
            conn.close()


class Database:
    """Convenience wrapper around get_cursor() for common DB operations.

    Model files use this instead of writing raw try/except/commit/close
    boilerplate. Methods: execute, executemany, fetch_one, fetch_all, count.
    """

    # These methods are static because they perform database operations
    # directly and do not need to store or access Database object state.
    @staticmethod
    def execute(sql: str, params: Sequence[Any] = ()) -> int:
        """Run an INSERT/UPDATE/DELETE. Returns the new row id (for INSERT)
        or the number of affected rows (for UPDATE/DELETE)."""
        # Open a write-enabled cursor so transaction handling and the
        # shared write lock are managed automatically by get_cursor().
        with get_cursor(write=True) as cur:
            # Execute the supplied SQL statement using the provided parameters.
            cur.execute(sql, params)
            # Return the inserted row ID when available; otherwise return
            # the number of rows affected by the operation.
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    @staticmethod
    def executemany(sql: str, seq_of_params: Iterable[Sequence[Any]]) -> int:
        # Open a write-enabled cursor to safely perform multiple database writes
        with get_cursor(write=True) as cur:
            # Execute the same SQL statement for each set of parameters.
            cur.executemany(sql, list(seq_of_params))
            # Return the total number of affected rows.
            return cur.rowcount

    @staticmethod
    def fetch_one(sql: str, params: Sequence[Any] = ()) -> Optional[Row]:
        # Open a read-only cursor because this operation does not modify data.
        with get_cursor(write=False) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    @staticmethod
    def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        with get_cursor(write=False) as cur:
            # Execute the database query using the supplied parameters.
            cur.execute(sql, params)
            # Retrieve the first matching row. The row factory configured
            # in get_connection() converts the result into a Row object.
            return cur.fetchall()

    @staticmethod
    def count(sql: str, params: Sequence[Any] = ()) -> int:
        # Reuse fetch_one() to execute the query and retrieve the row
        # containing the count value instead of opening another connection.
        row = Database.fetch_one(sql, params)
        # If the query returned no row, there is no count value,
        # so return zero to provide a consistent integer result.
        if row is None:
            return 0
        # Extract and return the first value from the returned row.
        # The first value is expected to contain the database count.
        return list(row.values())[0]


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL DEFAULT '',
    email             TEXT UNIQUE,
    password          TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'driver',
    location_id       TEXT,
    phone             TEXT,
    national_id       TEXT,
    business_permit   TEXT,
    joined_at         TEXT,
    is_active         INTEGER DEFAULT 1,
    reset_token       TEXT,
    reset_expires_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_users_email       ON users(email);
CREATE INDEX IF NOT EXISTS ix_users_role        ON users(role);
CREATE INDEX IF NOT EXISTS ix_users_location_id ON users(location_id);

CREATE TABLE IF NOT EXISTS locations (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    name                   TEXT NOT NULL,
    address                TEXT,
    county                 TEXT,
    type                   TEXT,
    total_slots            INTEGER DEFAULT 0,
    hourly_rate            REAL DEFAULT 30.0,
    is_active              INTEGER DEFAULT 1,
    admin_id               TEXT,
    phone                  TEXT,
    description            TEXT,
    camera_url             TEXT,
    reservations_paused    INTEGER DEFAULT 0,
    mpesa_consumer_key     TEXT,
    mpesa_consumer_secret  TEXT,
    mpesa_shortcode        TEXT,
    mpesa_passkey          TEXT,
    mpesa_account_type     TEXT DEFAULT 'paybill'
);

CREATE TABLE IF NOT EXISTS slots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id  TEXT NOT NULL,
    slot_number  TEXT NOT NULL,
    level        TEXT DEFAULT 'G',
    type         TEXT DEFAULT 'standard',
    status       TEXT DEFAULT 'available',
    sort_order   INTEGER DEFAULT 0,
    UNIQUE(location_id, slot_number)
);
CREATE INDEX IF NOT EXISTS ix_slots_location_id ON slots(location_id);
CREATE INDEX IF NOT EXISTS ix_slots_status      ON slots(status);

CREATE TABLE IF NOT EXISTS sessions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_number         TEXT NOT NULL,
    location_id          TEXT NOT NULL,
    slot_id              TEXT,
    guard_id             TEXT,
    driver_id            TEXT,
    reservation_id       TEXT,
    entry_time           TEXT,
    exit_time            TEXT,
    fee_kes              REAL,
    payment_method       TEXT,
    is_paid              INTEGER DEFAULT 0,
    status               TEXT DEFAULT 'active',
    receipt_number       TEXT UNIQUE,
    mpesa_code           TEXT,
    mpesa_checkout_request_id TEXT,
    payer_phone          TEXT,
    receipt_email_sent   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sessions_plate_number ON sessions(plate_number);
CREATE INDEX IF NOT EXISTS ix_sessions_location_id  ON sessions(location_id);
CREATE INDEX IF NOT EXISTS ix_sessions_driver_id    ON sessions(driver_id);
CREATE INDEX IF NOT EXISTS ix_sessions_entry_time   ON sessions(entry_time);
CREATE INDEX IF NOT EXISTS ix_sessions_status       ON sessions(status);
-- Note: no index on mpesa_checkout_request_id here. For a brand-new database
-- the column exists right away, but for an upgraded (pre-existing) database
-- this script's CREATE TABLE is skipped since sessions already exists, so
-- the column wouldn't exist yet at this point. _migrate_add_missing_columns()
-- below adds the column AND its index safely, after checking it's present.

-- One vehicle can't have two active sessions at once. This is what
-- actually backs the "already checked in" IntegrityError caught in
-- services/parking.py.
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_active_plate
    ON sessions(plate_number) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS reservations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id       TEXT,
    guard_id        TEXT,
    location_id     TEXT NOT NULL,
    slot_id         TEXT,
    event_label     TEXT,
    reserved_from   TEXT,
    reserved_until  TEXT,
    created_at      TEXT,
    reminder_sent   INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'pending',
    noshow_flagged  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_res_driver_id      ON reservations(driver_id);
CREATE INDEX IF NOT EXISTS ix_res_location_id    ON reservations(location_id);
CREATE INDEX IF NOT EXISTS ix_res_slot_id        ON reservations(slot_id);
CREATE INDEX IF NOT EXISTS ix_res_reserved_from  ON reservations(reserved_from);
CREATE INDEX IF NOT EXISTS ix_res_reserved_until ON reservations(reserved_until);
CREATE INDEX IF NOT EXISTS ix_res_status         ON reservations(status);

CREATE TABLE IF NOT EXISTS location_cameras (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id  TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT 'Camera',
    camera_url   TEXT NOT NULL,
    sort_order   INTEGER DEFAULT 0,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_location_cameras_location_id ON location_cameras(location_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT,
    role         TEXT,
    action       TEXT,
    details      TEXT,
    location_id  TEXT,
    timestamp    TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_user_id     ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS ix_audit_action      ON audit_logs(action);
CREATE INDEX IF NOT EXISTS ix_audit_location_id ON audit_logs(location_id);
CREATE INDEX IF NOT EXISTS ix_audit_timestamp   ON audit_logs(timestamp);

CREATE TABLE IF NOT EXISTS email_failures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    "to"       TEXT,
    subject    TEXT,
    error      TEXT,
    failed_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_email_failures_failed_at ON email_failures(failed_at);

CREATE TABLE IF NOT EXISTS system_state (
    key       TEXT PRIMARY KEY,
    last_run  TEXT
);

-- Server-side session store. See auth/session_manager.py.
CREATE TABLE IF NOT EXISTS http_sessions (
    session_id  TEXT PRIMARY KEY,
    data        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_http_sessions_expires_at ON http_sessions(expires_at);
"""


def init_db() -> None:
    """Create every table if it does not already exist.

    Called once at startup from main.py.
    """
    # Open a write-enabled cursor so the database structure can be
    # created or modified safely within a managed transaction.
    with get_cursor(write=True) as cur:
        # Execute the complete database schema to create the required
        # tables and database structures that do not already exist.
        cur.executescript(SCHEMA)
        # Check for database structure changes introduced in newer
        # versions and update older database files when necessary.
        _migrate_add_missing_columns(cur)


def _migrate_add_missing_columns(cur) -> None:
    """Add columns introduced after initial release to any pre-existing
    database file, so upgrading doesn't require deleting the database.
    """
    # Inspect the locations table to determine which columns currently exist.
    cur.execute("PRAGMA table_info(locations)")
    # Extract the names of all existing columns for easy membership checking.
    existing_location_cols = {row["name"] for row in cur.fetchall()}
    # Add the M-Pesa account type column when upgrading an older database
    # that was created before this field was introduced.
    if "mpesa_account_type" not in existing_location_cols:
        # PayBill vs Till changes which Daraja TransactionType an STK push
        # must use ("CustomerPayBillOnline" vs "CustomerBuyGoodsOnline").
        # Default to 'paybill' since Safaricom's shared sandbox shortcode
        # (174379) is a PayBill number.
        cur.execute(
            "ALTER TABLE locations ADD COLUMN mpesa_account_type TEXT DEFAULT 'paybill'"
        )

    # Inspect the sessions table before adding the M-Pesa checkout request ID
    cur.execute("PRAGMA table_info(sessions)")
    # Store the names of the existing session columns for the migration check.
    existing = {row["name"] for row in cur.fetchall()}
    # Add the checkout request ID to older databases that do not have it.
    if "mpesa_checkout_request_id" not in existing:
        cur.execute("ALTER TABLE sessions ADD COLUMN mpesa_checkout_request_id TEXT")
        # Index the checkout request ID so related session records can be
        # located efficiently when the application searches by this value.
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_sessions_mpesa_checkout_request_id "
            "ON sessions(mpesa_checkout_request_id)"
        )

    # Guards can be created without an email address (the admin shares their
    # password directly instead). Older databases have `email TEXT NOT NULL`,
    # which blocks that flow — rebuild the table without the constraint.
    cur.execute("PRAGMA table_info(users)")
    # Inspect the users table to determine whether email is still required.
    users_columns = cur.fetchall()
    # Locate the email column so its NOT NULL constraint can be checked.
    email_col = next((c for c in users_columns if c["name"] == "email"), None)
    # Rebuild the users table only when the email column exists and is
    # still marked as NOT NULL in an older database.
    if email_col is not None and email_col["notnull"]:
        # Create a replacement users table where email is optional while
        # preserving the other fields used by the application.
        cur.executescript(
            """
            CREATE TABLE users_new (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT NOT NULL DEFAULT '',
                email             TEXT UNIQUE,
                password          TEXT NOT NULL,
                role              TEXT NOT NULL DEFAULT 'driver',
                location_id       TEXT,
                phone             TEXT,
                national_id       TEXT,
                business_permit   TEXT,
                joined_at         TEXT,
                is_active         INTEGER DEFAULT 1,
                reset_token       TEXT,
                reset_expires_at  TEXT
            );
            INSERT INTO users_new SELECT
                id, name, email, password, role, location_id, phone,
                national_id, business_permit, joined_at, is_active,
                reset_token, reset_expires_at
            FROM users;
            DROP TABLE users;
            ALTER TABLE users_new RENAME TO users;
            CREATE INDEX IF NOT EXISTS ix_users_email       ON users(email);
            CREATE INDEX IF NOT EXISTS ix_users_role        ON users(role);
            CREATE INDEX IF NOT EXISTS ix_users_location_id ON users(location_id);
            """
        )


    # setup, copy it into location_cameras as "Camera 1" so it still shows
    # up after the upgrade.
    # Find locations that still contain the old camera configuration and
    # have not yet been migrated to the dedicated location_cameras table.
    locations_with_legacy_camera = cur.execute(
        """
        SELECT l.id, l.camera_url FROM locations l
        WHERE l.camera_url IS NOT NULL AND l.camera_url != ''
          AND NOT EXISTS (
              SELECT 1 FROM location_cameras lc WHERE lc.location_id = CAST(l.id AS TEXT)
          )
        """
    ).fetchall()
    # Process each location that still has a legacy camera configuration.
    for row in locations_with_legacy_camera:
        # Copy the old camera information into the new camera table so
        # existing camera configurations remain available after upgrading
        cur.execute(
            """
            INSERT INTO location_cameras (location_id, label, camera_url, sort_order, created_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (str(row["id"]), "Camera 1", row["camera_url"], datetime.now(timezone.utc).isoformat()),
        )
