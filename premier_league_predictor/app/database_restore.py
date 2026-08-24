import os
import shutil
import sqlite3
import tempfile


REQUIRED_COLUMNS = {
    "players": {"id", "name", "pin_hash", "admin"},
    "settings": {"key", "value"},
    "fixtures": {
        "id", "season", "matchday", "utc_date", "status",
        "home_team", "away_team", "home_score", "away_score",
    },
    "predictions": {
        "id", "player_id", "fixture_id", "home_score", "away_score",
        "points",
    },
}


def database_has_users(path):
    if not os.path.exists(path):
        return False

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='players'"
        ).fetchone()
        if not row:
            return False
        return conn.execute("SELECT 1 FROM players LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def validate_predictor_database(path, require_users=False):
    uri = "file:" + os.path.abspath(path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError("Backup failed its SQLite integrity check.")

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing_tables = set(REQUIRED_COLUMNS) - tables
        if missing_tables:
            raise ValueError(
                "Backup is not a compatible Predictor database. Missing tables: "
                + ", ".join(sorted(missing_tables))
            )

        for table, required in REQUIRED_COLUMNS.items():
            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            missing = required - columns
            if missing:
                raise ValueError(
                    f"Backup is not a compatible Predictor database. "
                    f"Table {table} is missing columns: "
                    + ", ".join(sorted(missing))
                )

        user_count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        admin_count = conn.execute(
            "SELECT COUNT(*) FROM players WHERE admin = 1"
        ).fetchone()[0]
        if require_users and user_count == 0:
            raise ValueError("Backup does not contain any players.")
        if require_users and admin_count == 0:
            raise ValueError("Backup does not contain an administrator.")

        return {"users": user_count, "admins": admin_count}
    finally:
        conn.close()


def install_database(upload_path, database_path):
    validate_predictor_database(upload_path, require_users=True)
    database_dir = os.path.dirname(database_path) or "."
    os.makedirs(database_dir, exist_ok=True)
    fd, replacement = tempfile.mkstemp(
        prefix="predictor-restore-", suffix=".db", dir=database_dir
    )
    os.close(fd)
    try:
        shutil.copyfile(upload_path, replacement)
        validate_predictor_database(replacement, require_users=True)
        with open(replacement, "rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(replacement, database_path)
    finally:
        if os.path.exists(replacement):
            os.remove(replacement)
