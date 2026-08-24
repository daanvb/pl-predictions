import sqlite3
import os
import hashlib

DB = "/data/predictor.db"


def get_db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()


def _add_column_if_missing(conn, table, column, definition):
    cols = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }

    if column not in cols:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db(seed_default_player=True):
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            pin_hash TEXT NOT NULL,
            admin INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fixtures (
            id INTEGER PRIMARY KEY,
            season INTEGER NOT NULL,
            matchday INTEGER,
            utc_date TEXT NOT NULL,
            status TEXT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_score INTEGER,
            away_score INTEGER,
            last_updated TEXT
        )
    """)

    _add_column_if_missing(conn, "fixtures", "minute", "INTEGER")
    _add_column_if_missing(conn, "fixtures", "injury_time", "INTEGER")
    _add_column_if_missing(conn, "fixtures", "broadcaster", "TEXT")
    _add_column_if_missing(conn, "fixtures", "goals_json", "TEXT")
    _add_column_if_missing(conn, "fixtures", "live_data_source", "TEXT")

    # Historical results are stored separately so they can power local
    # form/head-to-head statistics without ever interfering with the live
    # Predictor fixture list or prediction foreign keys.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_fixtures (
            id INTEGER PRIMARY KEY,
            season INTEGER NOT NULL,
            matchday INTEGER,
            utc_date TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            status TEXT DEFAULT 'FINISHED'
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_historical_fixture_teams
        ON historical_fixtures(home_team, away_team)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_historical_fixture_date
        ON historical_fixtures(utc_date)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            fixture_id INTEGER NOT NULL,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            points INTEGER DEFAULT 0,
            updated_at TEXT,
            UNIQUE(player_id, fixture_id),
            FOREIGN KEY(player_id) REFERENCES players(id),
            FOREIGN KEY(fixture_id) REFERENCES fixtures(id)
        )
    """)

    _add_column_if_missing(
        conn,
        "predictions",
        "dp",
        "INTEGER DEFAULT 0"
    )

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_predictions_player
        ON predictions(player_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_predictions_fixture
        ON predictions(fixture_id)
    """)

    # Isolated test-mode data. These tables are deliberately separate from
    # real fixtures/predictions so testing can never affect the live league.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS test_fixtures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_score INTEGER,
            away_score INTEGER,
            status TEXT DEFAULT 'SCHEDULED'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS test_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tester TEXT NOT NULL,
            fixture_id INTEGER NOT NULL,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            points INTEGER DEFAULT 0,
            UNIQUE(tester, fixture_id),
            FOREIGN KEY(fixture_id) REFERENCES test_fixtures(id)
        )
    """)

    _add_column_if_missing(
        conn,
        "test_predictions",
        "dp",
        "INTEGER DEFAULT 0"
    )

    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) FROM players"
    ).fetchone()[0]

    if seed_default_player and count == 0:
        conn.execute(
            """
            INSERT INTO players(name, pin_hash, admin)
            VALUES (?, ?, ?)
            """,
            ("Dan", hash_pin("1234"), 1)
        )
        conn.commit()

    conn.close()


def get_setting(key):
    conn = get_db()

    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,)
    ).fetchone()

    conn.close()

    return row["value"] if row else None


def set_setting(key, value):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (key, value)
    )

    conn.commit()
    conn.close()
