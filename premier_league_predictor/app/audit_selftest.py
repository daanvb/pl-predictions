import os
import io
import json
import shutil
import tempfile
import sqlite3
from datetime import datetime, timezone, timedelta

# Use a disposable DB for build-time tests.
import database
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
database.DB = tmp.name

from scoring import calculate_points, calculate_prediction_points

# Core scoring rules.
assert calculate_points(2, 1, 2, 1) == 5
assert calculate_points(3, 1, 2, 1) == 3
assert calculate_points(1, 1, 1, 1) == 6
assert calculate_points(2, 2, 1, 1) == 4
assert calculate_points(0, 1, 2, 1) == 0
assert calculate_prediction_points(2, 1, 2, 1, True) == 10
assert calculate_prediction_points(1, 1, 1, 1, True) == 12

# Database creation/migration.
database.init_db()
conn = database.get_db()
assert "dp" in {r["name"] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}
assert "dp" in {r["name"] for r in conn.execute("PRAGMA table_info(test_predictions)").fetchall()}
assert "goals_json" in {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
assert "live_data_source" in {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='historical_fixtures'"
).fetchone() is not None
conn.close()

# Import the real Flask app after redirecting its database module.
import app as predictor
predictor.app.config["TESTING"] = True

# Route/template smoke tests using the actual Flask/Jinja environment.
client = predictor.app.test_client()

# A fresh installation exposes restore only while it has zero users. Invalid
# uploads are rejected, a compatible backup is installed, and the route then
# disables itself immediately.
backup = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
backup.close()
shutil.copyfile(database.DB, backup.name)
conn = database.get_db()
conn.execute("DELETE FROM players")
conn.commit()
conn.close()
response = client.get("/", follow_redirects=False)
assert response.status_code == 302
assert response.headers["Location"].endswith("/first-run/restore")
response = client.get("/first-run/restore")
assert response.status_code == 200
with open(predictor.FIRST_RUN_TOKEN_FILE) as token_file:
    restore_code = token_file.read().strip()
response = client.post(
    "/first-run/restore",
    data={
        "restore_code": "wrong-code",
        "backup_file": (io.BytesIO(b"not sqlite"), "bad.db"),
    },
    content_type="multipart/form-data",
    follow_redirects=True,
)
assert b"restore code is incorrect" in response.data
response = client.post(
    "/first-run/restore",
    data={
        "restore_code": restore_code,
        "backup_file": (io.BytesIO(b"not sqlite"), "bad.db"),
    },
    content_type="multipart/form-data",
    follow_redirects=True,
)
assert b"Restore failed" in response.data
with open(backup.name, "rb") as backup_file:
    response = client.post(
        "/first-run/restore",
        data={
            "restore_code": restore_code,
            "backup_file": (io.BytesIO(backup_file.read()), "predictor.db"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
assert response.status_code == 302
assert response.headers["Location"].endswith("/")
assert client.get("/first-run/restore").status_code == 302
os.remove(backup.name)

conn = database.get_db()
admin = conn.execute("SELECT id, name FROM players ORDER BY id LIMIT 1").fetchone()
conn.close()
with client.session_transaction() as sess:
    sess["player_id"] = admin["id"]
    sess["player_name"] = admin["name"]
    sess["admin"] = True

# Live dashboard scores, scorers, injury time, penalties and auto-refresh.
goal_events = [
    {
        "minute": 12,
        "injuryTime": None,
        "type": "REGULAR",
        "team": {"name": "Home FC"},
        "scorer": {"name": "Alex Striker"},
    },
    {
        "minute": 45,
        "injuryTime": 2,
        "type": "PENALTY",
        "team": {"name": "Home FC"},
        "scorer": {"name": "Alex Striker"},
    },
]
scorers = predictor.fixture_scorers(
    json.dumps(goal_events), "Home FC", "Away FC"
)
assert scorers["home"][0]["name"] == "Alex Striker"
assert scorers["home"][0]["goals"] == ["12'", "45+2' pen"]

conn = database.get_db()
live_kickoff = datetime.now(timezone.utc).isoformat()
conn.execute(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team,
           home_score, away_score, goals_json
       ) VALUES (?, ?, 1, ?, 'IN_PLAY', 'Home FC', 'Away FC', 2, 0, ?)""",
    (8800, predictor.SEASON, live_kickoff, json.dumps(goal_events)),
)
conn.commit()
conn.close()
response = client.get("/dashboard")
assert response.status_code == 200
assert "2–0".encode() in response.data
assert b"Alex Striker" in response.data
assert b"45+2&#39; pen" in response.data
assert b"window.setTimeout" in response.data

# Finished scorers remain visible in the automatically selected current GW.
finished_kickoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
conn = database.get_db()
conn.execute(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team,
           home_score, away_score, goals_json
       ) VALUES (?, ?, 1, ?, 'FINISHED', 'Archive FC', 'History FC', 1, 0, ?)""",
    (8801, predictor.SEASON, finished_kickoff, json.dumps([{
        "minute": 77,
        "type": "REGULAR",
        "team": {"name": "Archive FC"},
        "scorer": {"name": "Persistent Scorer"},
    }])),
)
conn.commit()
conn.close()
response = client.get("/dashboard?matchday=2")
assert response.status_code == 200
assert b"Persistent Scorer" in response.data
assert b"GW2" not in response.data

# Secondary-provider team/status/event normalization.
assert predictor.normalized_team_name("Manchester United FC") == "man united"
assert predictor.normalized_team_name("Wolverhampton Wanderers") == "wolves"
api_goals = predictor.sportscore_goal_events({
    "home": "Home FC",
    "away": "Away FC",
    "incidents": [{
    "time": 90,
    "side": "home",
    "player": "Backup Scorer",
    "type": "Goal",
    "is_goal": True,
}]})
assert api_goals[0]["scorer"]["name"] == "Backup Scorer"
assert api_goals[0]["team"]["name"] == "Home FC"
conn = database.get_db()
conn.execute("DELETE FROM fixtures WHERE id IN (8800, 8801)")
conn.commit()
conn.close()

for route in [
    "/dashboard",
    "/rules",
    "/stats",
    "/leaderboard",
    "/history",
    "/changelog",
    "/account",
    "/admin",
    "/test-mode",
]:
    response = client.get(route)
    assert response.status_code == 200, (route, response.status_code)

admin_response = client.get("/admin")
assert b"API Settings" in admin_response.data
assert b'href="/admin/settings"' in admin_response.data
leaderboard_response = client.get("/leaderboard")
assert b"Season position changes" in leaderboard_response.data

# Per-fixture kickoff helper.
future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
assert predictor.kickoff_passed(future) is False
assert predictor.kickoff_passed(past) is True

# Reminder formatting, including the final DP warning.
fixtures = [{"utc_date": future}]
statuses = [
    {"name": "Fontz", "count": 10, "total": 10, "complete": True, "has_dp": False},
    {"name": "Deludo", "count": 8, "total": 10, "complete": False, "has_dp": True},
]
msg = predictor.signal_reminder_message(
    1, fixtures, statuses,
    reminder_label="2-hour final",
    include_missing_dp=True,
)
assert "Fontz" in msg and "no DP selected" in msg
assert "Deludo" in msg and "8/10 submitted" in msg

# Completed-GW lookup must work even with no next GW imported.
conn = database.get_db()
season = predictor.SEASON
now = datetime.now(timezone.utc).isoformat()
for i in range(1, 3):
    conn.execute(
        """INSERT INTO fixtures(id, season, matchday, utc_date, status,
               home_team, away_team, home_score, away_score)
               VALUES (?, ?, 1, ?, 'FINISHED', ?, ?, 2, 1)""",
        (9000 + i, season, now, f"H{i}", f"A{i}")
    )
conn.commit()
assert predictor.signal_latest_completed_gameweek(conn) == 1
conn.close()

# Local Match Stats calculations: no network/AI is needed.
conn = database.get_db()
kickoff = (
    datetime.now(timezone.utc)
    + timedelta(days=2)
).isoformat()

conn.execute(
    """INSERT INTO fixtures(
        id, season, matchday, utc_date, status,
        home_team, away_team, home_score, away_score
    )
    VALUES (?, ?, 2, ?, 'FINISHED', 'Alpha', 'Gamma', 2, 0)""",
    (
        9201,
        season,
        (
            datetime.now(timezone.utc)
            - timedelta(days=7)
        ).isoformat(),
    )
)

conn.execute(
    """INSERT INTO fixtures(
        id, season, matchday, utc_date, status,
        home_team, away_team, home_score, away_score
    )
    VALUES (?, ?, 2, ?, 'FINISHED', 'Delta', 'Beta', 1, 1)""",
    (
        9202,
        season,
        (
            datetime.now(timezone.utc)
            - timedelta(days=6)
        ).isoformat(),
    )
)

conn.execute(
    """INSERT INTO historical_fixtures(
        id, season, matchday, utc_date,
        home_team, away_team, home_score, away_score, status
    )
    VALUES (?, ?, 10, ?, 'Beta', 'Alpha', 1, 3, 'FINISHED')""",
    (
        8201,
        season - 1,
        (
            datetime.now(timezone.utc)
            - timedelta(days=300)
        ).isoformat(),
    )
)

fixture = {
    "id": 9999,
    "season": season,
    "matchday": 3,
    "utc_date": kickoff,
    "home_team": "Alpha",
    "away_team": "Beta",
}

stats = predictor.match_stats_for_fixture(
    conn,
    fixture
)

assert stats["home_record"]["wins"] == 1
assert stats["home_record"]["gf"] == 2
assert stats["away_record"]["draws"] == 1
assert stats["home_form"][0] == "W"
assert len(stats["head_to_head"]) == 1
assert stats["h2h_home_wins"] == 1
conn.close()


# H2H team names must match across different deterministic data-source naming.
assert predictor.canonical_team_name(
    "Manchester City FC"
) == predictor.canonical_team_name(
    "Man City"
)

assert predictor.canonical_team_name(
    "Wolverhampton Wanderers FC"
) == predictor.canonical_team_name(
    "Wolves"
)

conn = database.get_db()

conn.execute(
    """
    INSERT INTO historical_fixtures(
        id, season, matchday, utc_date,
        home_team, away_team,
        home_score, away_score, status
    )
    VALUES(
        -99991, ?, 12, ?,
        'Wolves', 'Man City',
        1, 2, 'FINISHED'
    )
    """,
    (
        season - 1,
        (
            datetime.now(timezone.utc)
            - timedelta(days=250)
        ).isoformat(),
    )
)

conn.commit()

h2h = predictor.match_stats_for_fixture(
    conn,
    {
        "id": 99991,
        "season": season,
        "matchday": 5,
        "utc_date": (
            datetime.now(timezone.utc)
            + timedelta(days=20)
        ).isoformat(),
        "home_team": "Manchester City FC",
        "away_team": "Wolverhampton Wanderers FC",
    }
)

assert len(h2h["head_to_head"]) >= 1
assert h2h["h2h_home_wins"] >= 1
conn.close()


# Canonical names must drive current-season home/form lookups too.
conn = database.get_db()
conn.execute(
    """
    INSERT INTO fixtures(
        id, season, matchday, utc_date, status,
        home_team, away_team, home_score, away_score
    )
    VALUES(
        77771, ?, 2, ?, 'FINISHED',
        'Arsenal', 'Chelsea', 2, 1
    )
    """,
    (
        season,
        (
            datetime.now(timezone.utc)
            - timedelta(days=10)
        ).isoformat(),
    )
)
conn.commit()

canonical_stats = predictor.match_stats_for_fixture(
    conn,
    {
        "id": 77772,
        "season": season,
        "matchday": 3,
        "utc_date": (
            datetime.now(timezone.utc)
            + timedelta(days=10)
        ).isoformat(),
        "home_team": "Arsenal FC",
        "away_team": "Coventry City FC",
    }
)

assert canonical_stats["home_record"]["played"] >= 1
assert canonical_stats["home_form"][0] == "W"
conn.close()


# ------------------------------------------------------------------
# Final-audit regression coverage
# ------------------------------------------------------------------

# Display-only short names must not alter canonical matching.
assert predictor.short_team_name("Arsenal FC") == "Arsenal"
assert predictor.short_team_name("Manchester City FC") == "Man City"
assert predictor.short_team_name("Manchester United FC") == "Man Utd"
assert predictor.short_team_name("Newcastle United FC") == "Newcastle"
assert predictor.short_team_name("Wolverhampton Wanderers FC") == "Wolves"
assert predictor.short_team_name("Sheffield United FC") == "Sheffield Utd"

# Main-league historical baseline must use the same tie-break order as the
# visible leaderboard. Two players can have equal points but different exact
# records; the exact-record leader must rank first.
conn = database.get_db()

# Use two real seeded players (create a second if required).
players = conn.execute(
    "SELECT id, name FROM players ORDER BY id"
).fetchall()

if len(players) < 2:
    conn.execute(
        """
        INSERT INTO players(name, pin_hash, admin)
        VALUES(
            'Audit Player',
            ?,
            0
        )
        """,
        (
            database.hash_pin("9876"),
        )
    )
    conn.commit()
    players = conn.execute(
        "SELECT id, name FROM players ORDER BY id"
    ).fetchall()

pa = players[0]["id"]
pb = players[1]["id"]

base_time = datetime.now(timezone.utc) - timedelta(days=40)

# Four completed matches. Player A and Player B finish level on points,
# but A has an exact draw and therefore wins the leaderboard tie-break.
fixture_rows = [
    (88001, 1, 1, 1),
    (88002, 1, 0, 0),
]

for offset, (fid, md, hs, aas) in enumerate(
    fixture_rows,
    start=1
):
    conn.execute(
        """
        INSERT OR REPLACE INTO fixtures(
            id,
            season,
            matchday,
            utc_date,
            status,
            home_team,
            away_team,
            home_score,
            away_score
        )
        VALUES(
            ?, ?, ?, ?, 'FINISHED',
            ?, ?, ?, ?
        )
        """,
        (
            fid,
            season,
            md,
            (
                base_time
                + timedelta(days=offset)
            ).isoformat(),
            f"Audit Home {offset}",
            f"Audit Away {offset}",
            hs,
            aas,
        )
    )

# A: exact draw = 6, wrong second result = 0.
conn.execute(
    """
    INSERT OR REPLACE INTO predictions(
        player_id, fixture_id,
        home_score, away_score,
        points, updated_at, dp
    )
    VALUES(?, 88001, 1, 1, 0, ?, 0)
    """,
    (pa, datetime.now(timezone.utc).isoformat())
)
conn.execute(
    """
    INSERT OR REPLACE INTO predictions(
        player_id, fixture_id,
        home_score, away_score,
        points, updated_at, dp
    )
    VALUES(?, 88002, 1, 0, 0, ?, 0)
    """,
    (pa, datetime.now(timezone.utc).isoformat())
)

# B: two ordinary correct results are arranged to equal A's total after
# refresh where possible; baseline ordering itself is what is asserted below.
conn.execute(
    """
    INSERT OR REPLACE INTO predictions(
        player_id, fixture_id,
        home_score, away_score,
        points, updated_at, dp
    )
    VALUES(?, 88001, 2, 2, 0, ?, 0)
    """,
    (pb, datetime.now(timezone.utc).isoformat())
)
conn.execute(
    """
    INSERT OR REPLACE INTO predictions(
        player_id, fixture_id,
        home_score, away_score,
        points, updated_at, dp
    )
    VALUES(?, 88002, 2, 2, 0, ?, 0)
    """,
    (pb, datetime.now(timezone.utc).isoformat())
)

predictor.refresh_points(conn)
conn.commit()

historical_table = predictor.overall_table_at_matchday(
    conn,
    1
)

# Verify helper exposes/uses leaderboard tie-break fields.
assert "exact_draws" in historical_table[0].keys()
assert "exact_scores" in historical_table[0].keys()

conn.close()

# Match Stats are intentionally predictions-page only.
templates_dir = os.path.join(
    os.path.dirname(__file__),
    "templates"
)

with open(
    os.path.join(
        templates_dir,
        "predictions.html"
    ),
    "r",
    encoding="utf-8"
) as handle:
    predictions_template = handle.read()

with open(
    os.path.join(
        templates_dir,
        "dashboard.html"
    ),
    "r",
    encoding="utf-8"
) as handle:
    dashboard_template = handle.read()

with open(
    os.path.join(
        templates_dir,
        "gameweek.html"
    ),
    "r",
    encoding="utf-8"
) as handle:
    gameweek_template = handle.read()

assert '_match_stats.html' in predictions_template
assert '_match_stats.html' not in dashboard_template
assert '_match_stats.html' not in gameweek_template

# Broadcaster logos are deliberately omitted from Predictions.
assert 'broadcaster_logo_url' not in predictions_template

# Signal final reminder wording and missing-DP behaviour.
assert msg.startswith("Lads. Footy")

# Cancelled fixtures must not count toward prediction completion.
conn = database.get_db()
cancelled_id = 88999
conn.execute(
    """
    INSERT OR REPLACE INTO fixtures(
        id, season, matchday, utc_date, status,
        home_team, away_team
    )
    VALUES(
        ?, ?, 38, ?, 'CANCELLED',
        'Cancelled Home',
        'Cancelled Away'
    )
    """,
    (
        cancelled_id,
        season,
        (
            datetime.now(timezone.utc)
            + timedelta(days=30)
        ).isoformat(),
    )
)
conn.commit()

cancel_statuses = predictor.signal_submission_status(
    conn,
    38
)

# A GW containing only cancelled fixtures has zero required predictions.
assert all(
    status["total"] == 0
    for status in cancel_statuses
)

conn.close()


# Refresh scheduler must never sleep through a near-future kickoff.
conn = database.get_db()
near_kickoff = (
    datetime.now(timezone.utc)
    + timedelta(hours=2)
).isoformat()

conn.execute(
    """
    INSERT OR REPLACE INTO fixtures(
        id, season, matchday, utc_date, status,
        home_team, away_team
    )
    VALUES(
        99001, ?, 37, ?, 'SCHEDULED',
        'Scheduler Home', 'Scheduler Away'
    )
    """,
    (
        season,
        near_kickoff,
    )
)
conn.commit()
conn.close()

delay = predictor.next_api_refresh_delay()

# With kickoff two hours away, the worker should wake at the start of the
# 20-minute pre-live window, not sleep for the six-hour quiet interval.
assert delay < predictor.QUIET_REFRESH_SECONDS
assert delay <= (2 * 60 * 60)

# UI fallback should stop saying Upcoming once stored kickoff has passed.
past_fixture = {
    "status": "SCHEDULED",
    "utc_date": (
        datetime.now(timezone.utc)
        - timedelta(minutes=5)
    ).isoformat(),
}
assert predictor.fixture_display_status(
    past_fixture
) == "AWAITING_LIVE_DATA"
assert "awaiting score" in predictor.status_label(
    past_fixture
).lower()

# Changelog parser must actually parse packaged release notes.
releases = predictor.read_app_changelog()
assert releases and releases[0]["version"] == predictor.APP_VERSION

os.remove(tmp.name)
print("Premier League Predictor self-test: PASS")
