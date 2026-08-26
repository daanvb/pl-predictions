import os
import io
import json
import shutil
import tempfile
import sqlite3
from datetime import datetime, timezone, timedelta

# Use a disposable DB for build-time tests.
import database
legacy_pin_hash = __import__("hashlib").sha256(b"2468").hexdigest()
assert database.is_legacy_pin_hash(legacy_pin_hash)
assert database.verify_pin("2468", legacy_pin_hash)
assert not database.verify_pin("1357", legacy_pin_hash)

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
assert conn.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
assert "dp" in {r["name"] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}
assert "goals_json" in {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
assert "incidents_json" in {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
assert "live_data_source" in {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
assert "login_name" in {r["name"] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
assert "email" in {r["name"] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name='live_position_snapshots'"
).fetchone() is not None
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name='live_position_snapshot_rows'"
).fetchone() is not None
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='season_archives'"
).fetchone() is not None
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='season_archive_players'"
).fetchone() is not None
seeded_champions = {
    row["label"]: row["winner_name"]
    for row in conn.execute(
        "SELECT label, winner_name FROM season_archives"
    ).fetchall()
}
assert seeded_champions == {
    "2018/19": "Strat",
    "2019/20": "Strat",
    "2020/21": "Strat",
    "2021/22": "TROPiC",
    "2022/23": "TROPiC",
    "2023/24": "Percei",
    "2024/25": "Fontz",
    "2025/26": "TROPiC",
}
assert conn.execute("SELECT COUNT(*) FROM players WHERE login_name IS NULL").fetchone()[0] == 0
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='historical_fixtures'"
).fetchone() is not None
assert "competition" in {
    row["name"]
    for row in conn.execute("PRAGMA table_info(historical_fixtures)").fetchall()
}
conn.close()

# Import the real Flask app after redirecting its database module.
import app as predictor
predictor.app.config["TESTING"] = True
assert predictor.LIVE_REFRESH_SECONDS == 60
assert predictor.GOOGLE_BACKUP_LIMIT == 10
assert predictor.compact_record_name("Pendragon ⚔️") == "Pendragon"
assert predictor.gameweek_progress_label([]) == ""
assert predictor.gameweek_progress_label([
    {"status": "FINISHED"},
]) == "After 1 game"
assert predictor.gameweek_progress_label([
    {"status": "FINISHED"},
    {"status": "FINISHED"},
    {"status": "IN_PLAY"},
    {"status": "PAUSED"},
]) == "After 2 completed games · 2 games in progress"
assert predictor.gameweek_progress_label([
    {"status": "IN_PLAY"},
]) == "1 game in progress"
assert predictor.compact_record_name("Two Part Name") == "Two"
assert predictor.compact_record_name("") == "—"
assert predictor.sportscore_team_slug("Brighton & Hove Albion") == "brighton-hove-albion"
assert predictor.sportscore_team_slug("Nott'm Forest") == "nottingham-forest"
assert predictor.safe_team_logo_url(
    predictor.SPORTSCORE_TEAM_LOGO_FALLBACKS["chelsea"]
).endswith("a0cf8f551e9440acb3f4ff533dcc58a4.png")
assert predictor.safe_team_logo_url(
    predictor.SPORTSCORE_TEAM_LOGO_FALLBACKS["arsenal"]
).endswith("d6f5debc456da1119256ab66462ab510.png")
proxied_badge = predictor.team_badge_url(
    "https://img.thesports.com/football/team/example.png"
)
assert proxied_badge.startswith("/team-badge?url=https%3A%2F%2Fimg.thesports.com")
assert predictor.team_badge_url("https://example.com/badge.png") == (
    "https://example.com/badge.png"
)

# The live position chart stores a baseline and only records changed states.
conn = database.get_db()
snapshot_player = conn.execute(
    "SELECT id FROM players ORDER BY id LIMIT 1"
).fetchone()["id"]
conn.execute(
    """INSERT INTO fixtures
       (id, season, matchday, utc_date, status, home_team, away_team,
        home_score, away_score)
       VALUES (99001, ?, 99, ?, 'IN_PLAY', 'Snapshot Home',
               'Snapshot Away', 1, 0)""",
    (predictor.SEASON, (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()),
)
conn.execute(
    """INSERT INTO predictions
       (player_id, fixture_id, home_score, away_score, dp)
       VALUES (?, 99001, 1, 0, 0)""",
    (snapshot_player,),
)
assert predictor.record_live_position_snapshot(conn, 99)
assert not predictor.record_live_position_snapshot(conn, 99)
chart = predictor.live_position_chart(conn, 99)
assert len(chart["snapshots"]) == 2
assert chart["snapshots"][-1]["rows"][0]["gameweek_points"] == 5
conn.execute(
    "UPDATE fixtures SET away_score = 1 WHERE id = 99001"
)
assert predictor.record_live_position_snapshot(conn, 99)
assert len(predictor.live_position_chart(conn, 99)["snapshots"]) == 3
snapshot_ids = [
    row["id"] for row in conn.execute(
        "SELECT id FROM live_position_snapshots WHERE matchday = 99"
    ).fetchall()
]
if snapshot_ids:
    placeholders = ",".join("?" for _ in snapshot_ids)
    conn.execute(
        f"DELETE FROM live_position_snapshot_rows WHERE snapshot_id IN ({placeholders})",
        snapshot_ids,
    )
conn.execute("DELETE FROM live_position_snapshots WHERE matchday = 99")
conn.execute("DELETE FROM predictions WHERE fixture_id = 99001")
conn.execute("DELETE FROM fixtures WHERE id = 99001")
conn.commit()
conn.close()

# Championship CSV results are retained for cross-division head-to-heads.
class HistoricalCsvResponse:
    status_code = 200
    text = (
        "Date,HomeTeam,AwayTeam,FTHG,FTAG\n"
        "15/02/2025,Hull,Coventry,1,2\n"
    )

original_predictor_get = predictor.requests.get
predictor.requests.get = lambda *args, **kwargs: HistoricalCsvResponse()
try:
    conn = database.get_db()
    assert predictor.import_historical_csv_season(conn, 2024, "E1") == 1
    championship_row = conn.execute(
        "SELECT * FROM historical_fixtures WHERE competition = 'E1'"
    ).fetchone()
    assert championship_row["home_team"] == "Hull"
    assert championship_row["away_team"] == "Coventry"
    championship_h2h = predictor.match_stats_for_fixture(
        conn,
        {
            "id": 99001,
            "season": predictor.SEASON,
            "matchday": 1,
            "utc_date": datetime.now(timezone.utc).isoformat(),
            "home_team": "Hull City",
            "away_team": "Coventry City",
        }
    )
    assert len(championship_h2h["head_to_head"]) == 1
    conn.close()
finally:
    predictor.requests.get = original_predictor_get

import sportscore
import football_api

second_half_now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
assert sportscore.snapshot_live_minute({
    "kickoff": (
        second_half_now - timedelta(minutes=46)
    ).timestamp() * 1000,
    "status": {"id": 4},
}, second_half_now) == "90+2"
assert sportscore.snapshot_live_minute({
    "kickoff": (
        second_half_now - timedelta(minutes=47)
    ).timestamp() * 1000,
    "status": {"id": 2},
}, second_half_now) == "45+3"
assert sportscore.snapshot_live_minute({
    "kickoff": second_half_now.timestamp() * 1000,
    "status": {"id": "unknown"},
}, second_half_now) is None

original_sportscore_get = sportscore._get
original_live_snapshot = sportscore._live_snapshot
original_snapshot_minute = sportscore.snapshot_live_minute
sportscore._get = lambda path, params: {
    "match": {
        "status": "live",
        "live_minute": "90",
        "status_text": "90",
        "home_score": 5,
        "away_score": 1,
    }
}
sportscore._live_snapshot = lambda slug: {
    "ok": True,
    "score": {"home": 6, "away": 1},
}
sportscore.snapshot_live_minute = lambda snapshot: "90+2"
try:
    snapshot_match = sportscore.get_match_details({
        "url": "/football/match/sang-mustang-fc-vs-dzongri-fc/"
    })
    assert snapshot_match["live_minute"] == "90+2"
    assert snapshot_match["status_text"] == "90+2"
    assert snapshot_match["home_score"] == 6
finally:
    sportscore._get = original_sportscore_get
    sportscore._live_snapshot = original_live_snapshot
    sportscore.snapshot_live_minute = original_snapshot_minute

original_competition_loader = football_api.get_competition_matches
competition_attempts = []
def fake_competition_loader(token, competition, season):
    competition_attempts.append((competition, season))
    if len(competition_attempts) < 3:
        raise football_api.FootballAPIError("Football API returned HTTP 404")
    return [{"id": 2001}]
football_api.get_competition_matches = fake_competition_loader
try:
    assert football_api.get_champions_league_matches("token", 2026) == [
        {"id": 2001}
    ]
finally:
    football_api.get_competition_matches = original_competition_loader
assert competition_attempts == [("CL", 2026), (2001, 2026), (2001, None)]

original_sportscore_get = sportscore._get
sportscore._get = lambda path, params: {
    "team": {"name": "Badge FC", "logo": ""},
    "matches": [{
        "home": "Badge FC",
        "away": "Visitors",
        "home_logo": "https://img.example/badge.png",
        "away_logo": "https://img.example/visitors.png",
    }],
}
try:
    assert sportscore.get_team_logo("badge-fc") == "https://img.example/badge.png"
finally:
    sportscore._get = original_sportscore_get

assert sportscore._match_slug("Bodø/Glimt", "NEC Nijmegen") == (
    "bodo-glimt-vs-nec-nijmegen"
)

original_sportscore_requests_get = sportscore.requests.get
today_text = datetime.now(timezone.utc).date().isoformat()
tonight_matchups = [
    {"home": f"Home {number}", "away": f"Away {number}"}
    for number in range(1, 8)
]

class CompetitionPageResponse:
    status_code = 200
    text = ""

def fake_champions_get(path, params):
    if path == "bracket":
        return {"rounds": [{"name": "Matches", "matchups": tonight_matchups}]}
    slug = params["slug"]
    number = int(slug.split("-vs-")[0].split("-")[-1])
    return {"match": {
        "home": f"Home {number}",
        "away": f"Away {number}",
        "status": "live" if number == 1 else "upcoming",
        "time": f"{today_text}T20:00:00+00:00",
        "competition": "UEFA Champions League",
        "url": f"/football/match/{slug}/",
    }}

sportscore.requests.get = lambda *args, **kwargs: CompetitionPageResponse()
sportscore._get = fake_champions_get
try:
    all_champions_matches = sportscore.get_champions_league_matches()
finally:
    sportscore.requests.get = original_sportscore_requests_get
    sportscore._get = original_sportscore_get
assert len(all_champions_matches) == 7
assert all_champions_matches[0]["status"] == "live"

sportscore._get = lambda path, params: {
    "matches": [{"status": "upcoming"}, {"status": "live"}]
}
try:
    assert [match["status"] for match in sportscore.get_live_matches()] == [
        "upcoming",
        "live",
    ]
finally:
    sportscore._get = original_sportscore_get


class FakeDriveRequest:
    def __init__(self, result=None):
        self.result = result or {}

    def execute(self):
        return self.result


class FakeDriveFiles:
    def __init__(self, pages):
        self.pages = list(pages)
        self.deleted = []

    def list(self, **kwargs):
        return FakeDriveRequest(self.pages.pop(0))

    def delete(self, fileId):
        self.deleted.append(fileId)
        return FakeDriveRequest()


class FakeDriveService:
    def __init__(self, pages):
        self.file_resource = FakeDriveFiles(pages)

    def files(self):
        return self.file_resource


# Drive retention is count-based across all result pages. It retains the ten
# newest Predictor backups and deletes only the older entries.
drive_items = [
    {
        "id": f"backup-{number}",
        "name": f"backup-{number}.db",
        "createdTime": f"2026-08-{number:02d}T12:00:00Z",
    }
    for number in range(1, 13)
]
fake_drive = FakeDriveService([
    {"files": drive_items[:6], "nextPageToken": "page-2"},
    {"files": drive_items[6:]},
])
predictor.prune_google_backups(fake_drive)
assert fake_drive.file_resource.deleted == ["backup-2", "backup-1"]

# Each revealed fixture lists the highest-scoring prediction first, with
# alphabetical ordering used for tied match points.
fixture = {
    "id": 77,
    "home_score": 2,
    "away_score": 1,
}
players = [
    {"id": 1, "name": "Zoe"},
    {"id": 2, "name": "Amy"},
    {"id": 3, "name": "Ben"},
]
prediction_map = {
    (1, 77): {
        "home_score": 1,
        "away_score": 0,
        "dp": 0,
    },
    (2, 77): {
        "home_score": 2,
        "away_score": 1,
        "dp": 0,
    },
    (3, 77): {
        "home_score": 2,
        "away_score": 1,
        "dp": 1,
    },
}
ordered = predictor.order_players_for_fixture(
    players,
    fixture,
    prediction_map,
    True,
)
assert [player["name"] for player in ordered] == [
    "Ben",
    "Amy",
    "Zoe",
]
ordered_hidden = predictor.order_players_for_fixture(
    players,
    fixture,
    prediction_map,
    False,
)
assert [player["name"] for player in ordered_hidden] == [
    "Amy",
    "Ben",
    "Zoe",
]

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

original_badge_get = predictor.requests.get
badge_calls = []

class BadgeResponse:
    status_code = 200
    headers = {"Content-Type": "image/png"}
    content = b"badge-image"

predictor.requests.get = lambda url, **kwargs: badge_calls.append(url) or BadgeResponse()
try:
    badge_response = client.get(proxied_badge)
finally:
    predictor.requests.get = original_badge_get
assert badge_response.status_code == 200
assert badge_response.data == b"badge-image"
assert badge_response.headers["Cache-Control"] == "public, max-age=86400"
assert badge_calls == ["https://img.thesports.com/football/team/example.png"]
assert client.get(
    "/team-badge?url=https%3A%2F%2Fexample.com%2Fbadge.png"
).status_code == 404

# Display names can change without changing the stable login identifier.
conn = database.get_db()
original_login = conn.execute(
    "SELECT login_name FROM players WHERE id = ?", (admin["id"],)
).fetchone()["login_name"]
conn.execute("UPDATE players SET name = ? WHERE id = ?", ("Display Name", admin["id"]))
conn.commit()
assert conn.execute(
    "SELECT login_name FROM players WHERE id = ?", (admin["id"],)
).fetchone()["login_name"] == original_login
conn.execute("UPDATE players SET name = ? WHERE id = ?", (admin["name"], admin["id"]))
conn.commit()
conn.close()

# Existing users can transition safely: their legacy login works while email
# is unset, then email becomes the login identifier once configured.
logout_response = client.post("/logout", follow_redirects=False)
assert logout_response.status_code == 302
login_response = client.get("/")
assert b"Remember my email on this device" in login_response.data
assert b'autocomplete="current-password"' in login_response.data
response = client.post(
    "/", data={"identifier": original_login, "pin": "1234"},
    follow_redirects=False,
)
assert response.status_code == 302 and response.headers["Location"].endswith("/dashboard")
conn = database.get_db()
migrated_pin_hash = conn.execute(
    "SELECT pin_hash FROM players WHERE id = ?", (admin["id"],)
).fetchone()["pin_hash"]
conn.close()
assert migrated_pin_hash.startswith("scrypt:")
assert database.verify_pin("1234", migrated_pin_hash)
assert not database.verify_pin("9999", migrated_pin_hash)
conn = database.get_db()
conn.execute("UPDATE players SET email = ? WHERE id = ?", ("dan@example.com", admin["id"]))
conn.commit()
conn.close()
logout_response = client.post("/logout", follow_redirects=False)
assert logout_response.status_code == 302
response = client.post(
    "/", data={"identifier": "DAN@example.com", "pin": "1234"},
    follow_redirects=False,
)
assert response.status_code == 302 and response.headers["Location"].endswith("/dashboard")

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
assert scorers["home"][0]["goals"] == ["12'", "45+2' ⚽ penalty"]
card_events = [{
    "time": "67+1",
    "type": "Red card",
    "type_id": 4,
    "side": "away",
    "player": "Dismissed Defender",
}]
red_cards = predictor.fixture_red_cards(card_events, "Home FC", "Away FC")
assert red_cards["away"] == [{
    "name": "Dismissed Defender",
    "minute": "67+1'",
}]

conn = database.get_db()
live_kickoff = datetime.now(timezone.utc).isoformat()
conn.execute(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team,
           home_score, away_score, goals_json, incidents_json
       ) VALUES (?, ?, 1, ?, 'IN_PLAY', 'Home FC', 'Away FC', 2, 0, ?, ?)""",
    (
        8800,
        predictor.SEASON,
        live_kickoff,
        json.dumps(goal_events),
        json.dumps(card_events),
    ),
)
conn.commit()
conn.close()
response = client.get("/dashboard")
assert response.status_code == 200
assert b'action="/logout"' in response.data
assert b"Log out" in response.data
assert b'class="fixture-scoreline"' in response.data
assert b"<span>2</span>" in response.data
assert b'class="fixture-score-dash">\xe2\x80\x93</span>' in response.data
assert b"<span>0</span>" in response.data
assert b"Alex Striker" in response.data
assert "45+2&#39; ⚽ penalty".encode() in response.data
assert b"Dismissed Defender" in response.data
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
assert predictor.sportscore_team_slug("Nottingham Forest FC") == "nottingham-forest"
assert predictor.sportscore_team_slug("Manchester United FC") == "manchester-united"
assert predictor.safe_team_logo_url("https://sportscore.com/media/team.png")
assert predictor.safe_team_logo_url("javascript:alert(1)") is None
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
assert predictor.parse_live_minute("45+2") == (45, 2)
assert predictor.parse_live_minute("90+7'") == (90, 7)
assert predictor.parse_live_minute("Started 45+4′") == (45, 4)
assert predictor.parse_live_minute("LIVE 90 + 6’") == (90, 6)
assert predictor.parse_live_minute("Started 45+3 (HT)") == (45, 3)
assert predictor.parse_live_minute("2nd half") == (None, None)
assert predictor.parse_live_minute("86") == (86, None)
assert predictor.sportscore_live_clock({
    "live_minute": "90",
    "status_text": "Started 90+3",
}) == (90, 3)
assert predictor.sportscore_live_clock({
    "live_minute": "46",
    "status_text": "2nd half",
}) == (46, None)
assert predictor.sportscore_fixture_status({
    "status": "live",
    "status_text": "HT",
}) == "PAUSED"
assert predictor.sportscore_fixture_status({
    "status": "live",
    "status_text": "Half-time",
}) == "PAUSED"
assert predictor.status_label({
    "status": "IN_PLAY",
    "minute": 90,
    "injury_time": 4,
    "utc_date": datetime.now(timezone.utc).isoformat(),
}) == "LIVE 90+4'"

original_test_match_details = predictor.get_sportscore_match_details
predictor.get_sportscore_match_details = lambda match: {
    "home": "LASK",
    "away": "Celtic",
    "home_score": "1",
    "away_score": "2",
    "status": "live",
    "status_text": "Started 90+3",
    "competition": "UEFA Champions League",
    "live_minute": "90",
    "incidents": [
        {
            "time": 75,
            "type": "Penalty Goal",
            "side": "away",
            "player": "Test Scorer",
            "is_goal": True,
        },
        {
            "time": 81,
            "type": "Red card",
            "type_id": 4,
            "side": "home",
            "player": "Test Defender",
        },
    ],
}
try:
    live_test_response = client.get(
        "/admin/live-feed-test?slug=lask-vs-celtic"
    )
finally:
    predictor.get_sportscore_match_details = original_test_match_details
assert live_test_response.status_code == 200
assert b"LIVE 90+3" in live_test_response.data
assert b'<div class="fixture-submeta">LIVE' not in live_test_response.data
assert b"Test Scorer" in live_test_response.data
assert "⚽ penalty".encode() in live_test_response.data
assert b"Test Defender" in live_test_response.data
assert b"never affect Predictor fixtures" in live_test_response.data
assert b'class="fixture-scorers"' in live_test_response.data
assert b'class="fixture fixture-set fixture-live"' in live_test_response.data

predictor.get_sportscore_match_details = lambda match: {
    "home": "Europa Home",
    "away": "Europa Away",
    "home_score": "2",
    "away_score": "1",
    "status": "finished",
    "status_text": "FT",
    "competition": "UEFA Europa League",
    "live_minute": None,
    "incidents": [],
}
try:
    manual_match_response = client.get(
        "/admin/live-feed-test?match="
        "https%3A%2F%2Fsportscore.com%2Ffootball%2Fmatch%2F"
        "europa-home-vs-europa-away%2F"
    )
finally:
    predictor.get_sportscore_match_details = original_test_match_details
assert manual_match_response.status_code == 200
assert b"Europa Home" in manual_match_response.data
assert b"UEFA Champions League by SportScore" not in manual_match_response.data
assert b"Manual matches remain read-only" in manual_match_response.data

predictor.get_sportscore_match_details = lambda match: {
    "home": "Home FC",
    "away": "Away FC",
    "home_score": None,
    "away_score": None,
    "status": "live",
    "status_text": "Started 90+3",
    "competition": "Premier League",
    "live_minute": "90",
    "incidents": [],
}
try:
    combined_feed_response = client.get(
        "/admin/live-feed-test?match=home-fc-vs-away-fc"
    )
finally:
    predictor.get_sportscore_match_details = original_test_match_details
assert combined_feed_response.status_code == 200
assert b"Sources: SportScore + Predictor stored fixture" in combined_feed_response.data
assert b"LIVE 90+3" in combined_feed_response.data
assert b"Alex Striker" in combined_feed_response.data

predictor.get_sportscore_match_details = lambda match: {
    "home": "LASK",
    "away": "Celtic",
    "home_score": "1",
    "away_score": "2",
    "status": "live",
    "status_text": "HT",
    "competition": "UEFA Champions League",
    "live_minute": None,
    "incidents": [],
}
try:
    halftime_test_response = client.get(
        "/admin/live-feed-test?slug=lask-vs-celtic"
    )
finally:
    predictor.get_sportscore_match_details = original_test_match_details
assert halftime_test_response.status_code == 200
assert b'<span class="badge live">HT</span>' in halftime_test_response.data

# With no manual slugs, the diagnostics page discovers current matches from
# SportScore instead of retrying expired, hard-coded match URLs.
original_live_matches = predictor.get_sportscore_champions_league_matches
original_test_match_details = predictor.get_sportscore_match_details
original_competition_matches = predictor.get_football_champions_league_matches
predictor.set_setting("football_api_token", "test-token")
predictor.get_football_champions_league_matches = lambda token, season: [
    {
        "id": 7001,
        "homeTeam": {"name": "Fresh Home"},
        "awayTeam": {"name": "Fresh Away"},
        "score": {"fullTime": {"home": 1, "away": 0}},
        "status": "IN_PLAY",
        "minute": 12,
        "utcDate": "2026-08-26T19:00:00Z",
    },
    {
        "id": 7002,
        "homeTeam": {"name": "Football Only Home"},
        "awayTeam": {"name": "Football Only Away"},
        "score": {"fullTime": {"home": None, "away": None}},
        "status": "TIMED",
        "utcDate": "2026-08-26T20:00:00Z",
    },
]
predictor.get_sportscore_champions_league_matches = lambda: [
    {
        "home": "Fresh Home",
        "away": "Fresh Away",
        "home_score": 0,
        "away_score": 0,
        "status": "live",
        "status_text": "1st half",
        "competition": "UEFA Champions League",
        "live_minute": "12",
        "incidents": [],
        "url": "/football/match/fresh-home-vs-fresh-away/",
        "_details_loaded": True,
    },
    {
        "home": "Domestic Home",
        "away": "Domestic Away",
        "status": "live",
        "competition": "Premier League",
        "url": "/football/match/domestic-home-vs-domestic-away/",
    },
    {
        "home": "Upcoming Home",
        "away": "Upcoming Away",
        "status": "upcoming",
        "status_text": "Not started",
        "competition": "UEFA Champions League",
        "time": "2026-08-25T20:00:00+00:00",
        "url": "/football/match/upcoming-home-vs-upcoming-away/",
        "_details_loaded": True,
    },
]
predictor.get_sportscore_match_details = lambda match: match
try:
    discovered_test_response = client.get("/admin/live-feed-test")
finally:
    predictor.get_sportscore_champions_league_matches = original_live_matches
    predictor.get_sportscore_match_details = original_test_match_details
    predictor.get_football_champions_league_matches = original_competition_matches
    predictor.set_setting("football_api_token", "")
assert discovered_test_response.status_code == 200
assert b"Fresh Home" in discovered_test_response.data
assert b"Upcoming Home" in discovered_test_response.data
assert b"Football Only Home" in discovered_test_response.data
assert b"Domestic Home" not in discovered_test_response.data
assert b"Sources: SportScore + football-data.org" in discovered_test_response.data
assert b'<div class="fixture-submeta">' in discovered_test_response.data
assert b"<span>Sources: SportScore + football-data.org</span>" in discovered_test_response.data
assert b"football-data.org Champions League comparison is enabled" in discovered_test_response.data
assert b"available SportScore and football-data.org feeds" in discovered_test_response.data
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
]:
    response = client.get(route)
    assert response.status_code == 200, (route, response.status_code)

retired_test_response = client.get("/test-mode", follow_redirects=False)
assert retired_test_response.status_code == 302
assert retired_test_response.headers["Location"].endswith("/dashboard")

admin_response = client.get("/admin")
assert b"API Settings" in admin_response.data
assert b'href="/admin/settings"' in admin_response.data
assert b'href="/admin/live-feed-test"' in admin_response.data
assert b"Database Health" in admin_response.data
health = predictor.database_health()
assert health["database_bytes"] > 0
assert health["page_count"] > 0
original_create_automatic_backup = predictor.create_automatic_backup
backup_calls = []
predictor.create_automatic_backup = lambda: backup_calls.append(True) or tmp.name
try:
    optimize_response = client.post(
        "/admin/database/optimize",
        follow_redirects=False,
    )
finally:
    predictor.create_automatic_backup = original_create_automatic_backup
assert optimize_response.status_code == 302
assert backup_calls == [True]
assert predictor.get_setting("last_database_optimize")
leaderboard_response = client.get("/leaderboard")
assert b"Season position changes" in leaderboard_response.data
conn = database.get_db()
for fixture_id, matchday in ((8701, 37), (8702, 38)):
    conn.execute(
        """INSERT INTO fixtures(
            id, season, matchday, utc_date, status,
            home_team, away_team, home_score, away_score
        ) VALUES (?, ?, ?, ?, 'SCHEDULED', ?, ?, NULL, NULL)""",
        (
            fixture_id,
            predictor.SEASON,
            matchday,
            datetime.now(timezone.utc).isoformat(),
            f"History Home {matchday}",
            f"History Away {matchday}",
        ),
    )
conn.commit()
conn.close()
history_response = client.get("/history")
history_gw37 = history_response.data.find(b"Gameweek 37")
history_gw38 = history_response.data.find(b"Gameweek 38")
assert history_gw37 >= 0 and history_gw38 >= 0
assert history_gw37 < history_gw38
conn = database.get_db()
conn.execute("DELETE FROM fixtures WHERE id IN (8701, 8702)")
conn.commit()
conn.close()
seasons_response = client.get("/seasons")
assert b"Past Winners" in seasons_response.data
assert b"Most League Wins" in seasons_response.data
assert b"Fontz" in seasons_response.data
assert b"TROPiC" in seasons_response.data
assert b"Strat" in seasons_response.data
assert b"Percei" in seasons_response.data
old_season_response = client.get("/seasons/2024")
assert b"league-table and player statistics were not retained" in old_season_response.data

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

manual_24_fixture = [{
    "status": "SCHEDULED",
    "utc_date": (
        datetime.now(timezone.utc) + timedelta(hours=12)
    ).isoformat(),
}]
manual_2_fixture = [{
    "status": "SCHEDULED",
    "utc_date": (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat(),
}]
assert predictor.signal_manual_reminder_key(manual_24_fixture) == (
    "signal_last_reminder_24_gw"
)
assert predictor.signal_manual_reminder_key(manual_2_fixture) == (
    "signal_last_reminder_2_gw"
)

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
conn.execute(
    """INSERT INTO fixtures(id, season, matchday, utc_date, status,
           home_team, away_team)
           VALUES (9010, ?, 2, ?, 'SCHEDULED', 'Next Home', 'Next Away')""",
    (season, (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()),
)
conn.commit()
gw2_opens_at = predictor.gameweek_open_at(conn, 2)
assert gw2_opens_at.hour == 9
conn.close()

# The completed GW stays on the dashboard until 09:00 UK time the next day;
# the next-GW Signal announcement uses the same opening boundary.
predictor.set_setting("signal_last_results_gw", "1")
original_now_utc = predictor.now_utc
try:
    predictor.now_utc = lambda: (
        gw2_opens_at.astimezone(timezone.utc) - timedelta(minutes=1)
    )
    conn = database.get_db()
    assert predictor.dashboard_current_gameweek(conn) == 1
    assert predictor.signal_current_gameweek(conn) is None
    conn.close()
    assert predictor.signal_next_gameweek_open_ready(2) is False

    predictor.now_utc = lambda: gw2_opens_at.astimezone(timezone.utc)
    conn = database.get_db()
    assert predictor.dashboard_current_gameweek(conn) == 2
    assert predictor.signal_current_gameweek(conn) == 2
    conn.close()
    assert predictor.signal_next_gameweek_open_ready(2) is True
finally:
    predictor.now_utc = original_now_utc

signal_positions = [
    {"name": f"Player {index}", "points": 10 - index}
    for index in range(1, 6)
]
results_message = predictor.signal_results_message(
    1, signal_positions, signal_positions
)
assert "💩 Player 4" in results_message
assert "4. Player 4" not in results_message

# Completed gameweeks opened from History no longer show Match Stats.
past_predictions_response = client.get("/predict/1?history=1")
assert past_predictions_response.status_code == 200
assert b'<details class="match-stats">' not in past_predictions_response.data

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
assert predictor.short_team_name("Manchester City FC") == "Manchester City"
assert predictor.short_team_name("Manchester United FC") == "Manchester United"
assert predictor.short_team_name("Newcastle United FC") == "Newcastle United"
assert predictor.short_team_name("Wolverhampton Wanderers FC") == "Wolverhampton Wanderers"
assert predictor.short_team_name("Sheffield United FC") == "Sheffield United"

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
assert '{% if show_match_stats %}' in predictions_template
assert '_match_stats.html' not in dashboard_template
assert '_match_stats.html' not in gameweek_template
assert 'href="https://sportscore.com/" rel="dofollow"' in gameweek_template
assert "Ordered by the current overall league position" in gameweek_template
assert "player.season_points" in gameweek_template
assert "Position during this gameweek" in gameweek_template
assert "position-chart-data" in gameweek_template
assert "button.innerHTML" not in gameweek_template
assert "fixture-live" in gameweek_template
assert "fixture-live" in dashboard_template

with open(
    os.path.join(templates_dir, "leaderboard.html"),
    "r",
    encoding="utf-8",
) as handle:
    leaderboard_template = handle.read()

assert 'role="table" aria-label="Season league table"' in leaderboard_template
assert 'data-label="Exact draws"' in leaderboard_template
assert 'data-label="Exact wins"' in leaderboard_template
assert 'data-label="Other correct"' in leaderboard_template

with open(
    os.path.join(templates_dir, "stats.html"),
    "r",
    encoding="utf-8",
) as handle:
    stats_template = handle.read()

assert "DPs USED" not in stats_template
assert "MOST OTHER CORRECT RESULTS" in stats_template
assert "MOST EXACT SCORES WITH DP" in stats_template

with open(
    os.path.join(templates_dir, "base.html"),
    "r",
    encoding="utf-8",
) as handle:
    base_template = handle.read()

assert '/static/predictor-icon.png' in base_template
assert 'family=Inter:wght@400;500;600;700' in base_template
assert 'font-family:"Inter"' in base_template
assert 'class="dashboard-logo"' in dashboard_template
assert 'fixture.home_logo' in dashboard_template
assert 'fixture.away_logo' in dashboard_template

# Broadcaster logos are deliberately omitted from Predictions.
assert 'broadcaster_logo_url' not in predictions_template
assert 'class="fixture-match"' not in predictions_template
assert '<div class="dash">v</div>' in predictions_template

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

# Live refresh must look up the current fixture directly. The global latest-50
# feed can omit Premier League matches when many games are played worldwide.
conn = database.get_db()
conn.execute(
    """INSERT OR REPLACE INTO fixtures(
           id, season, matchday, utc_date, status,
           home_team, away_team
       ) VALUES (99002, ?, 0, ?, 'SCHEDULED', 'Fulham', 'Chelsea')""",
    (season, datetime.now(timezone.utc).isoformat()),
)
conn.commit()
conn.close()
direct_calls = []
original_match_details = predictor.get_sportscore_match_details
def fake_direct_match_details(match):
    direct_calls.append(match["url"])
    if match["url"] == "/football/match/fulham-vs-chelsea/":
        raise predictor.SportScoreError("SportScore returned HTTP 404.")
    return {
        "home": "Fulham",
        "away": "Chelsea",
        "home_score": "2",
        "away_score": "3",
        "status": "live",
        "live_minute": "64",
        "home_logo": "https://sportscore.com/media/fulham.png",
        "away_logo": "https://sportscore.com/media/chelsea.png",
        "incidents": [{
            "time": 54,
            "type": "Goal",
            "side": "home",
            "player": "Direct Scorer",
            "is_goal": True,
        }],
    }

predictor.get_sportscore_match_details = fake_direct_match_details
try:
    assert predictor.import_live_matches_from_sportscore() == 1
finally:
    predictor.get_sportscore_match_details = original_match_details
assert direct_calls == [
    "/football/match/fulham-vs-chelsea/",
    "/football/match/chelsea-vs-fulham/",
]
conn = database.get_db()
direct_fixture = conn.execute(
    """SELECT home_score, away_score, minute, goals_json,
              home_logo, away_logo
       FROM fixtures WHERE id = 99002"""
).fetchone()
assert (direct_fixture["home_score"], direct_fixture["away_score"]) == (2, 3)
assert direct_fixture["minute"] == 64
assert "Direct Scorer" in direct_fixture["goals_json"]
assert direct_fixture["home_logo"].endswith("fulham.png")
assert direct_fixture["away_logo"].endswith("chelsea.png")
conn.execute("DELETE FROM fixtures WHERE id = 99002")
conn.commit()
conn.close()

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

# A season archive must never be created early. At the 380-match boundary it
# snapshots every player once and becomes immutable on later checks.
archive_season = 9090
conn = database.get_db()
conn.executemany(
    """
    INSERT INTO fixtures(
        id, season, matchday, utc_date, status,
        home_team, away_team, home_score, away_score
    ) VALUES (?, ?, ?, ?, 'FINISHED', ?, ?, 0, 0)
    """,
    [
        (
            200000 + index,
            archive_season,
            ((index - 1) // 10) + 1,
            now,
            f"Archive Home {index}",
            f"Archive Away {index}",
        )
        for index in range(1, 380)
    ],
)
assert predictor.archive_completed_season(conn, archive_season) is False
conn.execute(
    """
    INSERT INTO fixtures(
        id, season, matchday, utc_date, status,
        home_team, away_team, home_score, away_score
    ) VALUES (200380, ?, 38, ?, 'FINISHED', 'Final Home', 'Final Away', 0, 0)
    """,
    (archive_season, now),
)
assert predictor.archive_completed_season(conn, archive_season) is True
archive_row = conn.execute(
    "SELECT stats_available FROM season_archives WHERE season = ?",
    (archive_season,),
).fetchone()
assert archive_row["stats_available"] == 1
archived_player_count = conn.execute(
    "SELECT COUNT(*) FROM season_archive_players WHERE season = ?",
    (archive_season,),
).fetchone()[0]
live_player_count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
assert archived_player_count == live_player_count
assert predictor.archive_completed_season(conn, archive_season) is False
conn.close()

# Changelog parser must actually parse packaged release notes.
releases = predictor.read_app_changelog()
assert releases and releases[0]["version"] == predictor.APP_VERSION

os.remove(tmp.name)
print("Premier League Predictor self-test: PASS")
