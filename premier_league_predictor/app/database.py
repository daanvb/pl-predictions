import sqlite3
import os
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

DB = "/data/predictor.db"


def harden_path_permissions(path, mode=0o600):
    """Apply private Unix permissions where the host filesystem supports it."""
    try:
        os.chmod(path, mode)
        return True
    except (OSError, TypeError):
        return False


def get_db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    harden_path_permissions(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("PRAGMA trusted_schema = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
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
    _add_column_if_missing(
        conn, "players", "hide_news_ticker", "INTEGER NOT NULL DEFAULT 0"
    )
    _add_column_if_missing(
        conn, "players", "entry_fee_paid", "INTEGER NOT NULL DEFAULT 0"
    )
    _add_column_if_missing(
        conn, "players", "treasurer", "INTEGER NOT NULL DEFAULT 0"
    )
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
    _add_column_if_missing(conn, "fixtures", "match_phase", "TEXT")
    _add_column_if_missing(conn, "fixtures", "home_penalty_score", "INTEGER")
    _add_column_if_missing(conn, "fixtures", "away_penalty_score", "INTEGER")
    _add_column_if_missing(conn, "fixtures", "broadcaster", "TEXT")
    _add_column_if_missing(conn, "fixtures", "goals_json", "TEXT")
    _add_column_if_missing(conn, "fixtures", "incidents_json", "TEXT")
    _add_column_if_missing(conn, "fixtures", "live_data_source", "TEXT")
    _add_column_if_missing(conn, "fixtures", "home_logo", "TEXT")
    _add_column_if_missing(conn, "fixtures", "away_logo", "TEXT")
    # Keep competitions separate while preserving existing football-data.org IDs
    # and prediction foreign keys for the Premier League.
    _add_column_if_missing(
        conn, "fixtures", "competition",
        "TEXT NOT NULL DEFAULT 'premier_league'"
    )
    _add_column_if_missing(conn, "fixtures", "source_provider", "TEXT")
    _add_column_if_missing(conn, "fixtures", "source_fixture_id", "TEXT")
    conn.execute(
        "UPDATE fixtures SET competition = 'premier_league' "
        "WHERE competition IS NULL OR TRIM(competition) = ''"
    )
    conn.execute(
        "UPDATE fixtures SET source_provider = 'football-data.org', "
        "source_fixture_id = CAST(id AS TEXT) "
        "WHERE source_provider IS NULL"
    )

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fixtures_season_matchday_date
        ON fixtures(season, matchday, utc_date)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fixtures_season_status_date
        ON fixtures(season, status, utc_date)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fixtures_competition_season_date
        ON fixtures(competition, season, utc_date)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fixtures_provider_source
        ON fixtures(source_provider, source_fixture_id)
        WHERE source_provider IS NOT NULL AND source_fixture_id IS NOT NULL
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

    # Append-only, hash-chained record of prediction and DP changes. Scores
    # remain concealed in the player-facing ledger until the fixture locks.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prediction_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            fixture_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            kickoff_utc TEXT NOT NULL,
            matchday INTEGER,
            changed_at TEXT NOT NULL,
            action TEXT NOT NULL,
            revision INTEGER NOT NULL,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            dp INTEGER NOT NULL DEFAULT 0,
            commitment_salt TEXT NOT NULL,
            score_commitment TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL UNIQUE,
            UNIQUE(player_id, fixture_id, revision)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_prediction_audit_fixture_time
        ON prediction_audit_events(fixture_id, changed_at, id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_prediction_audit_player_time
        ON prediction_audit_events(player_id, changed_at, id)
    """)

    # Existing installations receive one baseline commitment per prediction.
    # This is additive and does not alter live prediction rows or points.
    existing_audit_count = conn.execute(
        "SELECT COUNT(*) FROM prediction_audit_events"
    ).fetchone()[0]
    if existing_audit_count == 0:
        existing_predictions = conn.execute(
            """SELECT player_id, fixture_id, home_score, away_score,
                      COALESCE(dp, 0) AS dp, updated_at
               FROM predictions ORDER BY id"""
        ).fetchall()
        for prediction in existing_predictions:
            append_prediction_audit_event(
                conn,
                player_id=prediction["player_id"],
                fixture_id=prediction["fixture_id"],
                home_score=prediction["home_score"],
                away_score=prediction["away_score"],
                dp=prediction["dp"],
                action="baseline",
                changed_at=(prediction["updated_at"] or datetime.now(timezone.utc).isoformat()),
            )
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS prediction_audit_no_update
        BEFORE UPDATE ON prediction_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'prediction audit events are immutable');
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS prediction_audit_no_delete
        BEFORE DELETE ON prediction_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'prediction audit events are immutable');
        END
    """)

    # Change-only snapshots power the live Gameweek position chart and remain
    # available when the completed Gameweek is viewed later.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_position_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season INTEGER NOT NULL,
            matchday INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            state_signature TEXT NOT NULL,
            UNIQUE(season, matchday, state_signature)
        )
    """)
    _add_column_if_missing(conn, "live_position_snapshots", "cause_fixture_id", "INTEGER")
    _add_column_if_missing(conn, "live_position_snapshots", "cause_label", "TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_position_snapshot_rows (
            snapshot_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            position INTEGER NOT NULL,
            season_points INTEGER NOT NULL,
            gameweek_points INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, player_id),
            FOREIGN KEY(snapshot_id)
                REFERENCES live_position_snapshots(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_live_position_snapshots_gameweek
        ON live_position_snapshots(season, matchday, captured_at)
    """)


    conn.execute("DROP TABLE IF EXISTS bigballs_shadow_samples")
    conn.execute("DROP TABLE IF EXISTS predictor_live_samples")
    conn.execute("DELETE FROM settings WHERE key LIKE 'bigballs_%'")

    # API-Football IDs cannot replace football-data.org fixture IDs: predictions
    # use the latter as foreign keys. Keep provider mappings and observations
    # separate while API-Football acts as a targeted live-data fallback.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_fixture_mappings (
            fixture_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            provider_fixture_id TEXT NOT NULL,
            match_method TEXT NOT NULL,
            mapped_at TEXT NOT NULL,
            PRIMARY KEY (fixture_id, provider),
            UNIQUE (provider, provider_fixture_id),
            FOREIGN KEY(fixture_id) REFERENCES fixtures(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_event_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            fixture_id INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            event_type TEXT,
            event_minute TEXT,
            first_seen_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(provider, fixture_id, event_key),
            FOREIGN KEY(fixture_id) REFERENCES fixtures(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_provider_event_fixture_time
        ON provider_event_observations(provider, fixture_id, first_seen_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_live_states (
            provider TEXT NOT NULL,
            fixture_id INTEGER NOT NULL,
            state_signature TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(provider, fixture_id),
            FOREIGN KEY(fixture_id) REFERENCES fixtures(id) ON DELETE CASCADE
        )
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS competition_winners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competition TEXT NOT NULL,
            season_label TEXT NOT NULL,
            winner_name TEXT NOT NULL,
            UNIQUE(competition, season_label)
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


def _audit_payload(player_id, fixture_id, changed_at, action, revision,
                   home_score, away_score, dp, commitment_salt,
                   score_commitment, previous_hash, player_name,
                   home_team, away_team, kickoff_utc, matchday):
    return {
        "action": str(action),
        "away_score": int(away_score),
        "changed_at": str(changed_at),
        "commitment_salt": str(commitment_salt),
        "dp": 1 if dp else 0,
        "fixture_id": int(fixture_id),
        "home_team": str(home_team),
        "home_score": int(home_score),
        "kickoff_utc": str(kickoff_utc),
        "matchday": int(matchday) if matchday is not None else None,
        "player_id": int(player_id),
        "player_name": str(player_name),
        "previous_hash": str(previous_hash),
        "revision": int(revision),
        "score_commitment": str(score_commitment),
        "away_team": str(away_team),
    }


def prediction_score_commitment(player_id, fixture_id, home_score,
                                away_score, dp, salt):
    payload = json.dumps({
        "away_score": int(away_score),
        "dp": 1 if dp else 0,
        "fixture_id": int(fixture_id),
        "home_score": int(home_score),
        "player_id": int(player_id),
        "salt": str(salt),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append_prediction_audit_event(conn, *, player_id, fixture_id,
                                  home_score, away_score, dp, action,
                                  changed_at=None):
    changed_at = changed_at or datetime.now(timezone.utc).isoformat()
    last_event = conn.execute(
        "SELECT event_hash FROM prediction_audit_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    previous_hash = last_event["event_hash"] if last_event else "0" * 64
    revision = conn.execute(
        """SELECT COALESCE(MAX(revision), 0) + 1
           FROM prediction_audit_events
           WHERE player_id = ? AND fixture_id = ?""",
        (player_id, fixture_id),
    ).fetchone()[0]
    salt = secrets.token_hex(16)
    player = conn.execute(
        "SELECT name FROM players WHERE id = ?", (player_id,)
    ).fetchone()
    fixture = conn.execute(
        """SELECT home_team, away_team, utc_date, matchday
           FROM fixtures WHERE id = ?""",
        (fixture_id,),
    ).fetchone()
    if not player or not fixture:
        raise ValueError("Prediction audit requires an existing player and fixture")
    commitment = prediction_score_commitment(
        player_id, fixture_id, home_score, away_score, dp, salt
    )
    payload = _audit_payload(
        player_id, fixture_id, changed_at, action, revision,
        home_score, away_score, dp, salt, commitment, previous_hash,
        player["name"], fixture["home_team"], fixture["away_team"],
        fixture["utc_date"], fixture["matchday"],
    )
    event_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    conn.execute(
        """INSERT INTO prediction_audit_events(
               player_id, fixture_id, player_name, home_team, away_team,
               kickoff_utc, matchday, changed_at, action, revision,
               home_score, away_score, dp, commitment_salt,
               score_commitment, previous_hash, event_hash
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            player_id, fixture_id, player["name"], fixture["home_team"],
            fixture["away_team"], fixture["utc_date"], fixture["matchday"],
            changed_at, action, revision,
            home_score, away_score, 1 if dp else 0, salt,
            commitment, previous_hash, event_hash,
        ),
    )
    return event_hash


def verify_prediction_audit_chain(conn):
    previous_hash = "0" * 64
    latest = {}
    rows = conn.execute(
        "SELECT * FROM prediction_audit_events ORDER BY id"
    ).fetchall()
    for row in rows:
        commitment = prediction_score_commitment(
            row["player_id"], row["fixture_id"], row["home_score"],
            row["away_score"], row["dp"], row["commitment_salt"],
        )
        payload = _audit_payload(
            row["player_id"], row["fixture_id"], row["changed_at"],
            row["action"], row["revision"], row["home_score"],
            row["away_score"], row["dp"], row["commitment_salt"],
            row["score_commitment"], row["previous_hash"],
            row["player_name"], row["home_team"], row["away_team"],
            row["kickoff_utc"], row["matchday"],
        )
        calculated = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if (
            row["previous_hash"] != previous_hash
            or row["score_commitment"] != commitment
            or not hmac.compare_digest(row["event_hash"], calculated)
        ):
            return {"valid": False, "event_count": len(rows), "error_id": row["id"]}
        previous_hash = row["event_hash"]
        latest[(row["player_id"], row["fixture_id"])] = row

    predictions = conn.execute(
        """SELECT player_id, fixture_id, home_score, away_score,
                  COALESCE(dp, 0) AS dp FROM predictions"""
    ).fetchall()
    for prediction in predictions:
        event = latest.get((prediction["player_id"], prediction["fixture_id"]))
        if not event or any(
            int(event[field]) != int(prediction[field])
            for field in ("home_score", "away_score", "dp")
        ):
            return {"valid": False, "event_count": len(rows), "error_id": None}

    return {"valid": True, "event_count": len(rows), "error_id": None}


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
