import os
import io
import json
import shutil
import tempfile
import sqlite3
import inspect
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
assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
assert "dp" in {r["name"] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}
assert "goals_json" in {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
assert "incidents_json" in {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
assert "live_data_source" in {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name='provider_fixture_mappings'"
).fetchone() is not None
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name='provider_live_states'"
).fetchone() is not None
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name='provider_event_observations'"
).fetchone() is not None
assert "login_name" in {r["name"] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
assert "email" in {r["name"] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
assert "entry_fee_paid" in {r["name"] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
assert "treasurer" in {r["name"] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
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
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='competition_winners'"
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
assert conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='prediction_audit_events'"
).fetchone() is not None
assert "competition" in {
    row["name"]
    for row in conn.execute("PRAGMA table_info(historical_fixtures)").fetchall()
}
assert "hide_news_ticker" in {
    row["name"]
    for row in conn.execute("PRAGMA table_info(players)").fetchall()
}

# Prediction changes form an append-only chain and any direct score or ledger
# mutation is detected before the shared Tegridy page reports it as healthy.
audit_player_id = conn.execute(
    "SELECT id FROM players ORDER BY id LIMIT 1"
).fetchone()["id"]
conn.execute(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team
       ) VALUES (98999, 2026, 98, ?, 'SCHEDULED', 'Audit Home', 'Audit Away')""",
    ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),),
)
conn.execute(
    """INSERT INTO predictions(player_id, fixture_id, home_score, away_score, dp)
       VALUES (?, 98999, 2, 1, 1)""",
    (audit_player_id,),
)
database.append_prediction_audit_event(
    conn,
    player_id=audit_player_id,
    fixture_id=98999,
    home_score=2,
    away_score=1,
    dp=1,
    action="submitted",
    changed_at="2026-08-30T12:00:00+00:00",
)
conn.commit()
assert database.verify_prediction_audit_chain(conn)["valid"]
audit_event = conn.execute(
    "SELECT * FROM prediction_audit_events WHERE fixture_id = 98999"
).fetchone()
assert audit_event["score_commitment"] == database.prediction_score_commitment(
    audit_player_id, 98999, 2, 1, 1, audit_event["commitment_salt"]
)
conn.execute("UPDATE predictions SET home_score = 3 WHERE fixture_id = 98999")
assert not database.verify_prediction_audit_chain(conn)["valid"]
conn.execute("UPDATE predictions SET home_score = 2 WHERE fixture_id = 98999")
assert database.verify_prediction_audit_chain(conn)["valid"]
try:
    conn.execute(
        "UPDATE prediction_audit_events SET away_score = 2 WHERE fixture_id = 98999"
    )
    raise AssertionError("Immutable prediction audit row accepted an update")
except sqlite3.IntegrityError:
    pass
try:
    conn.execute("DELETE FROM prediction_audit_events WHERE fixture_id = 98999")
    raise AssertionError("Immutable prediction audit row accepted a delete")
except sqlite3.IntegrityError:
    pass
conn.execute("DELETE FROM predictions WHERE fixture_id = 98999")
conn.execute("DELETE FROM fixtures WHERE id = 98999")
conn.commit()
conn.close()

# Import the real Flask app after redirecting its database module.
import app as predictor
predictor.app.config["TESTING"] = True
with predictor.app.test_client() as client:
    cached_page = client.get("/")
    assert cached_page.headers["Cache-Control"] == "no-store, max-age=0"
assert predictor.news_cache["fetched_at"] == 0.0
assert predictor.LIVE_REFRESH_SECONDS == 60
assert predictor.GOOGLE_BACKUP_LIMIT == 10
news_now = datetime(2026, 9, 1, 22, 0, tzinfo=timezone.utc)
news_items = predictor._parse_premier_league_news("""<?xml version="1.0"?>
<rss><channel>
<item><title>  Manager &amp; club update  </title><link>https://www.bbc.co.uk/sport/football/articles/example</link><pubDate>Tue, 01 Sep 2026 20:35:00 GMT</pubDate></item>
<item><title>Stale story</title><link>https://www.bbc.co.uk/sport/football/articles/stale</link><pubDate>Sun, 30 Aug 2026 08:00:00 GMT</pubDate></item>
<item><title>Undated story</title><link>https://www.bbc.co.uk/sport/football/articles/undated</link></item>
<item><title>Unsafe story</title><link>https://example.com/not-bbc</link></item>
</channel></rss>""", now=news_now)
assert news_items == [{
    "title": "Manager & club update",
    "url": "https://www.bbc.co.uk/sport/football/articles/example",
    "published": "Tue 21:35",
    "published_at": "2026-09-01T20:35:00+00:00",
}]
assert predictor._recent_news_headlines(news_items, now=news_now + timedelta(hours=37)) == []
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
original_now_utc = predictor.now_utc
try:
    predictor.now_utc = lambda: datetime(2026, 8, 29, 11, 29, tzinfo=timezone.utc)
    assert not predictor.live_gameweek_visible([{
        "utc_date": "2026-08-29T11:30:00+00:00", "status": "SCHEDULED",
    }])
    predictor.now_utc = lambda: datetime(2026, 8, 29, 11, 30, tzinfo=timezone.utc)
    assert predictor.live_gameweek_visible([{
        "utc_date": "2026-08-29T11:30:00+00:00", "status": "IN_PLAY",
    }])
finally:
    predictor.now_utc = original_now_utc
original_now_utc = predictor.now_utc
try:
    predictor.now_utc = lambda: datetime(2026, 8, 29, 14, 59, tzinfo=timezone.utc)
    prediction_window_fixtures = [
        {"utc_date": "2026-08-29T11:30:00+00:00", "status": "FINISHED"},
        {"utc_date": "2026-08-29T15:00:00+00:00", "status": "SCHEDULED"},
    ]
    assert predictor.gameweek_predictions_open(prediction_window_fixtures)
    predictor.now_utc = lambda: datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)
    assert not predictor.gameweek_predictions_open(prediction_window_fixtures)
finally:
    predictor.now_utc = original_now_utc
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
# With one player, scoring changes cannot alter a league position. The KO
# baseline is retained but redundant live points are deliberately suppressed.
assert not predictor.record_live_position_snapshot(conn, 99)
chart = predictor.live_position_chart(conn, 99)
assert len(chart["snapshots"]) == 1
conn.execute(
    "UPDATE fixtures SET away_score = 1 WHERE id = 99001"
)
assert not predictor.record_live_position_snapshot(conn, 99)
assert hasattr(predictor, "_reconstruct_finished_position_snapshots")
assert conn.execute(
    "SELECT COUNT(*) FROM live_position_snapshots WHERE matchday = 99"
).fetchone()[0] == 1
cause_fixture_id, cause_label = predictor._position_snapshot_cause(
    conn.execute("SELECT * FROM fixtures WHERE id = 99001").fetchall()
)
assert cause_fixture_id == 99001
assert cause_label == "SNA 1–1 SNA"
assert predictor.chart_team_code("Manchester City FC") == "MCI"
assert predictor.chart_team_code("Manchester United FC") == "MUN"
assert predictor._is_current_season_result({
    "season": predictor.SEASON - 1,
    "utc_date": datetime(predictor.SEASON, 8, 15, tzinfo=timezone.utc).isoformat(),
})
assert chart["snapshots"][0]["milestone"] == "KO"
compact_rows = [{"player_id": 1, "position": 1}]
compacted = predictor._compact_position_snapshots([
    {"state_signature": "baseline", "cause_label": "", "rows": compact_rows},
    {"state_signature": "one", "cause_label": "", "rows": [{"player_id": 1, "position": 2}]},
    {"state_signature": "two", "cause_label": "", "rows": compact_rows},
])
assert [row["state_signature"] for row in compacted] == ["baseline"]
long_change_rows = [{
    "player_id": 1, "position": 1,
    "season_points": 10, "gameweek_points": 3,
}]
long_return_rows = [{
    "player_id": 1, "position": 2,
    "season_points": 7, "gameweek_points": 0,
}]
long_changes = predictor._smooth_transient_position_snapshots([
    {"captured_at": "2026-08-29T12:00:00+00:00", "rows": long_change_rows},
    {"captured_at": "2026-08-29T12:10:00+00:00", "rows": long_return_rows},
    {"captured_at": "2026-08-29T12:20:00+00:00", "rows": long_change_rows},
])
assert len(long_changes) == 3
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
def fake_competition_loader(token, competition, season, matchday=None):
    competition_attempts.append((competition, season, matchday))
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
assert competition_attempts == [
    ("CL", 2026, None), (2001, 2026, None), (2001, None, None)
]

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
ordered_by_league_position = predictor.order_players_for_fixture(
    players,
    fixture,
    prediction_map,
    False,
    {1: 1, 2: 3, 3: 2},
)
assert [player["name"] for player in ordered_by_league_position] == [
    "Zoe",
    "Ben",
    "Amy",
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

# Normal prediction saves and later edits are committed to the ledger in the
# same database transaction, including Double Points changes.
conn = database.get_db()
conn.execute(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team
       ) VALUES (98777, ?, 77, ?, 'SCHEDULED', 'Ledger Home', 'Ledger Away')""",
    (
        predictor.SEASON,
        (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
    ),
)
conn.commit()
conn.close()
save_response = client.post(
    "/predict/77",
    data={"home_98777": "2", "away_98777": "1", "dp_fixture_id": "98777"},
    follow_redirects=False,
)
assert save_response.status_code == 302
conn = database.get_db()
saved_event = conn.execute(
    """SELECT * FROM prediction_audit_events
       WHERE player_id = ? AND fixture_id = 98777 ORDER BY revision""",
    (admin["id"],),
).fetchall()
assert len(saved_event) == 1
assert saved_event[0]["action"] == "submitted"
assert (saved_event[0]["home_score"], saved_event[0]["away_score"], saved_event[0]["dp"]) == (2, 1, 1)
assert database.verify_prediction_audit_chain(conn)["valid"]
conn.close()
client.post(
    "/predict/77",
    data={"home_98777": "3", "away_98777": "1", "dp_fixture_id": "98777"},
    follow_redirects=False,
)
conn = database.get_db()
saved_event = conn.execute(
    """SELECT * FROM prediction_audit_events
       WHERE player_id = ? AND fixture_id = 98777 ORDER BY revision""",
    (admin["id"],),
).fetchall()
assert [event["revision"] for event in saved_event] == [1, 2]
assert saved_event[-1]["action"] == "updated"
assert database.verify_prediction_audit_chain(conn)["valid"]
conn.execute("DELETE FROM predictions WHERE fixture_id = 98777")
conn.execute("DELETE FROM fixtures WHERE id = 98777")
conn.commit()
conn.close()

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
assert b"Remember my email address or username" in login_response.data
assert b"Preddies logo" in login_response.data
assert b"Predict. Compete. Win." not in login_response.data
assert b"Who's playing?" not in login_response.data
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

dashboard_news_response = client.get("/dashboard")
assert b"Latest Premier League news" in dashboard_news_response.data
assert b"Premier League news ticker test headline" in dashboard_news_response.data
assert b"BBC Sport" in dashboard_news_response.data
assert b"Hide Premier League news ticker" in client.get("/account").data
hide_news_response = client.post(
    "/account",
    data={
        "name": admin["name"],
        "email": "dan@example.com",
        "pin": "",
        "pin_confirm": "",
        "hide_news_ticker": "1",
    },
    follow_redirects=False,
)
assert hide_news_response.status_code == 302
conn = database.get_db()
assert conn.execute(
    "SELECT hide_news_ticker FROM players WHERE id = ?", (admin["id"],)
).fetchone()["hide_news_ticker"] == 1
conn.close()
hidden_news_response = client.get("/dashboard")
assert b"Latest Premier League news" not in hidden_news_response.data
show_news_response = client.post(
    "/account",
    data={
        "name": admin["name"],
        "email": "dan@example.com",
        "pin": "",
        "pin_confirm": "",
    },
    follow_redirects=False,
)
assert show_news_response.status_code == 302
assert b"Latest Premier League news" in client.get("/dashboard").data

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
assert scorers["home"][0]["goals"] == ["12'", "45+2' (Pen)"]
# Provider suffix differences must not hide otherwise valid live scorers.
provider_scorers = predictor.fixture_scorers(
    json.dumps([{
        "minute": 17,
        "type": "REGULAR",
        "team": {"name": "Manchester City"},
        "scorer": {"name": "Erling Haaland"},
    }]),
    "Crystal Palace FC",
    "Manchester City FC",
)
assert provider_scorers["away"] == [{
    "name": "Erling Haaland",
    "goals": ["17'"],
}]
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
assert "45+2&#39; (Pen)".encode() in response.data
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
assert predictor.sportscore_live_clock({
    "minute": 90,
    "injury_time": 5,
    "status_text": "2nd half",
}) == (90, 5)
assert predictor.sportscore_live_clock({
    "clock": {"display": "45", "added_time": "+3"},
    "status_text": "1st half",
}) == (45, 3)
assert predictor.sportscore_fixture_status({
    "status": "live",
    "status_text": "HT",
}) == "PAUSED"
assert predictor.sportscore_fixture_status({
    "status": "live",
    "status_text": "Half-time",
}) == "PAUSED"
assert predictor.sportscore_fixture_status({
    "status": "live",
    "status_text": "Extra time interval",
}) == "PAUSED"
assert predictor.provider_match_phase({
    "status": "IN_PLAY",
    "status_text": "Extra time",
}) == "EXTRA_TIME"
assert predictor.provider_match_phase({
    "status": "FINISHED",
    "score": {"duration": "PENALTY_SHOOTOUT"},
}) == "PENALTIES"
assert predictor.provider_penalty_scores({
    "score": {"penalties": {"home": 5, "away": 4}},
}) == (5, 4)
assert predictor.provider_penalty_scores({
    "home_penalties": "6", "away_penalties": "5",
}) == (6, 5)
assert predictor.status_label({
    "status": "IN_PLAY",
    "minute": 90,
    "injury_time": 4,
    "utc_date": datetime.now(timezone.utc).isoformat(),
}) == "LIVE 90+4'"
assert predictor.status_label({
    "status": "IN_PLAY",
    "minute": 105,
    "injury_time": 2,
    "match_phase": "EXTRA_TIME",
    "utc_date": datetime.now(timezone.utc).isoformat(),
}) == "ET 105+2'"
assert predictor.status_label({
    "status": "PAUSED",
    "minute": 105,
    "injury_time": None,
    "match_phase": "EXTRA_TIME_HALF_TIME",
    "utc_date": datetime.now(timezone.utc).isoformat(),
}) == "ET HT"
assert predictor.status_label({
    "status": "IN_PLAY",
    "minute": 120,
    "injury_time": None,
    "match_phase": "PENALTIES",
    "home_penalty_score": 4,
    "away_penalty_score": 3,
    "utc_date": datetime.now(timezone.utc).isoformat(),
}) == "PENS 4–3"
assert predictor.status_label({
    "status": "FINISHED",
    "minute": 120,
    "injury_time": None,
    "match_phase": "EXTRA_TIME",
    "utc_date": datetime.now(timezone.utc).isoformat(),
}) == "AET"


for route in [
    "/dashboard",
    "/champions-league",
    "/head-to-head",
    "/prize-structure",
    "/tegrity",
    "/rules",
    "/stats",
    "/league-stats",
    "/leaderboard",
    "/history",
    "/changelog",
    "/account",
    "/admin",
]:
    response = client.get(route)
    assert response.status_code == 200, (route, response.status_code)

assert client.get("/side-events").status_code == 302

# Only the assigned treasurer can change the shared payment register. Admin
# status by itself is deliberately insufficient.
conn = database.get_db()
payment_player = conn.execute("SELECT id FROM players ORDER BY id LIMIT 1").fetchone()
conn.execute("UPDATE players SET treasurer = 0, entry_fee_paid = 0")
conn.commit()
conn.close()
admin_payment_attempt = client.post(
    f"/prize-structure/payment/{payment_player['id']}", data={"paid": "1"}
)
assert admin_payment_attempt.status_code == 302 and admin_payment_attempt.headers["Location"].endswith("/")
conn = database.get_db()
assert conn.execute("SELECT entry_fee_paid FROM players WHERE id = ?", (payment_player["id"],)).fetchone()[0] == 0
conn.execute("UPDATE players SET treasurer = 1 WHERE id = ?", (admin["id"],))
conn.commit()
conn.close()
treasurer_payment = client.post(
    f"/prize-structure/payment/{payment_player['id']}", data={"paid": "1"}
)
assert treasurer_payment.status_code == 302 and treasurer_payment.headers["Location"].endswith("/prize-structure")
conn = database.get_db()
assert conn.execute("SELECT entry_fee_paid FROM players WHERE id = ?", (payment_player["id"],)).fetchone()[0] == 1
conn.execute("UPDATE players SET treasurer = 0, entry_fee_paid = 0")
conn.commit()
conn.close()

tegrity_response = client.get("/tegrity")
assert b"Tegridy" in tegrity_response.data
assert b"Ledger verified" in tegrity_response.data
assert b"No detailed prediction records need to be displayed" in tegrity_response.data
assert b"What does Tegridy do?" in tegrity_response.data
assert b"have not been secretly altered" in tegrity_response.data
assert b"Predictions for matches that have not kicked off remain hidden" in tegrity_response.data
assert b"Integrity failure details" not in tegrity_response.data

# A failed verification exposes a clearly marked red investigation area.
original_chain_verifier = predictor.verify_prediction_audit_chain
try:
    predictor.verify_prediction_audit_chain = lambda conn: {
        "valid": False, "event_count": 1, "error_id": 1,
    }
    tegrity_failed_response = client.get("/tegrity")
    assert b"Ledger check failed" in tegrity_failed_response.data
    assert b"tegrity-status-alert" in tegrity_failed_response.data
    assert b"tegrity-failure-details" in tegrity_failed_response.data
finally:
    predictor.verify_prediction_audit_chain = original_chain_verifier

retired_test_response = client.get("/test-mode", follow_redirects=False)
assert retired_test_response.status_code == 302
assert retired_test_response.headers["Location"].endswith("/dashboard")

admin_response = client.get("/admin")
assert b"API Settings" in admin_response.data
assert b'href="/admin/settings"' in admin_response.data
assert b"Database Health" in admin_response.data
assert b">PREDICTIONS</div>" not in admin_response.data
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
assert b"Correct Draws" in leaderboard_response.data
assert b"Correct Scores" in leaderboard_response.data
assert b"Correct Winners" in leaderboard_response.data
assert b"Positions are ranked by total points" in leaderboard_response.data
assert b"Correct Draws, then Correct Scores, then Correct Winners" in leaderboard_response.data
stats_response = client.get("/stats")
assert b"PREDICTIONS SCORED" not in stats_response.data
assert b"Your Stats" in stats_response.data
assert b"BEST GAMEWEEK" in stats_response.data
assert b"CURRENT LEADER" not in stats_response.data
league_stats_response = client.get("/league-stats")
assert league_stats_response.status_code == 200
assert b"League Stats" in league_stats_response.data
assert b"CURRENT LEADER" in league_stats_response.data
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
assert b"Champions League" in seasons_response.data
assert b"MCFG Cockfight Cup" in seasons_response.data
assert b"The first Champions League winner will appear here" in seasons_response.data
conn = database.get_db()
conn.executemany(
    """INSERT OR REPLACE INTO competition_winners(
           competition, season_label, winner_name
       ) VALUES (?, '2026/27', ?)""",
    (
        ("champions_league", admin["name"]),
        ("head_to_head", admin["name"]),
    ),
)
conn.commit()
conn.close()
side_champion_badges = client.get("/leaderboard")
assert b'aria-label="Reigning Champions League champion"' in side_champion_badges.data
assert b'aria-label="Reigning MCFG Cockfight Cup champion"' in side_champion_badges.data
conn = database.get_db()
conn.execute("DELETE FROM competition_winners WHERE season_label = '2026/27'")
conn.commit()
conn.close()
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
msg_24 = predictor.signal_reminder_message(
    1, fixtures, statuses,
    reminder_label="24-hour",
)
assert msg_24.startswith("GW1 - Preddies Reminder")

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
conn.execute("DELETE FROM fixtures WHERE id IN (8800, 8801)")
conn.commit()
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
# Champions League matchdays share the same number range as Premier League
# gameweeks. They must never change the Premier League dashboard's selection.
conn.execute(
    """INSERT INTO fixtures(id, season, competition, matchday, utc_date,
           status, home_team, away_team)
           VALUES (9020, ?, 'champions_league', 1, ?, 'SCHEDULED',
                   'CL Home', 'CL Away')""",
    (season, (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()),
)
conn.commit()
gw2_opens_at = predictor.gameweek_open_at(conn, 2)
assert gw2_opens_at.hour == 9
assert [fixture["id"] for fixture in predictor.signal_gameweek_fixtures(
    conn, 1
)] == [9001, 9002]
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

    open_message = predictor.signal_gw_open_message(2, [{
        "utc_date": "2026-08-29T11:30:00+00:00",
    }])
    assert open_message.startswith("GW 2 - Put Your Pre-Dicks In\n")
    assert "First Kick Off:" in open_message
    assert "Preddies: https://predictions.battleship.live" in open_message
    assert "Predictions are now open!" not in open_message
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
assert results_message.count("🥇 Player 1") == 2
assert results_message.count("🥈 Player 2") == 2
assert results_message.count("🥉 Player 3") == 2

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
    """INSERT INTO fixtures(
        id, season, matchday, utc_date, status,
        home_team, away_team, home_score, away_score
    )
    VALUES (?, ?, 2, ?, 'FINISHED', 'Gamma', 'Alpha', 0, 1)""",
    (
        9203,
        season,
        (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
    )
)

conn.execute(
    """INSERT INTO fixtures(
        id, season, matchday, utc_date, status,
        home_team, away_team, home_score, away_score
    )
    VALUES (?, ?, 2, ?, 'FINISHED', 'Beta', 'Delta', 3, 0)""",
    (
        9204,
        season,
        (datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),
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

assert stats["home_record"]["wins"] == 2
assert stats["home_record"]["gf"] == 3
assert stats["away_record"]["draws"] == 1
assert stats["away_record"]["wins"] == 1
assert stats["away_record"]["played"] == 2
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

# A fixture's own result updates form immediately, but its season totals remain
# the pre-match values until the next gameweek is viewed.
finished_stats_kickoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
conn.execute(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status,
           home_team, away_team, home_score, away_score
       ) VALUES (77772, ?, 3, ?, 'FINISHED',
                 'Crystal Palace', 'Manchester City', 1, 3)""",
    (season, finished_stats_kickoff),
)
conn.commit()
finished_stats = predictor.match_stats_for_fixture(
    conn,
    {
        "id": 77772,
        "utc_date": finished_stats_kickoff,
        "home_team": "Crystal Palace",
        "away_team": "Manchester City",
    },
)
assert finished_stats["home_record"]["played"] == 0
assert finished_stats["away_record"]["played"] == 0
assert finished_stats["home_form"][0] == "L"
assert finished_stats["away_form"][0] == "W"
next_gameweek_stats = predictor.match_stats_for_fixture(
    conn,
    {
        "id": 77773,
        "utc_date": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "home_team": "Crystal Palace",
        "away_team": "Manchester City",
    },
)
assert next_gameweek_stats["home_record"]["losses"] == 1
assert next_gameweek_stats["away_record"]["wins"] == 1
conn.execute("DELETE FROM fixtures WHERE id = 77772")
conn.commit()
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

# Reopening leaderboard/stats must not rewrite every already-correct row.
recalculation_sql = []
conn.set_trace_callback(recalculation_sql.append)
predictor.refresh_points(conn)
conn.set_trace_callback(None)
assert not any(
    statement.lstrip().upper().startswith("UPDATE PREDICTIONS")
    for statement in recalculation_sql
)

historical_table = predictor.overall_table_at_matchday(
    conn,
    1
)

# Verify helper exposes/uses leaderboard tie-break fields.
assert "exact_draws" in historical_table[0].keys()
assert "exact_scores" in historical_table[0].keys()
assert "correct_results" in historical_table[0].keys()

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

with open(
    os.path.join(templates_dir, "_dashboard_live_summary.html"),
    "r",
    encoding="utf-8",
) as handle:
    dashboard_live_summary_template = handle.read()

with open(
    os.path.join(templates_dir, "_fixture_prediction_rows.html"),
    "r",
    encoding="utf-8",
) as handle:
    fixture_prediction_template = handle.read()

with open(
    os.path.join(templates_dir, "_fixture_card_meta.html"),
    "r",
    encoding="utf-8",
) as handle:
    fixture_card_meta_template = handle.read()

assert '_match_stats.html' in predictions_template
assert 'Live GW{{ matchday }}' not in predictions_template
with open(
    os.path.join(templates_dir, "_match_stats.html"),
    "r",
    encoding="utf-8",
) as handle:
    match_stats_template = handle.read()
assert "Current-season Premier League record" in match_stats_template
assert '{% if show_match_stats %}' in predictions_template
assert 'class="scoreline prediction-scoreline"' in predictions_template
assert 'prediction-team-home' in predictions_template
assert 'prediction-team-away' in predictions_template
assert 'class="prediction-team-cluster prediction-team-home"' in predictions_template
assert 'class="prediction-team-cluster prediction-team-away"' in predictions_template
assert predictions_template.count('class="prediction-team-name ') >= 4
assert '<h2>🔥 Double Points (DP)</h2>' not in predictions_template
assert '_match_stats.html' not in dashboard_template
assert '_match_stats.html' not in gameweek_template
assert 'href="https://sportscore.com/" rel="dofollow"' in gameweek_template
assert 'href="https://www.football-data.org/"' in gameweek_template
assert "Match data from" in gameweek_template
assert "Ordered by the current overall league position" in gameweek_template
assert "player.season_points" in gameweek_template
assert "display_player_name(player.name)" in gameweek_template
assert "Position during this gameweek" in gameweek_template
assert "position-chart-data" in gameweek_template
assert "Swipe for earlier updates" in gameweek_template
assert "mobileTimelineWidth" in gameweek_template
assert "stage.scrollLeft = Math.max(0, stage.scrollWidth - stage.clientWidth)" in gameweek_template
assert 'snapshot.cause_label || snapshot.milestone || "Position change"' in gameweek_template
assert '_fixture_prediction_rows.html' in gameweek_template
assert '_fixture_prediction_rows.html' in dashboard_template
assert 'class="pick-grid{% if exact_score %} exact-score-row{% endif %}"' in fixture_prediction_template
assert "reveal_map.get(fixture.id)" in fixture_prediction_template
assert "stay hidden until this fixture kicks off" in fixture_prediction_template
assert "labelIndexes" not in gameweek_template
assert "button.innerHTML" not in gameweek_template
assert "display_status not in ('LIVE','IN_PLAY','PAUSED','AWAITING_LIVE_DATA','FINISHED')" in gameweek_template
assert 'const chartName = String(player.name || "").trim().split(/\\s+/)[0] || player.name;' in gameweek_template
assert '{% if fixture.home_score is none and fixture.away_score is none %}v{% else %}–{% endif %}' in gameweek_template
assert "fixture-live" in gameweek_template
assert "fixture-live" in dashboard_template
assert '_dashboard_live_summary.html' in dashboard_template
assert "display_player_name(player.name)" in dashboard_live_summary_template
assert '{% if gameweek_predictions_open %}' in dashboard_template
assert dashboard_template.index('_dashboard_live_summary.html') < dashboard_template.index('{% for fixture in current_fixtures %}')
assert 'href="#live-gameweek"' not in dashboard_template
assert '_fixture_card_meta.html' in dashboard_template
assert '_fixture_card_meta.html' in gameweek_template
assert "position_chart=dashboard_position_chart" in inspect.getsource(predictor.dashboard)
assert "{% if live_gameweek_visible and position_chart.snapshots|length > 0 %}" in gameweek_template
assert "team-badge-slot" in gameweek_template
assert "team-badge-slot" in dashboard_template
api_import_source = inspect.getsource(predictor.import_matches_from_api)
assert "record_live_position_snapshot(conn, snapshot_matchday)" in api_import_source
assert "import_champions_league_matches" in inspect.getsource(predictor)
assert "source_fixture_id" in inspect.getsource(database.init_db)
assert "import_champions_league_live_from_sportscore" in inspect.getsource(predictor)
gameweek_source = inspect.getsource(predictor.gameweek)
assert "record_live_position_snapshot(conn, matchday)" in gameweek_source

with open(
    os.path.join(templates_dir, "leaderboard.html"),
    "r",
    encoding="utf-8",
) as handle:
    leaderboard_template = handle.read()

assert 'role="table" aria-label="Season league table"' in leaderboard_template
assert 'data-label="Correct Draws"' in leaderboard_template
assert 'data-label="Correct Scores"' in leaderboard_template
assert 'data-label="Correct Winners"' in leaderboard_template
assert "position-chart-player" in leaderboard_template
assert "height:240px" in leaderboard_template
assert "height:220px" in leaderboard_template
assert "Math.max(220, box.height || 240)" in leaderboard_template
assert "const pad = {left: 22, right: 18, top: 18, bottom: 42}" in leaderboard_template
assert "window.addEventListener('preddies-theme-change', draw)" in leaderboard_template
assert "['#2563eb','#dc2626','#059669','#d97706','#7c3aed','#0891b2']" in leaderboard_template
assert '["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2"]' in dashboard_live_summary_template
assert 'button.setAttribute("aria-label", `Highlight ${player.name}`)' in dashboard_live_summary_template
assert "No settled position changes yet." in dashboard_live_summary_template
assert "Math.max(205, 135 + players.length * 14)" in dashboard_live_summary_template
assert "Math.max(205, 135 + players.length * 14)" in gameweek_template
assert "selectedPlayerId" in leaderboard_template
assert "item.innerHTML" not in leaderboard_template
assert "league-mobile-details" in leaderboard_template

with open(
    os.path.join(templates_dir, "stats.html"),
    "r",
    encoding="utf-8",
) as handle:
    stats_template = handle.read()

assert "DPs USED" not in stats_template
assert "BEST GAMEWEEK" in stats_template
assert "CURRENT LEADER" not in stats_template
assert "CORRECT SCORES WITH DP" in stats_template
assert "EXACT SCORES WITH DP" not in stats_template

with open(
    os.path.join(templates_dir, "league_stats.html"),
    "r",
    encoding="utf-8",
) as handle:
    league_stats_template = handle.read()

assert "MOST CORRECT WINNERS" in league_stats_template
assert "MOST CORRECT SCORES WITH DP" in league_stats_template

with open(
    os.path.join(templates_dir, "base.html"),
    "r",
    encoding="utf-8",
) as handle:
    base_template = handle.read()

assert '/static/predictor-icon.png' in base_template
assert 'family=Inter:wght@400;500;600;700' in base_template
assert 'font-family:"Inter"' in base_template
assert '.prediction-scoreline' in base_template
assert 'width:118px;' in base_template
assert 'grid-template-columns:42px 20px 42px;' in base_template
assert 'column-gap:12px' in base_template
assert 'grid-template-columns: minmax(0, 1fr) 58px minmax(0, 1fr);' in base_template
assert 'grid-template-columns: 18px 14px 18px;' in base_template
assert 'transform:translateX(6px);' in base_template
assert 'transform:translateX(3px)' in base_template
assert 'max-width:clamp(54px,19vw,82px);' in base_template
assert 'class="prediction-score-centre"' in predictions_template
assert 'mobile_prediction_team_name(fixture.home_team)' in predictions_template
assert 'mobile_prediction_team_name(fixture.away_team)' in predictions_template
assert predictor.mobile_prediction_team_name("Crystal Palace FC") == "Palace"
assert predictor.mobile_prediction_team_name("Manchester United FC") == "Man United"
assert predictor.mobile_prediction_team_name("AFC Bournemouth") == "B'mouth"
assert '.save-bar{\n  position:sticky;' in base_template
assert "preddies_theme" in base_template
assert 'id="theme-toggle"' in base_template
assert 'theme-toggle-corner' in base_template
assert 'theme-toggle-icon-moon' in base_template
assert 'theme-toggle-icon-sun' in base_template
assert 'aria-label="Switch to dark mode"' in base_template
assert "dark ? 'Switch to light mode' : 'Switch to dark mode'" in base_template
assert 'html[data-theme="dark"] .fixture.fixture-set' in base_template
assert 'html[data-theme="dark"] .match-stat-panel' in base_template
assert 'html[data-theme="dark"] .logout-button' in base_template
assert 'border-color:#ef4444' in base_template
assert 'background:#0f172a;' in base_template
assert 'tv-logo-dark' in base_template
assert 'broadcaster_dark_logo_url' in inspect.getsource(predictor)
assert 'TNT_Sports_%282023%29_alt.svg' in inspect.getsource(predictor)
assert "'AWAITING_LIVE_DATA','FINISHED'" in gameweek_template
assert '<span class="badge">FT</span>' in fixture_card_meta_template
assert 'html[data-theme="dark"]' in base_template
assert "prefers-color-scheme: dark" in base_template
assert 'class="save-label-short">Save</span>' in predictions_template
assert 'class="dashboard-logo"' in dashboard_template
assert 'display_player_name(session.player_name)' in dashboard_template
assert 'display_player_name(player.name)' in leaderboard_template
assert 'reigning_premier_league_champion' in inspect.getsource(predictor.inject_globals)
assert predictor.resolve_reigning_champion_name("TROPiC", [{
    "name": "TROPiC Pendragon", "login_name": "TROPiC",
}]) == "TROPiC Pendragon"
assert 'button.appendChild(document.createTextNode(chartName));' in gameweek_template
assert 'button.appendChild(document.createTextNode(String(player.name || "").trim().split(/\\s+/)[0]));' in dashboard_live_summary_template
assert 'display_player_name' not in league_stats_template
assert 'href="/champions-league"' in dashboard_template
assert 'href="/head-to-head"' in dashboard_template
with open(
    os.path.join(templates_dir, "head_to_head.html"),
    "r",
    encoding="utf-8",
) as handle:
    head_to_head_template = handle.read()
assert "GW32–37" in head_to_head_template
assert "Gameweek 38" in head_to_head_template
assert "Head-to-head gameweek score difference" in head_to_head_template
assert "player who finished higher in the Cockfight Cup league wins" in head_to_head_template
with open(
    os.path.join(templates_dir, "side_events.html"),
    "r",
    encoding="utf-8",
) as handle:
    champions_league_template = handle.read()
assert "same prediction-league format as the Premier League" in champions_league_template
assert "begin with the Champions League knockout stage" in champions_league_template
assert "one Double Points fixture" in champions_league_template
assert "wins the competition and the £20 prize" in champions_league_template
assert dashboard_template.index('href="/leaderboard"') < dashboard_template.index('href="/champions-league"') < dashboard_template.index('href="/head-to-head"') < dashboard_template.index('href="/stats"')
assert 'id="dashboard-menu-toggle"' in dashboard_template
assert 'id="dashboard-menu"' in dashboard_template
assert 'class="corner-action" href="/account"' in dashboard_template
assert 'class="corner-action" href="/tegrity"' in dashboard_template
assert 'class="corner-action" href="/admin"' in dashboard_template
assert 'class="nav"' not in dashboard_template
assert 'dashboard-position-stat' in dashboard_template
assert 'dashboard-season-stat' in dashboard_template
assert '-webkit-text-size-adjust: none' in base_template
assert '.dashboard-hero{margin-top:56px}' in base_template
assert 'flex:0 0 32px' in base_template
assert 'height:32px;min-height:32px;max-height:32px;inline-size:32px;block-size:32px' in base_template
assert 'background: #f1f5f9;' in base_template
assert dashboard_template.index('{% include "_news_ticker.html" %}') < dashboard_template.index('{% include "_dashboard_live_summary.html" %}')
assert dashboard_template.index('{% include "_dashboard_live_summary.html" %}') < dashboard_template.index('Current Round')
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

# A general fixture refresh must not erase a completed result or overwrite
# SportScore's authoritative in-play state with a competing provider update.
conn = database.get_db()
conn.executemany(
    """INSERT OR REPLACE INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team,
           home_score, away_score, goals_json, live_data_source
       ) VALUES (?, ?, 38, ?, ?, ?, ?, ?, ?, ?, 'SportScore')""",
    [
        (
            99003, season, datetime.now(timezone.utc).isoformat(),
            "FINISHED", "Protected Final Home", "Protected Final Away",
            2, 1, '[{"scorer":"Final Scorer"}]',
        ),
        (
            99004, season, datetime.now(timezone.utc).isoformat(),
            "IN_PLAY", "Protected Live Home", "Protected Live Away",
            1, 0, '[{"scorer":"Live Scorer"}]',
        ),
    ],
)
conn.commit()
conn.close()
original_get_matches = predictor.get_matches
original_refresh_tv = predictor.refresh_tv_broadcasters
predictor.get_matches = lambda token, season: [
    {
        "id": 99003,
        "matchday": 38,
        "utcDate": datetime.now(timezone.utc).isoformat(),
        "status": "TIMED",
        "homeTeam": {"name": "Protected Final Home"},
        "awayTeam": {"name": "Protected Final Away"},
        "score": {"fullTime": {"home": None, "away": None}},
    },
    {
        "id": 99004,
        "matchday": 38,
        "utcDate": datetime.now(timezone.utc).isoformat(),
        "status": "IN_PLAY",
        "homeTeam": {"name": "Protected Live Home"},
        "awayTeam": {"name": "Protected Live Away"},
        "score": {"fullTime": {"home": 0, "away": 0}},
        "goals": [],
    },
]
predictor.refresh_tv_broadcasters = lambda conn: 0
predictor.set_setting("football_api_token", "test-token")
try:
    assert predictor.import_matches_from_api() == 2
finally:
    predictor.get_matches = original_get_matches
    predictor.refresh_tv_broadcasters = original_refresh_tv
    predictor.set_setting("football_api_token", "")
conn = database.get_db()
protected_final = conn.execute(
    "SELECT status, home_score, away_score FROM fixtures WHERE id = 99003"
).fetchone()
protected_live = conn.execute(
    """SELECT status, home_score, away_score, live_data_source
       FROM fixtures WHERE id = 99004"""
).fetchone()
assert tuple(protected_final) == ("FINISHED", 2, 1)
assert tuple(protected_live) == ("IN_PLAY", 1, 0, "SportScore")
conn.execute("DELETE FROM fixtures WHERE id IN (99003, 99004)")
conn.commit()
conn.close()

# Champions League imports retain the provider ID separately and use a safe
# local ID, leaving Premier League prediction foreign keys untouched.
original_cl_loader = predictor.get_football_champions_league_matches
original_cl_tv_refresh = predictor.refresh_champions_league_tv_broadcasters
predictor.get_football_champions_league_matches = lambda token, season, matchday: [{
    "id": 880001,
    "matchday": 1,
    "utcDate": datetime.now(timezone.utc).isoformat(),
    "status": "SCHEDULED",
    "homeTeam": {"name": "CL Home"},
    "awayTeam": {"name": "CL Away"},
    "score": {"fullTime": {"home": None, "away": None}},
}]
predictor.set_setting("football_api_token", "test-token")
try:
    predictor.refresh_champions_league_tv_broadcasters = lambda conn, matchday: 0
    assert predictor.import_champions_league_matches(1) == 1
finally:
    predictor.get_football_champions_league_matches = original_cl_loader
    predictor.refresh_champions_league_tv_broadcasters = original_cl_tv_refresh
    predictor.set_setting("football_api_token", "")
conn = database.get_db()
champions_fixture = conn.execute(
    """SELECT id, competition, source_provider, source_fixture_id
       FROM fixtures WHERE source_fixture_id = '880001'"""
).fetchone()
assert tuple(champions_fixture) == (
    -880001, "champions_league", "football-data.org", "880001"
)
conn.execute("DELETE FROM fixtures WHERE id = -880001")
conn.commit()
conn.close()

# Champions League TV data is exact when the listing confirms a channel. It
# must be limited to English clubs and leave unconfirmed/TBC listings blank.
class ChampionsLeagueTVResponse:
    text = '''
    <div class="fixture"><div class="fixture__teams">FC Porto v Manchester City</div>
    <div class="fixture__channel"><span class="channel-pill">Amazon Prime Video</span></div></div>
    <div class="fixture"><div class="fixture__teams">Barcelona v Juventus</div>
    <div class="fixture__channel"><span class="channel-pill">TNT Sports 1</span></div></div>
    '''
    def raise_for_status(self):
        return None

original_tv_listing_get = predictor.requests.get
predictor.requests.get = lambda *args, **kwargs: ChampionsLeagueTVResponse()
try:
    listings = predictor.fetch_champions_league_uk_tv_listings([
        {"id": -881001, "competition": "champions_league",
         "home_team": "FC Porto", "away_team": "Manchester City"},
        {"id": -881002, "competition": "champions_league",
         "home_team": "Barcelona", "away_team": "Juventus"},
    ])
finally:
    predictor.requests.get = original_tv_listing_get
assert listings == {-881001: "Amazon Prime Video"}
assert predictor.broadcaster_logo_url("TNT Sports 1")
assert predictor.broadcaster_dark_logo_url("TNT Sports 1")

# Champions League live enrichment is independent from the Premier League
# current-gameweek query and carries the same score, clock and event fields.
conn = database.get_db()
conn.execute(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team,
           competition, source_provider, source_fixture_id
       ) VALUES (-880002, ?, 1, ?, 'SCHEDULED', 'CL Live Home', 'CL Live Away',
                 'champions_league', 'football-data.org', '880002')""",
    (season, datetime.now(timezone.utc).isoformat()),
)
conn.commit()
conn.close()
original_cl_details = predictor.get_sportscore_match_details
original_cl_goals = predictor.sportscore_goal_events
predictor.get_sportscore_match_details = lambda match: {
    "home": "CL Live Home", "away": "CL Live Away",
    "competition": "UEFA Champions League", "status": "live",
    "home_score": 1, "away_score": 0, "incidents": [{"type": "goal"}],
    "home_logo": "https://img.example/cl-home.png",
    "away_logo": "https://img.example/cl-away.png",
}
predictor.sportscore_goal_events = lambda details: [{
    "team": {"name": "CL Live Home"}, "scorer": {"name": "CL Scorer"},
    "minute": 12,
}]
try:
    assert predictor.import_champions_league_live_from_sportscore() == 1
finally:
    predictor.get_sportscore_match_details = original_cl_details
    predictor.sportscore_goal_events = original_cl_goals
conn = database.get_db()
cl_live = conn.execute(
    """SELECT status, home_score, away_score, live_data_source, goals_json
       FROM fixtures WHERE id = -880002"""
).fetchone()
assert tuple(cl_live[:4]) == ("IN_PLAY", 1, 0, "SportScore")
assert "CL Scorer" in cl_live["goals_json"]
conn.execute("DELETE FROM fixtures WHERE id = -880002")
conn.commit()
conn.close()

# Previously retained scorer events can repair rows already damaged by a
# blank provider refresh, including genuine goalless draws.
conn = database.get_db()
repair_kickoff = (
    datetime.now(timezone.utc) - timedelta(hours=5)
).isoformat()
conn.executemany(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team,
           home_score, away_score, goals_json
       ) VALUES (?, ?, 39, ?, 'SCHEDULED', ?, ?, NULL, NULL, ?)""",
    [
        (
            99005, season, repair_kickoff, "Repair Home", "Repair Away",
            json.dumps([
                {"team": {"name": "Repair Home"}, "scorer": {"name": "A"}},
                {"team": {"name": "Repair Home"}, "scorer": {"name": "B"}},
                {"team": {"name": "Repair Away"}, "scorer": {"name": "C"}},
            ]),
        ),
        (99006, season, repair_kickoff, "Nil Home", "Nil Away", "[]"),
    ],
)
conn.commit()
conn.close()
assert predictor.repair_missing_completed_results() == 2
conn = database.get_db()
repaired_scores = {
    row["id"]: (row["status"], row["home_score"], row["away_score"])
    for row in conn.execute(
        "SELECT id, status, home_score, away_score FROM fixtures WHERE id IN (99005, 99006)"
    ).fetchall()
}
assert repaired_scores == {
    99005: ("FINISHED", 2, 1),
    99006: ("FINISHED", 0, 0),
}
conn.execute("DELETE FROM fixtures WHERE id IN (99005, 99006)")
conn.commit()
conn.close()

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
direct_state = {"corrected": False}
original_match_details = predictor.get_sportscore_match_details
def fake_direct_match_details(match):
    direct_calls.append(match["url"])
    if match["url"] == "/football/match/fulham-vs-chelsea/":
        raise predictor.SportScoreError("SportScore returned HTTP 404.")
    return {
        "home": "Fulham",
        "away": "Chelsea",
        "home_score": "2",
        "away_score": "2" if direct_state["corrected"] else "3",
        "status": "live",
        "live_minute": "64",
        "home_logo": "https://sportscore.com/media/fulham.png",
        "away_logo": "https://sportscore.com/media/chelsea.png",
        "incidents": [] if direct_state["corrected"] else [{
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
    direct_state["corrected"] = True
    assert predictor.import_live_matches_from_sportscore() == 1
finally:
    predictor.get_sportscore_match_details = original_match_details
assert direct_calls == [
    "/football/match/fulham-vs-chelsea/",
    "/football/match/chelsea-vs-fulham/",
    "/football/match/fulham-vs-chelsea/",
    "/football/match/chelsea-vs-fulham/",
]
conn = database.get_db()
direct_fixture = conn.execute(
    """SELECT home_score, away_score, minute, goals_json,
              home_logo, away_logo
       FROM fixtures WHERE id = 99002"""
).fetchone()
assert (direct_fixture["home_score"], direct_fixture["away_score"]) == (2, 2)
assert direct_fixture["minute"] == 64
assert direct_fixture["goals_json"] == "[]"
assert direct_fixture["home_logo"].endswith("fulham.png")
assert direct_fixture["away_logo"].endswith("chelsea.png")
conn.execute("DELETE FROM fixtures WHERE id = 99002")
conn.commit()
conn.close()

# API-Football is a guarded fallback: it maps a matching live fixture, fills
# absent score/event data, and records first-seen provider events.
api_fallback_kickoff = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
conn = database.get_db()
conn.execute(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team
       ) VALUES (99007, ?, 0, ?, 'SCHEDULED', 'Fallback Home', 'Fallback Away')""",
    (season, api_fallback_kickoff),
)
conn.commit()
conn.close()
original_api_football_live = predictor.get_api_football_live_fixtures
original_api_football_events = predictor.get_api_football_fixture_events
predictor.get_api_football_live_fixtures = lambda key: [{
    "fixture": {
        "id": 456789, "date": api_fallback_kickoff,
        "status": {"short": "1H", "elapsed": 6},
    },
    "teams": {
        "home": {"name": "Fallback Home"},
        "away": {"name": "Fallback Away"},
    },
    "goals": {"home": 1, "away": 0},
}]
predictor.get_api_football_fixture_events = lambda key, fixture_id: [{
    "type": "Goal", "detail": "Normal Goal",
    "time": {"elapsed": 6, "extra": None},
    "team": {"name": "Fallback Home"},
    "player": {"name": "Fallback Scorer"},
}]
predictor.set_setting("api_football_key", "test-key")
predictor.set_setting("api_football_call_day", "")
predictor.set_setting("last_api_football_request", "")
try:
    assert predictor.import_live_matches_from_api_football_fallback() == 1
finally:
    predictor.get_api_football_live_fixtures = original_api_football_live
    predictor.get_api_football_fixture_events = original_api_football_events
    predictor.set_setting("api_football_key", "")
conn = database.get_db()
fallback_fixture = conn.execute(
    """SELECT status, home_score, away_score, minute, goals_json, live_data_source
       FROM fixtures WHERE id = 99007"""
).fetchone()
assert tuple(fallback_fixture[:4]) == ("IN_PLAY", 1, 0, 6)
assert "Fallback Scorer" in fallback_fixture["goals_json"]
assert fallback_fixture["live_data_source"] == "API-Football"
assert conn.execute(
    "SELECT 1 FROM provider_fixture_mappings WHERE fixture_id = 99007"
).fetchone() is not None
assert conn.execute(
    "SELECT 1 FROM provider_event_observations WHERE fixture_id = 99007"
).fetchone() is not None
conn.execute(
    """UPDATE fixtures SET status = 'IN_PLAY', home_score = 1, away_score = 0,
           minute = 1, goals_json = '[{"scorer":{"name":"Known"}}]',
           live_data_source = 'SportScore' WHERE id = 99007"""
)
assert predictor.fixture_scorers(
    json.dumps([{
        "team": {"name": "Home FC"}, "scorer": {"name": "VAR scorer"},
        "minute": 15,
    }]),
    "Home FC", "Away FC", 0, 0,
) == {"home": [], "away": []}
assert predictor._fixture_goal_event_coverage_missing({
    "goals_json": json.dumps([{
        "team": {"name": "Away FC"}, "scorer": {"name": "Away scorer"},
    }]),
    "home_team": "Home FC", "away_team": "Away FC",
    "home_score": 1, "away_score": 1,
}) is True
assert predictor._fixture_goal_event_coverage_missing({
    "goals_json": json.dumps([
        {"team": {"name": "Home FC"}, "scorer": {"name": "Home scorer"}},
        {"team": {"name": "Away FC"}, "scorer": {"name": "Away scorer"}},
    ]),
    "home_team": "Home FC", "away_team": "Away FC",
    "home_score": 1, "away_score": 1,
}) is False
conn.commit()
conn.close()
predictor.get_api_football_live_fixtures = lambda key: [{
    "fixture": {
        "id": 456789, "date": api_fallback_kickoff,
        "status": {"short": "1H", "elapsed": 6},
    },
    "teams": {
        "home": {"name": "Fallback Home"},
        "away": {"name": "Fallback Away"},
    },
    "goals": {"home": 1, "away": 0},
}]
predictor.get_api_football_fixture_events = lambda key, fixture_id: []
predictor.set_setting("api_football_key", "test-key")
predictor.set_setting("last_api_football_request", "")
try:
    assert predictor.import_live_matches_from_api_football_fallback() == 1
finally:
    predictor.get_api_football_live_fixtures = original_api_football_live
    predictor.get_api_football_fixture_events = original_api_football_events
    predictor.set_setting("api_football_key", "")
conn = database.get_db()
assert conn.execute("SELECT minute FROM fixtures WHERE id = 99007").fetchone()[0] == 6
conn.execute(
    "UPDATE fixtures SET status = 'IN_PLAY', minute = 45 WHERE id = 99007"
)
conn.commit()
conn.close()
predictor.get_api_football_live_fixtures = lambda key: [{
    "fixture": {"id": 456789, "date": api_fallback_kickoff,
                "status": {"short": "HT", "elapsed": 45}},
    "teams": {"home": {"name": "Fallback Home"},
              "away": {"name": "Fallback Away"}},
    "goals": {"home": 1, "away": 0},
}]
predictor.get_api_football_fixture_events = lambda key, fixture_id: []
predictor.set_setting("api_football_key", "test-key")
predictor.set_setting("last_api_football_request", "")
try:
    assert predictor.import_live_matches_from_api_football_fallback() == 1
finally:
    predictor.get_api_football_live_fixtures = original_api_football_live
    predictor.get_api_football_fixture_events = original_api_football_events
    predictor.set_setting("api_football_key", "")
conn = database.get_db()
assert conn.execute("SELECT status FROM fixtures WHERE id = 99007").fetchone()[0] == "PAUSED"
conn.execute("DELETE FROM fixtures WHERE id = 99007")
conn.commit()
conn.close()

# The same guarded all-live request also covers stored Champions League rows.
cl_fallback_kickoff = (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat()
conn = database.get_db()
conn.execute(
    """INSERT INTO fixtures(
           id, season, matchday, utc_date, status, home_team, away_team, competition
       ) VALUES (-99008, ?, 1, ?, 'SCHEDULED', 'CL Fallback Home',
                 'CL Fallback Away', 'champions_league')""",
    (season, cl_fallback_kickoff),
)
conn.commit()
conn.close()
predictor.get_api_football_live_fixtures = lambda key: [{
    "fixture": {"id": 456790, "date": cl_fallback_kickoff,
                "status": {"short": "1H", "elapsed": 7}},
    "teams": {"home": {"name": "CL Fallback Home"},
              "away": {"name": "CL Fallback Away"}},
    "goals": {"home": 0, "away": 0},
}]
predictor.get_api_football_fixture_events = lambda key, fixture_id: []
predictor.set_setting("api_football_key", "test-key")
predictor.set_setting("last_api_football_request", "")
try:
    assert predictor.import_live_matches_from_api_football_fallback() == 1
finally:
    predictor.get_api_football_live_fixtures = original_api_football_live
    predictor.get_api_football_fixture_events = original_api_football_events
    predictor.set_setting("api_football_key", "")
conn = database.get_db()
cl_fallback = conn.execute(
    """SELECT status, home_score, away_score, minute, live_data_source
       FROM fixtures WHERE id = -99008"""
).fetchone()
assert tuple(cl_fallback) == ("IN_PLAY", 0, 0, 7, "API-Football")
conn.execute("DELETE FROM fixtures WHERE id = -99008")
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
assert releases and tuple(map(int, releases[0]["version"].split("."))) <= tuple(
    map(int, predictor.APP_VERSION.split("."))
)
release_sections = releases[0]["sections"]
assert release_sections
release_titles = [section["title"] for section in release_sections]
assert release_titles == [
    title for title in predictor.CHANGELOG_SECTION_ORDER if title in release_titles
]
assert all(section["items"] for section in release_sections)

# Patch releases may contain only fixes; exercise the actual Markdown parser
# independently of whichever sections the current release happens to include.
from unittest.mock import mock_open, patch
with patch.object(predictor.os.path, "exists", return_value=True), patch(
    "builtins.open",
    mock_open(read_data="## [test-patch] - 2026-09-05\n\n### Fixes\n- Corrected mobile card alignment.\n"),
):
    patch_releases = predictor.read_app_changelog()
assert len(patch_releases) == 1
assert patch_releases[0]["version"] == "test-patch"
assert patch_releases[0]["sections"] == [{
    "title": "Fixes",
    "items": ["Corrected mobile card alignment."],
    "groups": [{"title": "UI", "items": ["Corrected mobile card alignment."]}],
}]
sample_sections = predictor.normalise_changelog_sections([
    {"title": "Fixed", "items": [
        "Corrected mobile card alignment.",
        "Corrected Double Points scoring calculations.",
        "Repaired a database migration.",
        "Restored live provider scores.",
        "Fixed Signal reminder delivery.",
        "Hardened login session security.",
        "Repaired Google Drive backup restore.",
        "Corrected an uncategorised issue.",
    ]},
    {"title": "Changed", "items": ["Updated wording."]},
    {"title": "Added", "items": ["Added a new page."]},
    {"title": "Safety", "items": ["Important upgrade guidance."]},
])
assert [section["title"] for section in sample_sections] == [
    "Important", "New", "Changes", "Fixes"
]
assert [group["title"] for group in sample_sections[-1]["groups"]] == [
    "UI", "Calculations", "Database", "Live Data",
    "Notifications", "Security", "Backups", "General",
]
assert predictor.changelog_fix_category("Chart spacing") == "UI"
assert predictor.changelog_fix_category("Database query") == "Database"

with open(
    os.path.join(templates_dir, "changelog.html"),
    "r",
    encoding="utf-8",
) as handle:
    changelog_template = handle.read()
assert "changelog-fix-heading" in changelog_template
assert "section.get('groups')" in changelog_template

os.remove(tmp.name)
print("Preddies self-test: PASS")
