import sqlite3
import os
import hashlib
import hmac
import re

from werkzeug.security import check_password_hash, generate_password_hash

DB = "/data/predictor.db"


def get_db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def hash_pin(pin):
    return generate_password_hash(pin, method="scrypt")


def is_legacy_pin_hash(value):
    return bool(re.fullmatch(r"[0-9a-f]{64}", value or ""))


def verify_pin(pin, stored_hash):
    """Accept old SHA-256 PINs while accounts migrate to salted scrypt."""
    if is_legacy_pin_hash(stored_hash):
        legacy_hash = hashlib.sha256(pin.encode()).hexdigest()
        return hmac.compare_digest(legacy_hash, stored_hash)

    try:
        return check_password_hash(stored_hash, pin)
    except (ValueError, TypeError):
        return False


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
    conn.execute("PRAGMA journal_mode = WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            pin_hash TEXT NOT NULL,
            admin INTEGER DEFAULT 0
        )
    """)
    _add_column_if_missing(conn, "players", "login_name", "TEXT")
    _add_column_if_missing(conn, "players", "email", "TEXT")
    conn.execute(
        "UPDATE players SET login_name = name WHERE login_name IS NULL OR TRIM(login_name) = ''"
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_players_email_nocase
           ON players(email COLLATE NOCASE) WHERE email IS NOT NULL"""
    )

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
    _add_column_if_missing(conn, "fixtures", "incidents_json", "TEXT")
    _add_column_if_missing(conn, "fixtures", "live_data_source", "TEXT")
    _add_column_if_missing(conn, "fixtures", "home_logo", "TEXT")
    _add_column_if_missing(conn, "fixtures", "away_logo", "TEXT")

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fixtures_season_matchday_date
        ON fixtures(season, matchday, utc_date)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fixtures_season_status_date
        ON fixtures(season, status, utc_date)
    """)

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
    _add_column_if_missing(conn, "historical_fixtures", "competition", "TEXT")

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

    # Permanent end-of-season snapshots. Player names are copied rather than
    # linked so later account edits cannot rewrite historical tables.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS season_archives (
            season INTEGER PRIMARY KEY,
            label TEXT NOT NULL,
            winner_name TEXT NOT NULL,
            archived_at TEXT,
            stats_available INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS season_archive_players (
            season INTEGER NOT NULL,
            position INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            points INTEGER DEFAULT 0,
            exact_draws INTEGER DEFAULT 0,
            exact_scores INTEGER DEFAULT 0,
            correct_results INTEGER DEFAULT 0,
            dp_exact_scores INTEGER DEFAULT 0,
            PRIMARY KEY (season, position),
            FOREIGN KEY(season) REFERENCES season_archives(season)
        )
    """)

    known_champions = (
        (2018, "2018/19", "Strat"),
        (2019, "2019/20", "Strat"),
        (2020, "2020/21", "Strat"),
        (2021, "2021/22", "TROPiC"),
        (2022, "2022/23", "TROPiC"),
        (2023, "2023/24", "Percei"),
        (2024, "2024/25", "Fontz"),
        (2025, "2025/26", "TROPiC"),
    )
    conn.executemany(
        """INSERT OR IGNORE INTO season_archives
           (season, label, winner_name, stats_available)
           VALUES (?, ?, ?, 0)""",
        known_champions,
    )

    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) FROM players"
    ).fetchone()[0]

    if seed_default_player and count == 0:
        conn.execute(
            """
            INSERT INTO players(name, login_name, email, pin_hash, admin)
            VALUES (?, ?, NULL, ?, ?)
            """,
            ("Dan", "Dan", hash_pin("1234"), 1)
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
