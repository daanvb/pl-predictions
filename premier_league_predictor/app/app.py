from flask import Flask, request, redirect, session, render_template, flash, send_file, Response
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import re
import secrets
import threading
import time
import sqlite3
import requests
import json
import csv
import io
import zlib
from urllib.parse import urljoin
from html.parser import HTMLParser
from html import unescape

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from database import init_db, get_db, hash_pin, get_setting, set_setting, DB
from database_restore import (
    database_has_users,
    install_database,
    validate_predictor_database,
)
from football_api import test_connection, get_match, get_matches, FootballAPIError
from sportscore import (
    get_live_matches as get_sportscore_live_matches,
    get_team_matches as get_sportscore_team_matches,
    get_match_details as get_sportscore_match_details,
    goal_events as sportscore_goal_events,
)
from scoring import calculate_points, calculate_prediction_points

APP_VERSION = "1.0.15"
SEASON = 2026
UK = ZoneInfo("Europe/London")

DATA_DIR = os.path.dirname(DB) or "/data"
SECRET_FILE = os.path.join(DATA_DIR, "secret.key")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
GOOGLE_TOKEN_FILE = os.path.join(DATA_DIR, "google_drive_token.json")
FIRST_RUN_TOKEN_FILE = os.path.join(DATA_DIR, "first_run_restore.token")
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GOOGLE_BACKUP_FOLDER = "Premier League Predictor Backups"
GOOGLE_RETENTION_DAYS = 30

QUIET_REFRESH_SECONDS = 6 * 60 * 60
# SportScore caches its live feed for 60 seconds, so polling more often would
# add load without producing fresher data.
LIVE_REFRESH_SECONDS = 60
FINAL_SCORER_BACKFILL_PER_REFRESH = 8
LIVE_WINDOW_BEFORE_SECONDS = 20 * 60
LIVE_WINDOW_AFTER_SECONDS = 3 * 60 * 60
MIN_REFRESH_SLEEP_SECONDS = 60

AUTO_BACKUP_SECONDS = 6 * 60 * 60
MAX_AUTO_BACKUPS = 5

SIGNAL_NOTIFICATION_CHECK_SECONDS = 15 * 60
SIGNAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF = 24
SIGNAL_FINAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF = 2

PREMIER_LEAGUE_FIXTURE_SOURCE = (
    "https://www.premierleague.com/en/news/4675097/"
    "all-380-fixtures-for-202627-premier-league-season"
)

SKY_SPORTS_LOGO = (
    "https://upload.wikimedia.org/wikipedia/commons/9/9d/"
    "Sky_Sports_2026.svg"
)

TNT_SPORTS_LOGO = (
    "https://upload.wikimedia.org/wikipedia/commons/8/83/"
    "TNT_Sports_%282023%29.svg"
)

app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024

# The Predictor is served publicly through an HTTPS reverse proxy.
# Trust one proxy hop for the original scheme and host.
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1
)

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

if os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "r") as f:
        app.secret_key = f.read().strip()
else:
    secret = secrets.token_hex(32)

    with open(SECRET_FILE, "w") as f:
        f.write(secret)

    app.secret_key = secret

init_db(seed_default_player=False)
database_restore_lock = threading.Lock()


def ensure_first_run_restore_token():
    if database_has_users(DB):
        if os.path.exists(FIRST_RUN_TOKEN_FILE):
            os.remove(FIRST_RUN_TOKEN_FILE)
        return None

    if os.path.exists(FIRST_RUN_TOKEN_FILE):
        with open(FIRST_RUN_TOKEN_FILE, "r") as handle:
            return handle.read().strip()

    token = secrets.token_urlsafe(18)
    try:
        with open(FIRST_RUN_TOKEN_FILE, "x") as handle:
            handle.write(token)
    except FileExistsError:
        with open(FIRST_RUN_TOKEN_FILE, "r") as handle:
            return handle.read().strip()
    try:
        os.chmod(FIRST_RUN_TOKEN_FILE, 0o600)
    except OSError:
        pass
    app.logger.warning(
        "FIRST-RUN RESTORE CODE: %s (enter this code in the restore screen)",
        token,
    )
    return token


ensure_first_run_restore_token()


def logged_in():
    return "player_id" in session


def is_admin():
    return bool(session.get("admin"))


def changelog_seen_key(player_id):
    return f"changelog_seen_version_{player_id}"


def changelog_has_unread_update():
    if not logged_in():
        return False

    seen_version = get_setting(
        changelog_seen_key(
            session["player_id"]
        )
    )

    return seen_version != APP_VERSION


def mark_changelog_seen():
    if not logged_in():
        return

    set_setting(
        changelog_seen_key(
            session["player_id"]
        ),
        APP_VERSION
    )


@app.before_request
def retire_test_mode_routes():
    if request.path == "/test-mode" or "test-mode" in request.path:
        flash("Test Mode has been retired.", "success")
        return redirect("/dashboard" if logged_in() else "/")


def now_utc():
    return datetime.now(timezone.utc)


def parse_utc(value):
    if not value:
        return None

    value = value.replace("Z", "+00:00")

    return datetime.fromisoformat(
        value
    ).astimezone(timezone.utc)


def kickoff_passed(utc_date):
    """Return True from the fixture's exact scheduled kickoff onward."""
    kickoff = parse_utc(utc_date)
    if not kickoff:
        return False
    return now_utc() >= kickoff


def fixture_is_locked(fixture):
    return kickoff_passed(fixture["utc_date"])


def goal_minute_label(goal):
    minute = goal.get("minute")
    if minute is None:
        return ""
    injury_time = goal.get("injuryTime")
    if injury_time:
        return f"{minute}+{injury_time}'"
    return f"{minute}'"


def fixture_scorers(goals_json, home_team, away_team):
    try:
        goals = json.loads(goals_json or "[]")
    except (TypeError, ValueError):
        goals = []

    grouped = {"home": [], "away": []}
    scorer_index = {"home": {}, "away": {}}

    for goal in goals:
        scorer = (goal.get("scorer") or {}).get("name")
        team = (goal.get("team") or {}).get("name")
        if not scorer or not team:
            continue

        if team.casefold() == home_team.casefold():
            side = "home"
        elif team.casefold() == away_team.casefold():
            side = "away"
        else:
            continue

        goal_type = (goal.get("type") or "").upper()
        marker = goal_minute_label(goal)
        if goal_type == "PENALTY":
            marker = f"{marker} pen".strip()
        elif goal_type in ("OWN", "OWN_GOAL"):
            marker = f"{marker} og".strip()

        key = scorer
        entry = scorer_index[side].get(key)
        if entry is None:
            entry = {"name": scorer, "goals": []}
            scorer_index[side][key] = entry
            grouped[side].append(entry)
        if marker:
            entry["goals"].append(marker)

    return grouped



def ranking_positions(rows):
    return {
        row["id"]: index
        for index, row in enumerate(
            rows,
            start=1
        )
    }


def season_label(season):
    return f"{season}/{str(season + 1)[-2:]}"


def archive_completed_season(conn, season):
    """Store one immutable final table once all 380 league games complete."""
    existing = conn.execute(
        "SELECT stats_available FROM season_archives WHERE season = ?",
        (season,),
    ).fetchone()
    if existing and existing["stats_available"]:
        return False

    fixture_status = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status IN ('FINISHED', 'CANCELLED')
                        THEN 1 ELSE 0 END) AS complete
        FROM fixtures
        WHERE season = ?
        """,
        (season,),
    ).fetchone()
    if (
        not fixture_status
        or fixture_status["total"] < 380
        or fixture_status["complete"] != fixture_status["total"]
    ):
        return False

    rows = conn.execute(
        """
        SELECT
            pl.name,
            COALESCE(SUM(CASE WHEN f.season = ? THEN p.points ELSE 0 END), 0)
                AS points,
            COALESCE(SUM(CASE
                WHEN f.season = ? AND f.status = 'FINISHED'
                 AND p.home_score = f.home_score
                 AND p.away_score = f.away_score
                 AND f.home_score = f.away_score
                THEN 1 ELSE 0 END), 0) AS exact_draws,
            COALESCE(SUM(CASE
                WHEN f.season = ? AND f.status = 'FINISHED'
                 AND p.home_score = f.home_score
                 AND p.away_score = f.away_score
                 AND f.home_score != f.away_score
                THEN 1 ELSE 0 END), 0) AS exact_scores,
            COALESCE(SUM(CASE
                WHEN f.season = ? AND f.status = 'FINISHED'
                 AND NOT (p.home_score = f.home_score AND p.away_score = f.away_score)
                 AND (
                    (f.home_score = f.away_score AND p.home_score = p.away_score)
                    OR (f.home_score > f.away_score AND p.home_score > p.away_score)
                    OR (f.home_score < f.away_score AND p.home_score < p.away_score)
                 )
                THEN 1 ELSE 0 END), 0) AS correct_results,
            COALESCE(SUM(CASE
                WHEN f.season = ? AND f.status = 'FINISHED'
                 AND COALESCE(p.dp, 0) = 1
                 AND p.home_score = f.home_score
                 AND p.away_score = f.away_score
                THEN 1 ELSE 0 END), 0) AS dp_exact_scores
        FROM players pl
        LEFT JOIN predictions p ON p.player_id = pl.id
        LEFT JOIN fixtures f ON f.id = p.fixture_id
        GROUP BY pl.id
        ORDER BY points DESC, exact_draws DESC, exact_scores DESC,
                 pl.name COLLATE NOCASE
        """,
        (season, season, season, season, season),
    ).fetchall()
    if not rows:
        return False

    conn.execute(
        """
        INSERT INTO season_archives
            (season, label, winner_name, archived_at, stats_available)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(season) DO UPDATE SET
            label = excluded.label,
            winner_name = excluded.winner_name,
            archived_at = excluded.archived_at,
            stats_available = 1
        """,
        (season, season_label(season), rows[0]["name"], now_utc().isoformat()),
    )
    conn.execute(
        "DELETE FROM season_archive_players WHERE season = ?",
        (season,),
    )
    conn.executemany(
        """
        INSERT INTO season_archive_players
            (season, position, player_name, points, exact_draws, exact_scores,
             correct_results, dp_exact_scores)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                season, position, row["name"], row["points"],
                row["exact_draws"], row["exact_scores"],
                row["correct_results"], row["dp_exact_scores"],
            )
            for position, row in enumerate(rows, start=1)
        ],
    )
    return True


def overall_table_at_matchday(
    conn,
    matchday
):
    """
    Reconstruct the season table at a completed-Gameweek boundary using the
    same ordering rules as the visible Season Leaderboard.

    This matters for positional arrows: if players are level on points, the
    baseline must use exact draws / exact winning scores before player name,
    otherwise an arrow can imply movement that never actually occurred.
    """
    if matchday is None or matchday < 1:
        return conn.execute(
            """
            SELECT
                id,
                name,
                0 AS points,
                0 AS exact_draws,
                0 AS exact_scores
            FROM players
            ORDER BY name COLLATE NOCASE
            """
        ).fetchall()

    return conn.execute(
        """
        SELECT
            pl.id,
            pl.name,

            COALESCE(
                SUM(
                    CASE
                    WHEN f.status = 'FINISHED'
                     AND f.matchday <= ?
                    THEN p.points
                    ELSE 0
                    END
                ),
                0
            ) AS points,

            COALESCE(
                SUM(
                    CASE
                    WHEN f.status = 'FINISHED'
                     AND f.matchday <= ?
                     AND p.home_score = f.home_score
                     AND p.away_score = f.away_score
                     AND f.home_score = f.away_score
                    THEN 1
                    ELSE 0
                    END
                ),
                0
            ) AS exact_draws,

            COALESCE(
                SUM(
                    CASE
                    WHEN f.status = 'FINISHED'
                     AND f.matchday <= ?
                     AND p.home_score = f.home_score
                     AND p.away_score = f.away_score
                     AND f.home_score != f.away_score
                    THEN 1
                    ELSE 0
                    END
                ),
                0
            ) AS exact_scores

        FROM players pl

        LEFT JOIN predictions p
          ON p.player_id = pl.id

        LEFT JOIN fixtures f
          ON f.id = p.fixture_id
         AND f.season = ?

        GROUP BY pl.id

        ORDER BY
            points DESC,
            exact_draws DESC,
            exact_scores DESC,
            pl.name COLLATE NOCASE
        """,
        (
            matchday,
            matchday,
            matchday,
            SEASON
        ),
    ).fetchall()


def table_position_change(
    current_position,
    previous_position
):
    if (
        current_position is None
        or previous_position is None
    ):
        return 0

    return (
        previous_position
        - current_position
    )


def local_datetime(utc_date):
    dt = parse_utc(utc_date)

    if not dt:
        return ""

    return dt.astimezone(
        UK
    ).strftime(
        "%a %d %b, %H:%M"
    )


def local_timestamp(value):
    dt = parse_utc(value)

    if not dt:
        return ""

    return dt.astimezone(
        UK
    ).strftime(
        "%d %b %Y %H:%M"
    )



def fixture_display_status(fixture):
    """
    UI-only fallback when the provider is late changing a scheduled match.

    Never writes inferred state to the DB. It simply prevents the interface
    from continuing to say "Upcoming" after the stored kickoff has passed.
    """
    status = fixture["status"]

    if status not in (
        "SCHEDULED",
        "TIMED",
    ):
        return status

    kickoff = parse_utc(
        fixture["utc_date"]
    )

    if not kickoff:
        return status

    now = now_utc()

    if (
        kickoff <= now
        <= kickoff + timedelta(
            seconds=LIVE_WINDOW_AFTER_SECONDS
        )
    ):
        return "AWAITING_LIVE_DATA"

    if (
        now
        > kickoff + timedelta(
            seconds=LIVE_WINDOW_AFTER_SECONDS
        )
    ):
        return "AWAITING_RESULT"

    return status


def status_label(fixture):
    status = fixture_display_status(
        fixture
    )

    if status in ("LIVE", "IN_PLAY"):
        minute = fixture["minute"]

        if minute:
            return f"LIVE {minute}'"

        return "LIVE"

    if status == "PAUSED":
        return "HT"

    if status == "FINISHED":
        return "FT"

    if status == "POSTPONED":
        return "POSTPONED"

    if status == "SUSPENDED":
        return "SUSPENDED"

    if status == "AWAITING_LIVE_DATA":
        return "LIVE · awaiting score"

    if status == "AWAITING_RESULT":
        return "Awaiting result"

    return local_datetime(
        fixture["utc_date"]
    )



class _TVTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data and data.strip():
            self.parts.append(data.strip())

    def text(self):
        return " ".join(self.parts)


TEAM_TV_ALIASES = {
    "afc bournemouth": ["AFC Bournemouth", "Bournemouth"],
    "arsenal": ["Arsenal"],
    "aston villa": ["Aston Villa"],
    "brentford": ["Brentford"],
    "brighton & hove albion": ["Brighton & Hove Albion", "Brighton"],
    "chelsea": ["Chelsea"],
    "coventry city": ["Coventry City"],
    "crystal palace": ["Crystal Palace"],
    "everton": ["Everton"],
    "fulham": ["Fulham"],
    "hull city": ["Hull City"],
    "ipswich town": ["Ipswich Town"],
    "leeds united": ["Leeds United", "Leeds"],
    "liverpool": ["Liverpool"],
    "manchester city": ["Manchester City", "Man City"],
    "manchester united": ["Manchester United", "Man Utd"],
    "newcastle united": ["Newcastle United", "Newcastle"],
    "nottingham forest": ["Nottingham Forest", "Nott'm Forest", "Nott’m Forest"],
    "sunderland": ["Sunderland"],
    "tottenham hotspur": ["Tottenham Hotspur", "Spurs", "Tottenham"],
    "wolverhampton wanderers": ["Wolverhampton Wanderers", "Wolves"],
}


def _normalise_team_for_tv(name):
    value = (name or "").strip()
    value = re.sub(r"\s+FC$", "", value, flags=re.I)
    value = value.replace("A.F.C.", "AFC")
    return value.strip().lower()


def _team_tv_aliases(name):
    key = _normalise_team_for_tv(name)
    aliases = TEAM_TV_ALIASES.get(key)

    if aliases:
        return aliases

    cleaned = re.sub(r"\s+FC$", "", (name or "").strip(), flags=re.I)
    return [cleaned]


def infer_uk_broadcaster_from_slot(utc_date):
    """
    Conservative UK fallback based on rights packages / normal weekend slots.
    Unknown midweek or unusual slots are left blank rather than guessed.
    """
    kickoff = parse_utc(utc_date)

    if not kickoff:
        return None

    local = kickoff.astimezone(UK)
    weekday = local.weekday()  # Mon=0 ... Sun=6
    hm = (local.hour, local.minute)

    # TNT holds the Saturday 12:30 package.
    if weekday == 5 and hm == (12, 30):
        return "TNT Sports"

    # Saturday 15:00 is subject to the UK blackout.
    if weekday == 5 and hm == (15, 0):
        return None

    # Normal Sky weekend windows. Unusual Saturday evening / midweek slots
    # are deliberately left for the official listing to avoid a bad guess.
    if weekday in (4, 6, 0):  # Friday, Sunday, Monday
        return "Sky Sports"

    if weekday == 5 and hm in ((17, 30),):
        return "Sky Sports"

    return None


def fetch_official_uk_tv_listings(fixtures):
    """
    Match fixtures to the Premier League's official 2026/27 fixture article.
    Returns {fixture_id: broadcaster}. Failure is non-fatal.
    """
    try:
        response = requests.get(
            PREMIER_LEAGUE_FIXTURE_SOURCE,
            timeout=20,
            headers={
                "User-Agent": "PremierLeaguePredictor/1.17"
            },
        )
        response.raise_for_status()
    except Exception as exc:
        print(
            f"[tv-listings] Official listing unavailable: {exc}",
            flush=True
        )
        return {}

    parser = _TVTextParser()

    try:
        parser.feed(response.text)
    except Exception as exc:
        print(
            f"[tv-listings] HTML parse failed: {exc}",
            flush=True
        )
        return {}

    text = unescape(parser.text())
    text = re.sub(r"\s+", " ", text)
    found = {}

    for fixture in fixtures:
        home_aliases = _team_tv_aliases(fixture["home_team"])
        away_aliases = _team_tv_aliases(fixture["away_team"])
        broadcaster = None

        for home in home_aliases:
            if broadcaster:
                break

            for away in away_aliases:
                pattern = (
                    re.escape(home)
                    + r"\s+v\s+"
                    + re.escape(away)
                    + r"\s+\((Sky Sports|TNT Sports)\)"
                )

                match = re.search(
                    pattern,
                    text,
                    flags=re.I
                )

                if match:
                    broadcaster = match.group(1)
                    break

        if broadcaster:
            found[fixture["id"]] = broadcaster

    return found


def refresh_tv_broadcasters(conn):
    fixtures = conn.execute(
        """
        SELECT id, home_team, away_team, utc_date, broadcaster
        FROM fixtures
        WHERE season = ?
        ORDER BY utc_date
        """,
        (SEASON,),
    ).fetchall()

    official = fetch_official_uk_tv_listings(fixtures)
    updated = 0

    for fixture in fixtures:
        broadcaster = official.get(
            fixture["id"]
        )

        if not broadcaster:
            broadcaster = infer_uk_broadcaster_from_slot(
                fixture["utc_date"]
            )

        # Only write a value if we know one. This avoids erasing a previously
        # confirmed broadcaster when the official page is temporarily unavailable.
        if broadcaster and broadcaster != fixture["broadcaster"]:
            conn.execute(
                """
                UPDATE fixtures
                SET broadcaster = ?
                WHERE id = ?
                """,
                (
                    broadcaster,
                    fixture["id"]
                )
            )
            updated += 1

    set_setting(
        "last_tv_refresh",
        now_utc().isoformat()
    )

    return updated


def broadcaster_logo_url(broadcaster):
    if broadcaster == "Sky Sports":
        return SKY_SPORTS_LOGO

    if broadcaster == "TNT Sports":
        return TNT_SPORTS_LOGO

    return None


def refresh_points(conn):
    predictions = conn.execute(
        """
        SELECT
            p.id,
            p.home_score AS predicted_home,
            p.away_score AS predicted_away,
            COALESCE(p.dp, 0) AS dp,
            f.home_score AS actual_home,
            f.away_score AS actual_away,
            f.status
        FROM predictions p
        JOIN fixtures f
          ON f.id = p.fixture_id
        WHERE f.home_score IS NOT NULL
          AND f.away_score IS NOT NULL
        """
    ).fetchall()

    for prediction in predictions:
        points = 0

        if prediction["status"] == "FINISHED":
            points = calculate_prediction_points(
                prediction["predicted_home"],
                prediction["predicted_away"],
                prediction["actual_home"],
                prediction["actual_away"],
                bool(prediction["dp"]),
            )

        conn.execute(
            """
            UPDATE predictions
            SET points = ?
            WHERE id = ?
            """,
            (
                points,
                prediction["id"]
            )
        )




def canonical_team_name(name):
    value = (
        (name or "")
        .strip()
        .lower()
        .replace("&", "and")
        .replace("’", "'")
    )

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value
    ).strip()

    aliases = {
        "afc bournemouth": "bournemouth",
        "bournemouth": "bournemouth",
        "arsenal fc": "arsenal",
        "arsenal": "arsenal",
        "aston villa fc": "aston villa",
        "aston villa": "aston villa",
        "brighton and hove albion fc": "brighton",
        "brighton and hove albion": "brighton",
        "brighton": "brighton",
        "burnley fc": "burnley",
        "burnley": "burnley",
        "chelsea fc": "chelsea",
        "chelsea": "chelsea",
        "crystal palace fc": "crystal palace",
        "crystal palace": "crystal palace",
        "everton fc": "everton",
        "everton": "everton",
        "fulham fc": "fulham",
        "fulham": "fulham",
        "leeds united fc": "leeds",
        "leeds united": "leeds",
        "leeds": "leeds",
        "liverpool fc": "liverpool",
        "liverpool": "liverpool",
        "manchester city fc": "manchester city",
        "manchester city": "manchester city",
        "man city": "manchester city",
        "manchester united fc": "manchester united",
        "manchester united": "manchester united",
        "man united": "manchester united",
        "newcastle united fc": "newcastle",
        "newcastle united": "newcastle",
        "newcastle": "newcastle",
        "nottingham forest fc": "nottingham forest",
        "nottingham forest": "nottingham forest",
        "nott m forest": "nottingham forest",
        "nottm forest": "nottingham forest",
        "sunderland afc": "sunderland",
        "sunderland": "sunderland",
        "tottenham hotspur fc": "tottenham",
        "tottenham hotspur": "tottenham",
        "tottenham": "tottenham",
        "spurs": "tottenham",
        "west ham united fc": "west ham",
        "west ham united": "west ham",
        "west ham": "west ham",
        "wolverhampton wanderers fc": "wolves",
        "wolverhampton wanderers": "wolves",
        "wolves": "wolves",
        "brentford fc": "brentford",
        "brentford": "brentford",
        "leicester city fc": "leicester",
        "leicester city": "leicester",
        "leicester": "leicester",
        "ipswich town fc": "ipswich",
        "ipswich town": "ipswich",
        "ipswich": "ipswich",
        "southampton fc": "southampton",
        "southampton": "southampton",
        "luton town fc": "luton",
        "luton town": "luton",
        "luton": "luton",
        "sheffield united fc": "sheffield united",
        "sheffield united": "sheffield united",
        "sheffield utd": "sheffield united",
    }

    if value in aliases:
        return aliases[value]

    value = re.sub(
        r"\b(?:football club|fc|afc)\b",
        "",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    return aliases.get(
        value,
        value
    )



def short_team_name(name):
    """Display-friendly club names; does not alter stored/API team names."""
    key = canonical_team_name(name)

    names = {
        "arsenal": "Arsenal",
        "aston villa": "Aston Villa",
        "bournemouth": "Bournemouth",
        "brentford": "Brentford",
        "brighton": "Brighton",
        "burnley": "Burnley",
        "chelsea": "Chelsea",
        "crystal palace": "Crystal Palace",
        "everton": "Everton",
        "fulham": "Fulham",
        "leeds": "Leeds",
        "liverpool": "Liverpool",
        "manchester city": "Man City",
        "manchester united": "Man Utd",
        "newcastle": "Newcastle",
        "nottingham forest": "Nott'm Forest",
        "sunderland": "Sunderland",
        "tottenham": "Spurs",
        "west ham": "West Ham",
        "wolves": "Wolves",
        "leicester": "Leicester",
        "ipswich": "Ipswich",
        "southampton": "Southampton",
        "luton": "Luton",
        "sheffield united": "Sheffield Utd",
        "coventry city": "Coventry",
        "queens park rangers": "QPR",
        "west brom": "West Brom",
        "norwich": "Norwich",
        "watford": "Watford",
    }

    if key in names:
        return names[key]

    # Safe generic fallback: remove common suffixes while retaining readable case.
    value = (name or "").strip()
    value = re.sub(r"\s+(?:FC|AFC)$", "", value, flags=re.I)
    return value



def football_data_co_uk_season_code(season):
    return (
        f"{season % 100:02d}"
        f"{(season + 1) % 100:02d}"
    )


def import_historical_csv_season(conn, season):
    season_code = football_data_co_uk_season_code(
        season
    )

    url = (
        "https://www.football-data.co.uk/"
        f"mmz4281/{season_code}/E0.csv"
    )

    response = requests.get(
        url,
        timeout=30
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"football-data.co.uk HTTP "
            f"{response.status_code}"
        )

    reader = csv.DictReader(
        io.StringIO(
            response.text
        )
    )

    imported = 0

    for row_number, row in enumerate(
        reader,
        start=1
    ):
        home = (
            row.get("HomeTeam")
            or ""
        ).strip()

        away = (
            row.get("AwayTeam")
            or ""
        ).strip()

        hs = (
            row.get("FTHG")
            or ""
        ).strip()

        aas = (
            row.get("FTAG")
            or ""
        ).strip()

        date_value = (
            row.get("Date")
            or ""
        ).strip()

        if not (
            home
            and away
            and hs != ""
            and aas != ""
            and date_value
        ):
            continue

        try:
            hs = int(hs)
            aas = int(aas)

            match_date = datetime.strptime(
                date_value,
                "%d/%m/%Y"
            ).replace(
                tzinfo=timezone.utc
            )

        except (ValueError, TypeError):
            continue

        identity = (
            f"{season}|{date_value}|"
            f"{home}|{away}|{row_number}"
        )

        match_id = -(
            zlib.crc32(
                identity.encode("utf-8")
            )
            + 1
        )

        conn.execute(
            """
            INSERT INTO historical_fixtures (
                id,
                season,
                matchday,
                utc_date,
                home_team,
                away_team,
                home_score,
                away_score,
                status
            )
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'FINISHED')

            ON CONFLICT(id)
            DO UPDATE SET
                season = excluded.season,
                utc_date = excluded.utc_date,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                status = 'FINISHED'
            """,
            (
                match_id,
                season,
                match_date.isoformat(),
                home,
                away,
                hs,
                aas,
            )
        )

        imported += 1

    return imported


def _result_for_team(
    home_team,
    away_team,
    home_score,
    away_score,
    team
):
    if home_score is None or away_score is None:
        return None

    team_key = canonical_team_name(
        team
    )

    if team_key == canonical_team_name(
        home_team
    ):
        gf = home_score
        ga = away_score

    elif team_key == canonical_team_name(
        away_team
    ):
        gf = away_score
        ga = home_score

    else:
        return None

    if gf > ga:
        result = "W"
    elif gf < ga:
        result = "L"
    else:
        result = "D"

    return {
        "result": result,
        "gf": gf,
        "ga": ga,
    }


def _record_summary(rows, team):
    wins = draws = losses = gf = ga = 0

    for row in rows:
        result = _result_for_team(
            row["home_team"],
            row["away_team"],
            row["home_score"],
            row["away_score"],
            team
        )

        if not result:
            continue

        gf += result["gf"]
        ga += result["ga"]

        if result["result"] == "W":
            wins += 1
        elif result["result"] == "D":
            draws += 1
        else:
            losses += 1

    played = wins + draws + losses

    return {
        "played": played,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "gf": gf,
        "ga": ga,
        "points": wins * 3 + draws,
        "ppg": (
            round(
                (wins * 3 + draws) / played,
                2
            )
            if played
            else 0
        ),
    }


def _recent_form(rows, team, limit=5):
    form = []

    for row in rows[:limit]:
        result = _result_for_team(
            row["home_team"],
            row["away_team"],
            row["home_score"],
            row["away_score"],
            team
        )

        if result:
            form.append(
                result["result"]
            )

    return form


def _historical_source_rows(
    conn,
    before_utc
):
    """
    Combine current stored completed fixtures and the separate historical
    archive. All calculations are local once the data has been downloaded.
    """
    current_rows = conn.execute(
        """
        SELECT
            id,
            season,
            matchday,
            utc_date,
            home_team,
            away_team,
            home_score,
            away_score
        FROM fixtures
        WHERE status = 'FINISHED'
          AND home_score IS NOT NULL
          AND away_score IS NOT NULL
          AND utc_date < ?
        """,
        (before_utc,),
    ).fetchall()

    history_rows = conn.execute(
        """
        SELECT
            id,
            season,
            matchday,
            utc_date,
            home_team,
            away_team,
            home_score,
            away_score
        FROM historical_fixtures
        WHERE home_score IS NOT NULL
          AND away_score IS NOT NULL
          AND utc_date < ?
        """,
        (before_utc,),
    ).fetchall()

    # Prevent accidental duplicate API match IDs if data has been imported
    # into both tables.
    by_id = {}

    for row in list(history_rows) + list(current_rows):
        by_id[row["id"]] = row

    return sorted(
        by_id.values(),
        key=lambda row: row["utc_date"],
        reverse=True
    )


def match_stats_for_fixture(
    conn,
    fixture
):
    home_team = fixture["home_team"]
    away_team = fixture["away_team"]
    kickoff = fixture["utc_date"]

    all_prior = _historical_source_rows(
        conn,
        kickoff
    )

    home_key = canonical_team_name(
        home_team
    )
    away_key = canonical_team_name(
        away_team
    )

    # Current-season home and away records only.
    # Use canonical names because different deterministic data sources
    # use variants such as "Arsenal" and "Arsenal FC".
    home_record_rows = [
        row
        for row in all_prior
        if row["season"] == SEASON
        and canonical_team_name(
            row["home_team"]
        ) == home_key
    ]

    away_record_rows = [
        row
        for row in all_prior
        if row["season"] == SEASON
        and canonical_team_name(
            row["away_team"]
        ) == away_key
    ]

    home_form_rows = [
        row
        for row in all_prior
        if row["season"] == SEASON
        and (
            canonical_team_name(
                row["home_team"]
            ) == home_key
            or canonical_team_name(
                row["away_team"]
            ) == home_key
        )
    ]

    away_form_rows = [
        row
        for row in all_prior
        if row["season"] == SEASON
        and (
            canonical_team_name(
                row["home_team"]
            ) == away_key
            or canonical_team_name(
                row["away_team"]
            ) == away_key
        )
    ]

    head_to_head = [
        row
        for row in all_prior
        if {
            canonical_team_name(
                row["home_team"]
            ),
            canonical_team_name(
                row["away_team"]
            )
        } == {
            home_key,
            away_key
        }
    ][:5]

    home_h2h_wins = 0
    away_h2h_wins = 0
    h2h_draws = 0

    for row in head_to_head:
        home_result = _result_for_team(
            row["home_team"],
            row["away_team"],
            row["home_score"],
            row["away_score"],
            home_team
        )

        if not home_result:
            continue

        if home_result["result"] == "W":
            home_h2h_wins += 1
        elif home_result["result"] == "L":
            away_h2h_wins += 1
        else:
            h2h_draws += 1

    return {
        "home_record": _record_summary(
            home_record_rows,
            home_team
        ),
        "away_record": _record_summary(
            away_record_rows,
            away_team
        ),
        "home_form": _recent_form(
            home_form_rows,
            home_team
        ),
        "away_form": _recent_form(
            away_form_rows,
            away_team
        ),
        "head_to_head": head_to_head,
        "h2h_home_wins": home_h2h_wins,
        "h2h_away_wins": away_h2h_wins,
        "h2h_draws": h2h_draws,
    }


def build_fixture_stats(
    conn,
    fixtures
):
    return {
        fixture["id"]: match_stats_for_fixture(
            conn,
            fixture
        )
        for fixture in fixtures
    }


def import_historical_results(
    seasons=None
):
    """
    Import previous Premier League results from deterministic data sources.
    Each season is independent so one unavailable year cannot abort all H2H.
    """
    token = get_setting(
        "football_api_token"
    )

    if seasons is None:
        seasons = [
            SEASON - 1,
            SEASON - 2,
            SEASON - 3,
            SEASON - 4,
            SEASON - 5,
        ]

    set_setting(
        "historical_results_last_attempt",
        now_utc().isoformat()
    )

    conn = get_db()
    imported = 0
    sources = []
    errors = []

    try:
        for season in seasons:
            season_imported = 0
            api_error = None

            if token:
                try:
                    matches = get_matches(
                        token,
                        season=season
                    )

                    for match in matches:
                        if match.get("status") != "FINISHED":
                            continue

                        full_time = (
                            match.get("score", {})
                            .get("fullTime", {})
                        )

                        hs = full_time.get("home")
                        aas = full_time.get("away")

                        if hs is None or aas is None:
                            continue

                        conn.execute(
                            """
                            INSERT INTO historical_fixtures (
                                id, season, matchday, utc_date,
                                home_team, away_team,
                                home_score, away_score, status
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'FINISHED')

                            ON CONFLICT(id)
                            DO UPDATE SET
                                season = excluded.season,
                                matchday = excluded.matchday,
                                utc_date = excluded.utc_date,
                                home_team = excluded.home_team,
                                away_team = excluded.away_team,
                                home_score = excluded.home_score,
                                away_score = excluded.away_score,
                                status = 'FINISHED'
                            """,
                            (
                                match["id"],
                                season,
                                match.get("matchday"),
                                match["utcDate"],
                                match.get("homeTeam", {}).get("name", ""),
                                match.get("awayTeam", {}).get("name", ""),
                                hs,
                                aas,
                            )
                        )

                        season_imported += 1

                except Exception as exc:
                    api_error = str(exc)

            if season_imported:
                sources.append(
                    f"{season}/{season + 1}: football-data.org"
                )
            else:
                try:
                    season_imported = (
                        import_historical_csv_season(
                            conn,
                            season
                        )
                    )

                    if season_imported:
                        sources.append(
                            f"{season}/{season + 1}: football-data.co.uk"
                        )

                except Exception as exc:
                    detail = (
                        f"{season}/{season + 1}: "
                    )

                    if api_error:
                        detail += (
                            f"API {api_error}; "
                        )

                    detail += (
                        f"CSV {exc}"
                    )

                    errors.append(detail)

            imported += season_imported

            # Preserve every successful season even if a later one fails.
            conn.commit()

        set_setting(
            "historical_results_last_refresh",
            now_utc().isoformat()
        )

        set_setting(
            "historical_results_last_sources",
            " | ".join(sources)
        )

        set_setting(
            "historical_results_last_error",
            " | ".join(errors)
        )

        return imported

    finally:
        conn.close()



def import_matches_from_api():
    token = get_setting(
        "football_api_token"
    )

    if not token:
        return 0

    matches = get_matches(
        token,
        season=SEASON
    )

    conn = get_db()
    imported = 0
    final_scorer_backfills = 0

    for match in matches:
        home = match.get(
            "homeTeam",
            {}
        )

        away = match.get(
            "awayTeam",
            {}
        )

        full_time = match.get(
            "score",
            {}
        ).get(
            "fullTime",
            {}
        )

        existing = conn.execute(
            "SELECT goals_json FROM fixtures WHERE id = ?",
            (match["id"],),
        ).fetchone()
        existing_goals_json = existing["goals_json"] if existing else None

        goals = match.get("goals")
        score_has_goals = any(
            isinstance(score, (int, float)) and score > 0
            for score in (full_time.get("home"), full_time.get("away"))
        )
        status = match.get("status")
        needs_live_goal_details = (
            goals is None or (not goals and score_has_goals)
        ) and status in ("LIVE", "IN_PLAY", "PAUSED")
        needs_final_goal_details = (
            status == "FINISHED"
            and existing_goals_json is None
            and score_has_goals
            and final_scorer_backfills < FINAL_SCORER_BACKFILL_PER_REFRESH
        )
        if status == "FINISHED" and existing_goals_json is None and not score_has_goals:
            goals = []
        if needs_live_goal_details or needs_final_goal_details:
            if needs_final_goal_details:
                # Count attempts as well as successes so a provider outage can
                # never turn a quiet refresh into hundreds of detail requests.
                final_scorer_backfills += 1
            try:
                details = get_match(token, match["id"])
                goals = details.get("goals")
            except FootballAPIError as exc:
                print(
                    f"[live-scorers] Match {match['id']}: {exc}",
                    flush=True,
                )

        conn.execute(
            """
            INSERT INTO fixtures (
                id,
                season,
                matchday,
                utc_date,
                status,
                home_team,
                away_team,
                home_score,
                away_score,
                last_updated,
                minute,
                injury_time,
                goals_json,
                live_data_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(id)
            DO UPDATE SET
                matchday = excluded.matchday,
                utc_date = excluded.utc_date,
                status = excluded.status,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                last_updated = excluded.last_updated,
                minute = excluded.minute,
                injury_time = excluded.injury_time,
                goals_json = COALESCE(excluded.goals_json, fixtures.goals_json),
                live_data_source = excluded.live_data_source
            """,
            (
                match["id"],
                SEASON,
                match.get("matchday"),
                match["utcDate"],
                match["status"],
                home.get("name", ""),
                away.get("name", ""),
                full_time.get("home"),
                full_time.get("away"),
                match.get("lastUpdated"),
                match.get("minute"),
                match.get("injuryTime"),
                json.dumps(goals) if goals is not None else None,
                "football-data.org",
            ),
        )

        imported += 1

    try:
        tv_updated = refresh_tv_broadcasters(conn)
        print(
            f"[tv-listings] Updated {tv_updated} broadcaster(s)",
            flush=True
        )
    except Exception as exc:
        print(
            f"[tv-listings] Refresh failed: {exc}",
            flush=True
        )

    refresh_points(conn)
    archive_completed_season(conn, SEASON)

    conn.commit()
    conn.close()

    set_setting(
        "last_api_refresh",
        now_utc().isoformat()
    )

    return imported


def normalized_team_name(name):
    value = re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()).strip()
    words = [word for word in value.split() if word not in ("fc", "afc")]
    value = " ".join(words)
    aliases = {
        "brighton hove albion": "brighton",
        "manchester city": "man city",
        "manchester united": "man united",
        "newcastle united": "newcastle",
        "nottingham forest": "nottm forest",
        "tottenham hotspur": "tottenham",
        "west ham united": "west ham",
        "wolverhampton wanderers": "wolves",
    }
    return aliases.get(value, value)


def sportscore_team_slug(name):
    normalized = normalized_team_name(name)
    aliases = {
        "man city": "manchester-city",
        "man united": "manchester-united",
        "newcastle": "newcastle-united",
        "nottm forest": "nottingham-forest",
        "tottenham": "tottenham-hotspur",
        "west ham": "west-ham-united",
        "wolves": "wolverhampton-wanderers",
    }
    return aliases.get(normalized, normalized.replace(" ", "-"))


def import_live_matches_from_sportscore(force_current_gameweek=False):
    matches = get_sportscore_live_matches()
    conn = get_db()
    updated = 0

    try:
        fixtures = conn.execute(
            """
            SELECT id, home_team, away_team, home_score, away_score,
                   status, goals_json
            FROM fixtures
            WHERE season = ?
              AND matchday = COALESCE(
                  (
                      SELECT MIN(matchday) FROM fixtures
                      WHERE season = ?
                        AND matchday IS NOT NULL
                        AND status NOT IN ('FINISHED', 'CANCELLED')
                  ),
                  (
                      SELECT MAX(matchday) FROM fixtures
                      WHERE season = ? AND matchday IS NOT NULL
                  )
              )
              AND status != 'CANCELLED'
            """,
            (SEASON, SEASON, SEASON),
        ).fetchall()
        fixture_map = {
            (
                normalized_team_name(row["home_team"]),
                normalized_team_name(row["away_team"]),
            ): row
            for row in fixtures
        }

        if force_current_gameweek:
            known_keys = {
                (
                    normalized_team_name(match.get("home")),
                    normalized_team_name(match.get("away")),
                )
                for match in matches
            }
            for fixture in fixtures:
                needs_scorers = (
                    not fixture["goals_json"]
                    and (
                        (fixture["home_score"] or 0) > 0
                        or (fixture["away_score"] or 0) > 0
                    )
                )
                key = (
                    normalized_team_name(fixture["home_team"]),
                    normalized_team_name(fixture["away_team"]),
                )
                if not needs_scorers or key in known_keys:
                    continue

                team_matches = get_sportscore_team_matches(
                    sportscore_team_slug(fixture["home_team"])
                )
                for candidate in team_matches:
                    candidate_key = (
                        normalized_team_name(candidate.get("home")),
                        normalized_team_name(candidate.get("away")),
                    )
                    if candidate_key == key:
                        matches.append(candidate)
                        known_keys.add(key)
                        break

        for match in matches:
            key = (
                normalized_team_name(match.get("home")),
                normalized_team_name(match.get("away")),
            )
            stored = fixture_map.get(key)
            if not stored:
                continue

            details = get_sportscore_match_details(match)
            goals = sportscore_goal_events(details)
            goals_json = json.dumps(goals) if goals else None
            minute_value = details.get("live_minute")
            try:
                minute_value = int(minute_value) if minute_value is not None else None
            except (TypeError, ValueError):
                minute_value = None

            conn.execute(
                """
                UPDATE fixtures
                SET status = ?,
                    home_score = COALESCE(?, home_score),
                    away_score = COALESCE(?, away_score),
                    minute = COALESCE(?, minute),
                    goals_json = COALESCE(?, goals_json),
                    last_updated = ?,
                    live_data_source = 'SportScore'
                WHERE id = ?
                """,
                (
                    "FINISHED" if details.get("status") == "finished" else "IN_PLAY",
                    details.get("home_score"),
                    details.get("away_score"),
                    minute_value,
                    goals_json,
                    now_utc().isoformat(),
                    stored["id"],
                ),
            )
            updated += 1

        if updated:
            refresh_points(conn)
            archive_completed_season(conn, SEASON)
        conn.commit()
    finally:
        conn.close()

    set_setting("last_sportscore_refresh", now_utc().isoformat())
    return updated


def next_api_refresh_delay():
    """
    Choose the next API refresh without ever sleeping through kickoff.

    The old implementation could enter the 6-hour quiet sleep while the next
    match was only a couple of hours away. This function calculates the wake-up
    time from the next stored kickoff instead.
    """
    conn = get_db()

    fixtures = conn.execute(
        """
        SELECT utc_date, status
        FROM fixtures
        WHERE season = ?
          AND status NOT IN (
              'FINISHED',
              'CANCELLED'
          )
        ORDER BY utc_date
        """,
        (SEASON,),
    ).fetchall()

    conn.close()

    now = now_utc()
    next_wake = None

    for fixture in fixtures:
        status = fixture["status"]

        if status in (
            "LIVE",
            "IN_PLAY",
            "PAUSED",
        ):
            return LIVE_REFRESH_SECONDS

        kickoff = parse_utc(
            fixture["utc_date"]
        )

        if not kickoff:
            continue

        live_start = (
            kickoff
            - timedelta(
                seconds=LIVE_WINDOW_BEFORE_SECONDS
            )
        )

        live_end = (
            kickoff
            + timedelta(
                seconds=LIVE_WINDOW_AFTER_SECONDS
            )
        )

        if live_start <= now <= live_end:
            return LIVE_REFRESH_SECONDS

        if now < live_start:
            seconds = (
                live_start - now
            ).total_seconds()

            if (
                next_wake is None
                or seconds < next_wake
            ):
                next_wake = seconds

    if next_wake is None:
        return QUIET_REFRESH_SECONDS

    return int(
        max(
            MIN_REFRESH_SLEEP_SECONDS,
            min(
                QUIET_REFRESH_SECONDS,
                next_wake
            )
        )
    )


def live_window_active():
    """
    Backwards-compatible helper used by tests/diagnostics.
    """
    return (
        next_api_refresh_delay()
        == LIVE_REFRESH_SECONDS
    )


def api_refresh_worker():
    time.sleep(20)

    # Historical results are static once a season has finished.
    try:
        if (
            get_setting("football_api_token")
            and not get_setting(
                "historical_results_last_attempt"
            )
        ):
            imported_history = (
                import_historical_results()
            )

            print(
                f"[match-stats] Imported "
                f"{imported_history} historical result(s)",
                flush=True
            )

    except Exception as exc:
        print(
            f"[match-stats] Historical import failed: {exc}",
            flush=True
        )

    while True:
        try:
            if get_setting(
                "football_api_token"
            ):
                imported = import_matches_from_api()

                set_setting(
                    "last_api_error",
                    ""
                )

                print(
                    f"[auto-refresh] "
                    f"Updated {imported} fixture(s)",
                    flush=True
                )

        except Exception as e:
            set_setting(
                "last_api_error",
                str(e)
            )

            set_setting(
                "last_api_error_at",
                now_utc().isoformat()
            )

            print(
                f"[auto-refresh] {e}",
                flush=True
            )

        delay = next_api_refresh_delay()

        if delay == LIVE_REFRESH_SECONDS:
            try:
                live_updates = import_live_matches_from_sportscore()
                set_setting("last_sportscore_error", "")
                print(
                    f"[SportScore] Updated {live_updates} live fixture(s)",
                    flush=True,
                )
            except Exception as exc:
                set_setting("last_sportscore_error", str(exc))
                set_setting("last_sportscore_error_at", now_utc().isoformat())
                print(f"[SportScore] {exc}", flush=True)

        print(
            f"[auto-refresh] "
            f"Next check in {delay}s",
            flush=True
        )

        time.sleep(
            delay
        )



def prune_auto_backups():
    if not os.path.exists(
        BACKUP_DIR
    ):
        return

    auto_backups = []

    for name in os.listdir(
        BACKUP_DIR
    ):
        if not (
            name.startswith(
                "auto-premier-league-predictor-"
            )
            and name.endswith(".db")
        ):
            continue

        path = os.path.join(
            BACKUP_DIR,
            name
        )

        if os.path.isfile(path):
            auto_backups.append(
                (
                    os.path.getmtime(path),
                    path
                )
            )

    auto_backups.sort(
        key=lambda item: item[0],
        reverse=True
    )

    for _, old_path in auto_backups[
        MAX_AUTO_BACKUPS:
    ]:
        try:
            os.remove(old_path)

        except OSError as e:
            print(
                f"[auto-backup] "
                f"Failed to delete "
                f"{old_path}: {e}",
                flush=True
            )


def create_automatic_backup():
    os.makedirs(
        BACKUP_DIR,
        exist_ok=True
    )

    timestamp = now_utc().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    backup_name = (
        "auto-premier-league-predictor-"
        f"{timestamp}.db"
    )

    backup_path = os.path.join(
        BACKUP_DIR,
        backup_name
    )

    source = sqlite3.connect(DB)
    destination = sqlite3.connect(
        backup_path
    )

    try:
        source.backup(destination)

    finally:
        destination.close()
        source.close()

    prune_auto_backups()

    set_setting(
        "last_auto_backup",
        now_utc().isoformat()
    )

    print(
        f"[auto-backup] "
        f"Created {backup_path}",
        flush=True
    )

    return backup_path


def auto_backup_worker():
    time.sleep(60)

    while True:
        try:
            create_local_and_cloud_backup()

        except Exception as e:
            print(
                f"[auto-backup] {e}",
                flush=True
            )

        time.sleep(
            AUTO_BACKUP_SECONDS
        )

def create_database_backup():
    timestamp = now_utc().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    backup_name = (
        "premier-league-predictor-"
        f"{timestamp}.db"
    )

    backup_path = os.path.join(
        BACKUP_DIR,
        backup_name
    )

    source = sqlite3.connect(DB)
    destination = sqlite3.connect(
        backup_path
    )

    try:
        source.backup(destination)

    finally:
        destination.close()
        source.close()

    return (
        backup_path,
        backup_name
    )


def validate_restore_database(path):
    return validate_predictor_database(path)



def google_client_config():
    client_id = get_setting("google_client_id")
    client_secret = get_setting("google_client_secret")

    if not client_id or not client_secret:
        return None

    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def google_public_base_url():
    value = get_setting("public_base_url")

    if value:
        return value.rstrip("/")

    return "https://battleship.live"


def google_redirect_uri():
    return (
        google_public_base_url()
        + "/admin/google/callback"
    )


def save_google_credentials(credentials):
    data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }

    with open(
        GOOGLE_TOKEN_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(data, f)


def load_google_credentials():
    if not os.path.exists(
        GOOGLE_TOKEN_FILE
    ):
        return None

    try:
        creds = Credentials.from_authorized_user_file(
            GOOGLE_TOKEN_FILE,
            scopes=[GOOGLE_DRIVE_SCOPE]
        )

        if (
            creds
            and creds.expired
            and creds.refresh_token
        ):
            creds.refresh(
                GoogleRequest()
            )

            save_google_credentials(
                creds
            )

        if creds and creds.valid:
            return creds

    except Exception as e:
        print(
            f"[google-drive] "
            f"Credential load failed: {e}",
            flush=True
        )

    return None


def google_drive_service():
    creds = load_google_credentials()

    if not creds:
        return None

    return build(
        "drive",
        "v3",
        credentials=creds,
        cache_discovery=False
    )


def google_drive_connected():
    return (
        google_drive_service()
        is not None
    )


def google_drive_folder_id(service):
    saved = get_setting(
        "google_drive_folder_id"
    )

    if saved:
        try:
            service.files().get(
                fileId=saved,
                fields="id,name,trashed"
            ).execute()

            return saved

        except Exception:
            pass

    query = (
        "mimeType = "
        "'application/vnd.google-apps.folder' "
        "and trashed = false "
        "and appProperties has "
        "{ key='app' and value='pl-predictor' }"
    )

    result = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id,name)",
        pageSize=10
    ).execute()

    folders = result.get(
        "files",
        []
    )

    if folders:
        folder_id = folders[0]["id"]

        set_setting(
            "google_drive_folder_id",
            folder_id
        )

        return folder_id

    metadata = {
        "name": GOOGLE_BACKUP_FOLDER,
        "mimeType": (
            "application/"
            "vnd.google-apps.folder"
        ),
        "appProperties": {
            "app": "pl-predictor"
        }
    }

    folder = service.files().create(
        body=metadata,
        fields="id"
    ).execute()

    folder_id = folder["id"]

    set_setting(
        "google_drive_folder_id",
        folder_id
    )

    return folder_id


def prune_google_backups(service):
    cutoff = (
        now_utc()
        - timedelta(
            days=GOOGLE_RETENTION_DAYS
        )
    )

    query = (
        "trashed = false "
        "and appProperties has "
        "{ key='backupType' "
        "and value='pl-predictor-db' }"
    )

    page_token = None

    while True:
        result = service.files().list(
            q=query,
            spaces="drive",
            fields=(
                "nextPageToken,"
                "files(id,name,createdTime)"
            ),
            pageSize=100,
            pageToken=page_token,
        ).execute()

        for item in result.get(
            "files",
            []
        ):
            created = parse_utc(
                item.get("createdTime")
            )

            if (
                created
                and created < cutoff
            ):
                try:
                    service.files().delete(
                        fileId=item["id"]
                    ).execute()

                except Exception as e:
                    print(
                        "[google-drive] "
                        "Cloud prune failed "
                        f"for {item['name']}: {e}",
                        flush=True
                    )

        page_token = result.get(
            "nextPageToken"
        )

        if not page_token:
            break


def upload_backup_to_google_drive(
    backup_path
):
    service = google_drive_service()

    if not service:
        raise RuntimeError(
            "Google Drive is not connected."
        )

    folder_id = google_drive_folder_id(
        service
    )

    filename = os.path.basename(
        backup_path
    )

    metadata = {
        "name": filename,
        "parents": [folder_id],
        "appProperties": {
            "backupType": (
                "pl-predictor-db"
            ),
            "appVersion": APP_VERSION,
        }
    }

    media = MediaFileUpload(
        backup_path,
        mimetype="application/octet-stream",
        resumable=False
    )

    uploaded = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,createdTime"
    ).execute()

    set_setting(
        "last_google_backup",
        now_utc().isoformat()
    )

    set_setting(
        "last_google_backup_error",
        ""
    )

    prune_google_backups(
        service
    )

    print(
        "[google-drive] Uploaded "
        f"{filename}",
        flush=True
    )

    return uploaded


def create_local_and_cloud_backup():
    backup_path = create_automatic_backup()

    if google_drive_connected():
        try:
            upload_backup_to_google_drive(
                backup_path
            )

        except Exception as e:
            set_setting(
                "last_google_backup_error",
                str(e)
            )

            print(
                f"[google-drive] "
                f"Upload failed: {e}",
                flush=True
            )

    return backup_path



def signal_flag(key, default=True):
    value = get_setting(key)
    if value is None:
        return default
    return value == "1"


def signal_settings():
    return {
        "api_url": (get_setting("signal_api_url") or "https://signal.battleship.live").rstrip("/"),
        "number": (get_setting("signal_number") or "+447740514908").strip(),
        "group_id": (get_setting("signal_group_id") or "group.RElGRUlPYlBOZGJtTkIyZ2c3bzRJV0N4dTF2Vm1aQkhaQS9yNFFIVkQ3VT0=").strip(),
        "group_name": (get_setting("signal_group_name") or "Put Your Pre Dicks in").strip(),
        "enabled": get_setting("signal_enabled") == "1",
        "notify_gw_open": signal_flag("signal_notify_gw_open", True),
        "notify_reminder": signal_flag("signal_notify_reminder", True),
        "notify_results": signal_flag("signal_notify_results", True),
    }


def signal_connection_status():
    settings = signal_settings()
    try:
        response = requests.get(f"{settings['api_url']}/v1/about", timeout=8)
        response.raise_for_status()
        data = response.json()
        return {"ok": True, "version": data.get("version", "Unknown"),
                "mode": data.get("mode", "Unknown"), "build": data.get("build", "Unknown"),
                "error": None}
    except Exception as exc:
        return {"ok": False, "version": None, "mode": None, "build": None, "error": str(exc)}


def send_signal_message(message):
    settings = signal_settings()
    if not settings["enabled"]:
        raise RuntimeError("Signal notifications are disabled.")
    if not settings["api_url"] or not settings["number"] or not settings["group_id"]:
        raise RuntimeError("Signal configuration is incomplete.")

    response = requests.post(
        f"{settings['api_url']}/v2/send",
        json={"message": message, "number": settings["number"],
              "recipients": [settings["group_id"]]},
        timeout=20,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        return {"response": response.text}



def signal_current_gameweek(conn):
    row = conn.execute(
        """
        SELECT MIN(matchday) AS matchday
        FROM fixtures
        WHERE season = ?
          AND matchday IS NOT NULL
          AND status NOT IN ('FINISHED', 'CANCELLED')
        """,
        (SEASON,),
    ).fetchone()

    return (
        row["matchday"]
        if row and row["matchday"] is not None
        else None
    )


def signal_latest_completed_gameweek(conn):
    """Return the latest fully completed imported Gameweek, if any."""
    row = conn.execute(
        """
        SELECT matchday
        FROM fixtures
        WHERE season = ?
          AND matchday IS NOT NULL
        GROUP BY matchday
        HAVING SUM(
            CASE
            WHEN status NOT IN ('FINISHED', 'CANCELLED')
            THEN 1 ELSE 0
            END
        ) = 0
        ORDER BY matchday DESC
        LIMIT 1
        """,
        (SEASON,),
    ).fetchone()

    return row["matchday"] if row else None


def signal_gameweek_fixtures(conn, matchday):
    return conn.execute(
        """
        SELECT *
        FROM fixtures
        WHERE season = ?
          AND matchday = ?
        ORDER BY utc_date
        """,
        (SEASON, matchday),
    ).fetchall()


def signal_gameweek_complete(fixtures):
    return bool(fixtures) and all(
        fixture["status"] in ("FINISHED", "CANCELLED")
        for fixture in fixtures
    )


def signal_submission_status(conn, matchday):
    fixtures = signal_gameweek_fixtures(conn, matchday)
    fixture_ids = [
        fixture["id"]
        for fixture in fixtures
        if fixture["status"] != "CANCELLED"
    ]
    total = len(fixture_ids)

    players = conn.execute(
        """
        SELECT id, name
        FROM players
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()

    result = []

    for player in players:
        if not fixture_ids:
            count = 0
            has_dp = False

        else:
            placeholders = ",".join("?" for _ in fixture_ids)

            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    COALESCE(
                        SUM(
                            CASE
                            WHEN COALESCE(dp, 0) = 1
                            THEN 1 ELSE 0
                            END
                        ),
                        0
                    ) AS dp_count
                FROM predictions
                WHERE player_id = ?
                  AND fixture_id IN ({placeholders})
                """,
                (player["id"], *fixture_ids),
            ).fetchone()

            count = row["total"]
            has_dp = row["dp_count"] > 0

        result.append({
            "name": player["name"],
            "count": count,
            "total": total,
            "complete": total > 0 and count >= total,
            "has_dp": has_dp,
        })

    return result


def signal_gw_open_message(matchday, fixtures):
    if not fixtures:
        return None

    return "\n".join([
        f"⚽ Premier League Predictor — GW{matchday}",
        "",
        "Predictions are now open!",
        "",
        f"First kick-off: {local_datetime(fixtures[0]['utc_date'])}",
        "",
        "Get Your Pre-Dicks In:",
        "https://predictions.battleship.live",
    ])


def signal_reminder_message(
    matchday,
    fixtures,
    statuses,
    reminder_label=None,
    include_missing_dp=False
):
    incomplete = [
        status
        for status in statuses
        if (
            not status["complete"]
            or (
                include_missing_dp
                and not status.get("has_dp", False)
            )
        )
    ]

    if not incomplete:
        return None

    if reminder_label == "2-hour final":
        title = "Lads. Footy"
    else:
        title = f"⏰ GW{matchday} Prediction Reminder"

        if reminder_label:
            title += f" — {reminder_label}"

    lines = [
        title,
        "",
        f"First kick-off: {local_datetime(fixtures[0]['utc_date'])}",
        "",
        "Still to complete predictions:",
    ]

    for status in incomplete:
        issues = []

        if not status["complete"]:
            issues.append(
                f"{status['count']}/{status['total']} submitted"
            )

        if (
            include_missing_dp
            and not status.get("has_dp", False)
        ):
            issues.append(
                "no DP selected"
            )

        lines.append(
            f"• {status['name']} — "
            + ", ".join(issues)
        )

    lines += [
        "",
        "https://predictions.battleship.live",
    ]

    return "\n".join(lines)


def signal_gw_table(conn, matchday):
    refresh_points(conn)
    archive_completed_season(conn, SEASON)
    conn.commit()

    return conn.execute(
        """
        SELECT
            pl.name,
            COALESCE(
                SUM(
                    CASE
                    WHEN f.season = ?
                     AND f.matchday = ?
                    THEN p.points
                    ELSE 0
                    END
                ),
                0
            ) AS points,
            COALESCE(SUM(CASE
                WHEN f.season = ? AND f.matchday = ?
                 AND f.status = 'FINISHED'
                 AND p.home_score = f.home_score
                 AND p.away_score = f.away_score
                 AND f.home_score = f.away_score
                THEN 1 ELSE 0 END), 0) AS exact_draws,
            COALESCE(SUM(CASE
                WHEN f.season = ? AND f.matchday = ?
                 AND f.status = 'FINISHED'
                 AND p.home_score = f.home_score
                 AND p.away_score = f.away_score
                 AND f.home_score != f.away_score
                THEN 1 ELSE 0 END), 0) AS exact_scores
        FROM players pl
        LEFT JOIN predictions p
          ON p.player_id = pl.id
        LEFT JOIN fixtures f
          ON f.id = p.fixture_id
        GROUP BY pl.id
        ORDER BY
            points DESC,
            exact_draws DESC,
            exact_scores DESC,
            pl.name COLLATE NOCASE
        """,
        (
            SEASON, matchday,
            SEASON, matchday,
            SEASON, matchday,
        ),
    ).fetchall()


def signal_overall_table(conn):
    refresh_points(conn)
    conn.commit()

    return conn.execute(
        """
        SELECT
            pl.name,
            COALESCE(SUM(p.points), 0) AS points,
            COALESCE(SUM(CASE
                WHEN f.status = 'FINISHED'
                 AND p.home_score = f.home_score
                 AND p.away_score = f.away_score
                 AND f.home_score = f.away_score
                THEN 1 ELSE 0 END), 0) AS exact_draws,
            COALESCE(SUM(CASE
                WHEN f.status = 'FINISHED'
                 AND p.home_score = f.home_score
                 AND p.away_score = f.away_score
                 AND f.home_score != f.away_score
                THEN 1 ELSE 0 END), 0) AS exact_scores
        FROM players pl
        LEFT JOIN predictions p
          ON p.player_id = pl.id
        LEFT JOIN fixtures f
          ON f.id = p.fixture_id
        GROUP BY pl.id
        ORDER BY
            points DESC,
            exact_draws DESC,
            exact_scores DESC,
            pl.name COLLATE NOCASE
        """
    ).fetchall()


def signal_manual_reminder_key(fixtures):
    """Return the scheduled reminder slot covered by a manual send."""
    active_fixtures = [
        fixture
        for fixture in fixtures
        if fixture["status"] != "CANCELLED"
    ]
    if not active_fixtures:
        return None

    first_kickoff = parse_utc(active_fixtures[0]["utc_date"])
    if not first_kickoff:
        return None

    time_to_kickoff = first_kickoff - now_utc()
    if (
        timedelta(0) < time_to_kickoff
        <= timedelta(
            hours=SIGNAL_FINAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF
        )
    ):
        return "signal_last_reminder_2_gw"
    if (
        timedelta(
            hours=SIGNAL_FINAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF
        ) < time_to_kickoff
        <= timedelta(
            hours=SIGNAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF
        )
    ):
        return "signal_last_reminder_24_gw"
    return None


def signal_results_message(matchday, gw_table, overall_table):
    lines = [
        f"🏆 GW{matchday} Results",
        "",
    ]

    for index, row in enumerate(gw_table, start=1):
        prefix = (
            "🥇" if index == 1
            else "🥈" if index == 2
            else "🥉" if index == 3
            else f"{index}."
        )
        lines.append(
            f"{prefix} {row['name']} — {row['points']} pts"
        )

    lines += [
        "",
        "📊 Overall League",
        "",
    ]

    for index, row in enumerate(overall_table, start=1):
        lines.append(
            f"{index}. {row['name']} — {row['points']} pts"
        )

    lines += [
        "",
        "https://predictions.battleship.live",
    ]

    return "\n".join(lines)


def process_signal_notifications():
    settings = signal_settings()

    if not settings["enabled"]:
        return

    conn = get_db()

    try:
        current_gw = signal_current_gameweek(conn)

        # Open/reminder automation only applies to an unfinished current GW.
        if current_gw is not None:
            fixtures = signal_gameweek_fixtures(
                conn,
                current_gw
            )

            if fixtures:
                if settings["notify_gw_open"]:
                    last_open = get_setting(
                        "signal_last_open_gw"
                    )

                    if str(current_gw) != (last_open or ""):
                        message = signal_gw_open_message(
                            current_gw,
                            fixtures
                        )

                        if message:
                            send_signal_message(message)
                            set_setting(
                                "signal_last_open_gw",
                                str(current_gw)
                            )

                if settings["notify_reminder"]:
                    active_fixtures = [
                        fixture
                        for fixture in fixtures
                        if fixture["status"] != "CANCELLED"
                    ]

                    if active_fixtures:
                        first_kickoff = parse_utc(
                            active_fixtures[0]["utc_date"]
                        )

                        if first_kickoff:
                            time_to_kickoff = first_kickoff - now_utc()
                            reminder_24_key = "signal_last_reminder_24_gw"
                            reminder_2_key = "signal_last_reminder_2_gw"
                            last_24 = get_setting(reminder_24_key)
                            last_2 = get_setting(reminder_2_key)
                            statuses = signal_submission_status(conn, current_gw)

                            if (
                                timedelta(hours=SIGNAL_FINAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF)
                                < time_to_kickoff
                                <= timedelta(hours=SIGNAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF)
                                and str(current_gw) != (last_24 or "")
                            ):
                                message = signal_reminder_message(
                                    current_gw,
                                    active_fixtures,
                                    statuses,
                                    reminder_label="24-hour",
                                    include_missing_dp=False
                                )
                                if message:
                                    send_signal_message(message)
                                set_setting(reminder_24_key, str(current_gw))

                            if (
                                timedelta(0) < time_to_kickoff
                                <= timedelta(hours=SIGNAL_FINAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF)
                                and str(current_gw) != (last_2 or "")
                            ):
                                message = signal_reminder_message(
                                    current_gw,
                                    active_fixtures,
                                    statuses,
                                    reminder_label="2-hour final",
                                    include_missing_dp=True
                                )
                                if message:
                                    send_signal_message(message)
                                set_setting(reminder_2_key, str(current_gw))

        # Results are independent of whether a future Gameweek has been imported.
        if settings["notify_results"]:
            result_gw = signal_latest_completed_gameweek(conn)
            last_results = get_setting("signal_last_results_gw")

            if (
                result_gw is not None
                and str(result_gw) != (last_results or "")
            ):
                send_signal_message(
                    signal_results_message(
                        result_gw,
                        signal_gw_table(conn, result_gw),
                        signal_overall_table(conn)
                    )
                )
                set_setting(
                    "signal_last_results_gw",
                    str(result_gw)
                )

        set_setting(
            "signal_last_notification_error",
            ""
        )

    except Exception as exc:
        set_setting(
            "signal_last_notification_error",
            str(exc)
        )
        raise

    finally:
        conn.close()


def signal_notification_worker():
    time.sleep(90)

    while True:
        try:
            process_signal_notifications()

        except Exception as exc:
            print(
                f"[signal-notify] {exc}",
                flush=True
            )

        time.sleep(
            SIGNAL_NOTIFICATION_CHECK_SECONDS
        )


@app.context_processor
def inject_globals():
    return {
        "local_datetime": local_datetime,
        "kickoff_passed": kickoff_passed,
        "status_label": status_label,
        "fixture_display_status": fixture_display_status,
        "calculate_points": calculate_points,
        "calculate_prediction_points": calculate_prediction_points,
        "app_version": APP_VERSION,
        "changelog_has_update": changelog_has_unread_update(),
        "broadcaster_logo_url": broadcaster_logo_url,
        "is_logged_in": logged_in(),
    }



@app.context_processor
def inject_short_team_name():
    return {
        "short_team_name": short_team_name
    }


@app.route("/", methods=["GET", "POST"])
def login():
    if not database_has_users(DB):
        return redirect("/first-run/restore")

    if logged_in():
        return redirect("/dashboard")

    registration_enabled = (
        get_setting(
            "registration_enabled"
        ) == "1"
    )

    if request.method == "POST":
        identifier = request.form.get(
            "identifier",
            ""
        ).strip()

        pin = request.form.get(
            "pin",
            ""
        ).strip()

        conn = get_db()

        player = conn.execute(
            """
            SELECT *
            FROM players
            WHERE (
                LOWER(email) = LOWER(?)
                OR (email IS NULL AND LOWER(COALESCE(login_name, name)) = LOWER(?))
            )
              AND pin_hash = ?
            """,
            (
                identifier,
                identifier,
                hash_pin(pin)
            ),
        ).fetchone()

        conn.close()

        if player:
            session.clear()

            session["player_id"] = player["id"]
            session["player_name"] = player["name"]
            session["admin"] = bool(
                player["admin"]
            )

            return redirect(
                "/dashboard"
            )

        flash(
            "Incorrect email or PIN.",
            "error"
        )

    return render_template(
        "login.html",
        registration_enabled=registration_enabled
    )


@app.route("/first-run/restore", methods=["GET", "POST"])
def first_run_restore():
    if database_has_users(DB):
        ensure_first_run_restore_token()
        return redirect("/")

    if request.method == "POST":
        supplied_token = request.form.get("restore_code", "").strip()
        expected_token = ensure_first_run_restore_token()
        if not secrets.compare_digest(supplied_token, expected_token or ""):
            flash("The first-run restore code is incorrect.", "error")
            return redirect("/first-run/restore")

        uploaded = request.files.get("backup_file")
        if not uploaded or not uploaded.filename:
            flash("Choose a Predictor .db backup first.", "error")
            return redirect("/first-run/restore")

        filename = secure_filename(uploaded.filename)
        if not filename.lower().endswith(".db"):
            flash("Backup must be a .db file.", "error")
            return redirect("/first-run/restore")

        temp_path = os.path.join(
            UPLOAD_DIR, "first-run-" + secrets.token_hex(16) + ".db"
        )
        uploaded.save(temp_path)
        try:
            with database_restore_lock:
                # Recheck inside the lock so concurrent requests cannot replace
                # a database after the first successful restore creates users.
                if database_has_users(DB):
                    return redirect("/")
                validate_predictor_database(temp_path, require_users=True)
                install_database(temp_path, DB)
                init_db(seed_default_player=False)

            session.clear()
            ensure_first_run_restore_token()
            flash("Backup restored successfully. Please log in.", "success")
            return redirect("/")
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            app.logger.warning("First-run database restore rejected: %s", exc)
            flash(f"Restore failed: {exc}", "error")
            return redirect("/first-run/restore")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    ensure_first_run_restore_token()
    return render_template("first_run_restore.html")


def order_players_for_fixture(
    players,
    fixture,
    prediction_map,
    revealed,
):
    """Order a fixture's player rows by current match points."""

    def sort_key(player):
        points = 0
        pred = prediction_map.get(
            (player["id"], fixture["id"])
        )

        if (
            revealed
            and pred
            and fixture["home_score"] is not None
            and fixture["away_score"] is not None
        ):
            points = calculate_prediction_points(
                pred["home_score"],
                pred["away_score"],
                fixture["home_score"],
                fixture["away_score"],
                bool(pred["dp"]),
            )

        return (-points, player["name"].casefold())

    return sorted(players, key=sort_key)


@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():
    if logged_in():
        return redirect("/dashboard")

    registration_enabled = (
        get_setting(
            "registration_enabled"
        ) == "1"
    )

    if not registration_enabled:
        return render_template(
            "register.html",
            registration_enabled=False
        )

    if request.method == "POST":
        name = request.form.get(
            "name",
            ""
        ).strip()

        email = request.form.get("email", "").strip().casefold()

        pin = request.form.get(
            "pin",
            ""
        ).strip()

        pin_confirm = request.form.get(
            "pin_confirm",
            ""
        ).strip()

        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            flash("Enter a valid email address.", "error")
            return redirect("/register")

        if (
            len(name) < 2
            or len(name) > 30
        ):
            flash(
                "Name must be between "
                "2 and 30 characters.",
                "error"
            )

            return redirect(
                "/register"
            )

        if (
            not pin.isdigit()
            or not 4 <= len(pin) <= 8
        ):
            flash(
                "PIN must contain "
                "4 to 8 digits.",
                "error"
            )

            return redirect(
                "/register"
            )

        if pin != pin_confirm:
            flash(
                "PINs do not match.",
                "error"
            )

            return redirect(
                "/register"
            )

        conn = get_db()

        existing = conn.execute(
            """
            SELECT id
            FROM players
            WHERE LOWER(email) = LOWER(?)
            """,
            (email,),
        ).fetchone()

        if existing:
            conn.close()

            flash(
                "That email address is already registered.",
                "error"
            )

            return redirect(
                "/register"
            )

        conn.execute(
            """
            INSERT INTO players(
                name,
                login_name,
                email,
                pin_hash,
                admin
            )
            VALUES (?, ?, ?, ?, 0)
            """,
            (
                name,
                name,
                email,
                hash_pin(pin)
            ),
        )

        conn.commit()
        conn.close()

        flash(
            "Registration complete. "
            "You can log in now.",
            "success"
        )

        return redirect("/")

    return render_template(
        "register.html",
        registration_enabled=True
    )



@app.route("/account", methods=["GET", "POST"])
def account():
    if not logged_in():
        return redirect("/")

    conn = get_db()
    player = conn.execute(
        "SELECT id, name, login_name, email, admin FROM players WHERE id = ?",
        (session["player_id"],)
    ).fetchone()

    if not player:
        conn.close()
        session.clear()
        return redirect("/")

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().casefold()
        pin = request.form.get("pin", "").strip()
        pin_confirm = request.form.get("pin_confirm", "").strip()

        if len(name) < 2 or len(name) > 30:
            conn.close()
            flash("Name must be between 2 and 30 characters.", "error")
            return redirect("/account")

        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            conn.close()
            flash("Enter a valid email address.", "error")
            return redirect("/account")

        email_dup = conn.execute(
            "SELECT id FROM players WHERE LOWER(email)=LOWER(?) AND id != ?",
            (email, session["player_id"]),
        ).fetchone()
        if email_dup:
            conn.close()
            flash("Another player already uses that email address.", "error")
            return redirect("/account")

        dup = conn.execute(
            "SELECT id FROM players WHERE LOWER(name)=LOWER(?) AND id != ?",
            (name, session["player_id"])
        ).fetchone()

        if dup:
            conn.close()
            flash("Another player already uses that name.", "error")
            return redirect("/account")

        if pin:
            if not pin.isdigit() or not 4 <= len(pin) <= 8:
                conn.close()
                flash("PIN must contain 4 to 8 digits.", "error")
                return redirect("/account")
            if pin != pin_confirm:
                conn.close()
                flash("PINs do not match.", "error")
                return redirect("/account")
            conn.execute(
                "UPDATE players SET name=?, email=?, pin_hash=? WHERE id=?",
                (name, email, hash_pin(pin), session["player_id"])
            )
        else:
            conn.execute(
                "UPDATE players SET name=?, email=? WHERE id=?",
                (name, email, session["player_id"])
            )

        conn.commit()
        conn.close()
        session["player_name"] = name
        flash("Your account has been updated.", "success")
        return redirect("/account")

    conn.close()
    return render_template("account.html", player=player)



def read_app_changelog():
    candidates = [
        "/app/CHANGELOG.md",
        os.path.join(
            os.path.dirname(__file__),
            "CHANGELOG.md"
        ),
        os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "CHANGELOG.md"
        ),
    ]

    path = next(
        (p for p in candidates if os.path.exists(p)),
        None
    )

    if not path:
        return []

    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    releases = []
    release = None
    section = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if line.startswith("## ["):
            match = re.match(
                r"## \[([^\]]+)\](?:\s*-\s*(.*))?",
                line
            )

            if match:
                release = {
                    "version": match.group(1),
                    "date": match.group(2) or "",
                    "sections": [],
                }
                releases.append(release)
                section = None

            continue

        if release and line.startswith("### "):
            section = {
                "title": line[4:].strip(),
                "items": [],
            }
            release["sections"].append(section)
            continue

        if release and section and line.startswith("- "):
            section["items"].append(
                line[2:].strip()
            )
            continue

        if (
            release
            and section
            and raw_line.startswith("  - ")
            and section["items"]
        ):
            section["items"][-1] += (
                " · " + raw_line.strip()[2:].strip()
            )

    return releases


@app.route("/changelog")
def changelog():
    if not logged_in():
        return redirect("/")

    mark_changelog_seen()

    return render_template(
        "changelog.html",
        releases=read_app_changelog(),
    )


@app.route("/prize-structure")
def prize_structure():
    if not logged_in():
        return redirect("/")

    return render_template(
        "prize_structure.html"
    )


@app.route("/rules")
def rules():
    if not logged_in():
        return redirect("/")

    return render_template(
        "rules.html"
    )


@app.route("/seasons")
def historical_seasons():
    if not logged_in():
        return redirect("/")

    conn = get_db()
    archives = conn.execute(
        """
        SELECT season, label, winner_name, archived_at, stats_available
        FROM season_archives
        ORDER BY season DESC
        """
    ).fetchall()
    title_rows = conn.execute(
        """
        SELECT winner_name, COUNT(*) AS titles
        FROM season_archives
        GROUP BY winner_name COLLATE NOCASE
        ORDER BY titles DESC, winner_name COLLATE NOCASE
        """
    ).fetchall()
    conn.close()

    title_record = title_rows[0]["titles"] if title_rows else 0
    most_titles = [
        row for row in title_rows
        if row["titles"] == title_record
    ]
    return render_template(
        "seasons.html",
        archives=archives,
        most_titles=most_titles,
        title_record=title_record,
    )


@app.route("/seasons/<int:season>")
def historical_season(season):
    if not logged_in():
        return redirect("/")

    conn = get_db()
    archive = conn.execute(
        "SELECT * FROM season_archives WHERE season = ?",
        (season,),
    ).fetchone()
    if not archive:
        conn.close()
        flash("That historical season is not available.", "error")
        return redirect("/seasons")

    table = conn.execute(
        """
        SELECT * FROM season_archive_players
        WHERE season = ?
        ORDER BY position
        """,
        (season,),
    ).fetchall()
    conn.close()

    records = {}
    for key in (
        "points", "exact_draws", "exact_scores",
        "correct_results", "dp_exact_scores",
    ):
        value = max((row[key] for row in table), default=0)
        records[key] = {
            "value": value,
            "players": [row for row in table if row[key] == value],
        }

    return render_template(
        "season_archive.html",
        archive=archive,
        table=table,
        records=records,
    )


@app.route("/stats")
def stats():
    if not logged_in():
        return redirect("/")

    conn = get_db()

    refresh_points(conn)
    archive_completed_season(conn, SEASON)
    conn.commit()

    personal = conn.execute(
        """
        SELECT
            COALESCE(SUM(p.points), 0)
                AS total_points,

            COUNT(p.id)
                AS predictions_made,

            COALESCE(
                SUM(
                    CASE
                    WHEN
                        p.home_score = f.home_score
                        AND p.away_score = f.away_score
                        AND f.home_score = f.away_score
                    THEN 1 ELSE 0
                    END
                ),
                0
            ) AS exact_draws,

            COALESCE(
                SUM(
                    CASE
                    WHEN
                        p.home_score = f.home_score
                        AND p.away_score = f.away_score
                        AND f.home_score != f.away_score
                    THEN 1 ELSE 0
                    END
                ),
                0
            ) AS exact_scores,

            COALESCE(
                SUM(
                    CASE
                    WHEN
                        NOT (
                            p.home_score = f.home_score
                            AND p.away_score = f.away_score
                        )
                        AND (
                            (
                                f.home_score = f.away_score
                                AND p.home_score = p.away_score
                            )
                            OR (
                                f.home_score > f.away_score
                                AND p.home_score > p.away_score
                            )
                            OR (
                                f.home_score < f.away_score
                                AND p.home_score < p.away_score
                            )
                        )
                    THEN 1 ELSE 0
                    END
                ),
                0
            ) AS correct_results,

            COALESCE(
                SUM(
                    CASE
                    WHEN COALESCE(p.dp, 0) = 1
                        AND p.home_score = f.home_score
                        AND p.away_score = f.away_score
                    THEN 1 ELSE 0
                    END
                ),
                0
            ) AS dp_exact_scores

        FROM predictions p
        JOIN fixtures f
          ON f.id = p.fixture_id

        WHERE p.player_id = ?
          AND f.status = 'FINISHED'
        """,
        (
            session["player_id"],
        ),
    ).fetchone()

    best_gameweek = conn.execute(
        """
        SELECT
            f.matchday,
            COALESCE(
                SUM(p.points),
                0
            ) AS points

        FROM predictions p

        JOIN fixtures f
          ON f.id = p.fixture_id

        WHERE p.player_id = ?
          AND f.status = 'FINISHED'

        GROUP BY f.matchday

        ORDER BY
            points DESC,
            f.matchday ASC

        LIMIT 1
        """,
        (
            session["player_id"],
        ),
    ).fetchone()

    # --------------------------------------------------------
    # Tie-aware league records
    # --------------------------------------------------------

    leader_rows = conn.execute(
        """
        SELECT
            pl.id,
            pl.name,
            COALESCE(
                SUM(p.points),
                0
            ) AS points
        FROM players pl
        LEFT JOIN predictions p
          ON p.player_id = pl.id
        GROUP BY pl.id
        ORDER BY
            points DESC,
            pl.name COLLATE NOCASE
        """
    ).fetchall()

    leader_value = (
        leader_rows[0]["points"]
        if leader_rows
        else 0
    )

    top_scorers = [
        row
        for row in leader_rows
        if row["points"] == leader_value
    ]

    exact_draw_rows = conn.execute(
        """
        SELECT
            pl.id,
            pl.name,
            COUNT(*) AS total
        FROM predictions p
        JOIN players pl
          ON pl.id = p.player_id
        JOIN fixtures f
          ON f.id = p.fixture_id
        WHERE f.status = 'FINISHED'
          AND p.home_score = f.home_score
          AND p.away_score = f.away_score
          AND f.home_score = f.away_score
        GROUP BY pl.id
        ORDER BY
            total DESC,
            pl.name COLLATE NOCASE
        """
    ).fetchall()

    exact_draw_value = (
        exact_draw_rows[0]["total"]
        if exact_draw_rows
        else 0
    )

    most_exact_draws = [
        row
        for row in exact_draw_rows
        if row["total"] == exact_draw_value
    ]

    exact_score_rows = conn.execute(
        """
        SELECT
            pl.id,
            pl.name,
            COUNT(*) AS total
        FROM predictions p
        JOIN players pl
          ON pl.id = p.player_id
        JOIN fixtures f
          ON f.id = p.fixture_id
        WHERE f.status = 'FINISHED'
          AND p.home_score = f.home_score
          AND p.away_score = f.away_score
          AND f.home_score != f.away_score
        GROUP BY pl.id
        ORDER BY
            total DESC,
            pl.name COLLATE NOCASE
        """
    ).fetchall()

    exact_score_value = (
        exact_score_rows[0]["total"]
        if exact_score_rows
        else 0
    )

    most_exact_scores = [
        row
        for row in exact_score_rows
        if row["total"] == exact_score_value
    ]

    correct_result_rows = conn.execute(
        """
        SELECT
            pl.id,
            pl.name,
            COUNT(*) AS total
        FROM predictions p
        JOIN players pl
          ON pl.id = p.player_id
        JOIN fixtures f
          ON f.id = p.fixture_id
        WHERE f.status = 'FINISHED'
          AND NOT (
              p.home_score = f.home_score
              AND p.away_score = f.away_score
          )
          AND (
              (f.home_score = f.away_score AND p.home_score = p.away_score)
              OR (f.home_score > f.away_score AND p.home_score > p.away_score)
              OR (f.home_score < f.away_score AND p.home_score < p.away_score)
          )
        GROUP BY pl.id
        ORDER BY
            total DESC,
            pl.name COLLATE NOCASE
        """
    ).fetchall()

    correct_result_value = (
        correct_result_rows[0]["total"]
        if correct_result_rows
        else 0
    )
    most_correct_results = [
        row
        for row in correct_result_rows
        if row["total"] == correct_result_value
    ]

    dp_exact_score_rows = conn.execute(
        """
        SELECT
            pl.id,
            pl.name,
            COUNT(*) AS total
        FROM predictions p
        JOIN players pl
          ON pl.id = p.player_id
        JOIN fixtures f
          ON f.id = p.fixture_id
        WHERE f.status = 'FINISHED'
          AND COALESCE(p.dp, 0) = 1
          AND p.home_score = f.home_score
          AND p.away_score = f.away_score
        GROUP BY pl.id
        ORDER BY
            total DESC,
            pl.name COLLATE NOCASE
        """
    ).fetchall()

    dp_exact_score_value = (
        dp_exact_score_rows[0]["total"]
        if dp_exact_score_rows
        else 0
    )
    most_dp_exact_scores = [
        row
        for row in dp_exact_score_rows
        if row["total"] == dp_exact_score_value
    ]

    best_gameweek_rows = conn.execute(
        """
        SELECT
            pl.id,
            pl.name,
            f.matchday,
            SUM(p.points) AS points
        FROM predictions p
        JOIN players pl
          ON pl.id = p.player_id
        JOIN fixtures f
          ON f.id = p.fixture_id
        WHERE f.status = 'FINISHED'
        GROUP BY
            pl.id,
            f.matchday
        ORDER BY
            points DESC,
            f.matchday ASC,
            pl.name COLLATE NOCASE
        """
    ).fetchall()

    best_gameweek_value = (
        best_gameweek_rows[0]["points"]
        if best_gameweek_rows
        else 0
    )

    best_gameweeks_overall = [
        row
        for row in best_gameweek_rows
        if row["points"] == best_gameweek_value
    ]

    completed_matches = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM fixtures
        WHERE season = ?
          AND status = 'FINISHED'
        """,
        (SEASON,),
    ).fetchone()

    conn.close()

    avg_points = 0

    if personal["predictions_made"]:
        avg_points = round(
            personal["total_points"]
            / personal["predictions_made"],
            2
        )

    return render_template(
        "stats.html",
        personal=personal,
        best_gameweek=best_gameweek,
        top_scorers=top_scorers,
        leader_value=leader_value,
        most_exact_draws=most_exact_draws,
        exact_draw_value=exact_draw_value,
        most_exact_scores=most_exact_scores,
        exact_score_value=exact_score_value,
        most_correct_results=most_correct_results,
        correct_result_value=correct_result_value,
        most_dp_exact_scores=most_dp_exact_scores,
        dp_exact_score_value=dp_exact_score_value,
        best_gameweeks_overall=best_gameweeks_overall,
        best_gameweek_value=best_gameweek_value,
        completed_matches=completed_matches["total"],
        avg_points=avg_points,
    )



@app.route("/dashboard")
def dashboard():
    if not logged_in():
        return redirect("/")

    conn = get_db()

    # Current GW = first gameweek that still has at least one unfinished fixture.
    current_row = conn.execute(
        """
        SELECT MIN(matchday) AS matchday
        FROM fixtures
        WHERE season = ?
          AND matchday IS NOT NULL
          AND status NOT IN ('FINISHED', 'CANCELLED')
        """,
        (SEASON,),
    ).fetchone()

    automatic_matchday = (
        current_row["matchday"]
        if current_row
        else None
    )

    # If the season is fully complete, show the final gameweek.
    if automatic_matchday is None:
        latest_row = conn.execute(
            """
            SELECT MAX(matchday) AS matchday
            FROM fixtures
            WHERE season = ?
              AND matchday IS NOT NULL
            """,
            (SEASON,),
        ).fetchone()

        automatic_matchday = (
            latest_row["matchday"]
            if latest_row
            else None
        )

    available_matchdays = [
        row["matchday"]
        for row in conn.execute(
            """
            SELECT DISTINCT matchday
            FROM fixtures
            WHERE season = ? AND matchday IS NOT NULL
            ORDER BY matchday
            """,
            (SEASON,),
        ).fetchall()
    ]
    current_matchday = automatic_matchday

    current_fixtures = []

    if current_matchday is not None:
        fixture_rows = conn.execute(
            """
            SELECT *
            FROM fixtures
            WHERE season = ?
              AND matchday = ?
            ORDER BY utc_date
            """,
            (
                SEASON,
                current_matchday
            ),
        ).fetchall()
        current_fixtures = []
        for row in fixture_rows:
            fixture = dict(row)
            fixture["scorers"] = fixture_scorers(
                fixture.get("goals_json"),
                fixture["home_team"],
                fixture["away_team"],
            )
            current_fixtures.append(fixture)

    row = conn.execute(
        """
        SELECT COALESCE(
            SUM(points),
            0
        ) AS total
        FROM predictions
        WHERE player_id = ?
        """,
        (
            session["player_id"],
        ),
    ).fetchone()

    # Current league position, using exactly the same ranking rules
    # as the Season Leaderboard.
    league_rows = conn.execute(
        """
        SELECT
            pl.id,
            pl.name,

            COALESCE(
                SUM(p.points),
                0
            ) AS points,

            COALESCE(
                SUM(
                    CASE
                    WHEN
                        f.status = 'FINISHED'
                        AND p.home_score = f.home_score
                        AND p.away_score = f.away_score
                        AND f.home_score = f.away_score
                    THEN 1 ELSE 0
                    END
                ),
                0
            ) AS exact_draws,

            COALESCE(
                SUM(
                    CASE
                    WHEN
                        f.status = 'FINISHED'
                        AND p.home_score = f.home_score
                        AND p.away_score = f.away_score
                        AND f.home_score != f.away_score
                    THEN 1 ELSE 0
                    END
                ),
                0
            ) AS exact_scores

        FROM players pl

        LEFT JOIN predictions p
          ON p.player_id = pl.id

        LEFT JOIN fixtures f
          ON f.id = p.fixture_id

        GROUP BY pl.id

        ORDER BY
            points DESC,
            exact_draws DESC,
            exact_scores DESC,
            pl.name COLLATE NOCASE
        """
    ).fetchall()

    league_position = None
    league_size = len(
        league_rows
    )

    for position, league_player in enumerate(
        league_rows,
        start=1
    ):
        if (
            league_player["id"]
            == session["player_id"]
        ):
            league_position = position
            break

    conn.close()

    return render_template(
        "dashboard.html",
        current_matchday=current_matchday,
        current_fixtures=current_fixtures,
        total_points=row["total"],
        league_position=league_position,
        league_size=league_size,
        dashboard_has_live_fixtures=any(
            fixture["status"] in ("LIVE", "IN_PLAY", "PAUSED")
            for fixture in current_fixtures
        ),
    )

@app.route("/history")
def history():
    if not logged_in():
        return redirect("/")

    conn = get_db()

    gameweeks = conn.execute(
        """
        SELECT
            matchday,
            COUNT(*) AS fixture_count,
            SUM(
                CASE
                WHEN status = 'FINISHED'
                THEN 1
                ELSE 0
                END
            ) AS finished_count
        FROM fixtures
        WHERE season = ?
          AND matchday IS NOT NULL
        GROUP BY matchday
        ORDER BY matchday DESC
        """,
        (SEASON,),
    ).fetchall()

    conn.close()

    return render_template(
        "history.html",
        gameweeks=gameweeks,
    )


@app.route(
    "/predict/<int:matchday>",
    methods=["GET", "POST"]
)
def predictions(matchday):
    if not logged_in():
        return redirect("/")

    conn = get_db()

    fixtures = conn.execute(
        """
        SELECT
            f.*,
            p.home_score AS predicted_home,
            p.away_score AS predicted_away,
            p.points,
            COALESCE(p.dp, 0) AS predicted_dp
        FROM fixtures f
        LEFT JOIN predictions p
          ON p.fixture_id = f.id
         AND p.player_id = ?
        WHERE f.season = ?
          AND f.matchday = ?
        ORDER BY f.utc_date
        """,
        (
            session["player_id"],
            SEASON,
            matchday
        ),
    ).fetchall()

    if not fixtures:
        conn.close()
        flash(
            "That gameweek does not exist.",
            "error"
        )
        return redirect("/dashboard")

    locked_dp_fixture_id = None

    for fixture in fixtures:
        if (
            fixture["predicted_dp"]
            and fixture_is_locked(fixture)
        ):
            locked_dp_fixture_id = fixture["id"]
            break

    if request.method == "POST":
        errors = []
        saved = 0
        locked_attempts = 0

        requested_dp_id = None
        requested_dp_raw = request.form.get(
            "dp_fixture_id",
            ""
        ).strip()

        if requested_dp_raw:
            try:
                requested_dp_id = int(
                    requested_dp_raw
                )
            except ValueError:
                errors.append(
                    "Invalid Double Points selection."
                )

        fixture_ids = {
            fixture["id"]
            for fixture in fixtures
        }

        if (
            requested_dp_id is not None
            and requested_dp_id not in fixture_ids
        ):
            errors.append(
                "Double Points selection is not part of this Gameweek."
            )
            requested_dp_id = None

        # Once the selected DP fixture kicks off, DP itself is locked
        # for the entire Gameweek and cannot be moved to a later match.
        if locked_dp_fixture_id is not None:
            requested_dp_id = locked_dp_fixture_id

        elif requested_dp_id is not None:
            selected_fixture = next(
                (
                    fixture
                    for fixture in fixtures
                    if fixture["id"] == requested_dp_id
                ),
                None
            )

            if (
                not selected_fixture
                or fixture_is_locked(
                    selected_fixture
                )
            ):
                errors.append(
                    "That match has already kicked off, so it cannot be selected for DP."
                )
                requested_dp_id = None

        for fixture in fixtures:
            if fixture_is_locked(fixture):
                if (
                    request.form.get(
                        f"home_{fixture['id']}"
                    ) is not None
                    or request.form.get(
                        f"away_{fixture['id']}"
                    ) is not None
                ):
                    locked_attempts += 1

                continue

            home_raw = request.form.get(
                f"home_{fixture['id']}",
                ""
            ).strip()

            away_raw = request.form.get(
                f"away_{fixture['id']}",
                ""
            ).strip()

            if (
                home_raw == ""
                and away_raw == ""
            ):
                continue

            if (
                home_raw == ""
                or away_raw == ""
            ):
                errors.append(
                    "Enter both scores for "
                    f"{fixture['home_team']} v "
                    f"{fixture['away_team']}."
                )
                continue

            try:
                home = int(home_raw)
                away = int(away_raw)

                if (
                    home < 0
                    or away < 0
                    or home > 30
                    or away > 30
                ):
                    raise ValueError

            except ValueError:
                errors.append(
                    "Invalid score for "
                    f"{fixture['home_team']} v "
                    f"{fixture['away_team']}."
                )
                continue

            # Re-check immediately before DB write.
            current_fixture = conn.execute(
                """
                SELECT *
                FROM fixtures
                WHERE id = ?
                """,
                (fixture["id"],)
            ).fetchone()

            if (
                not current_fixture
                or fixture_is_locked(
                    current_fixture
                )
            ):
                locked_attempts += 1
                continue

            conn.execute(
                """
                INSERT INTO predictions (
                    player_id,
                    fixture_id,
                    home_score,
                    away_score,
                    points,
                    updated_at,
                    dp
                )
                VALUES (?, ?, ?, ?, 0, ?, 0)

                ON CONFLICT(
                    player_id,
                    fixture_id
                )

                DO UPDATE SET
                    home_score =
                        excluded.home_score,
                    away_score =
                        excluded.away_score,
                    updated_at =
                        excluded.updated_at
                """,
                (
                    session["player_id"],
                    fixture["id"],
                    home,
                    away,
                    now_utc().isoformat(),
                ),
            )

            saved += 1

        # DP can be changed while its selected fixture is still open.
        # A locked DP is preserved and cannot be moved.
        if locked_dp_fixture_id is None:
            if requested_dp_id is not None:
                selected_prediction = conn.execute(
                    """
                    SELECT p.id
                    FROM predictions p
                    JOIN fixtures f
                      ON f.id = p.fixture_id
                    WHERE p.player_id = ?
                      AND p.fixture_id = ?
                      AND f.season = ?
                      AND f.matchday = ?
                    """,
                    (
                        session["player_id"],
                        requested_dp_id,
                        SEASON,
                        matchday
                    )
                ).fetchone()

                if selected_prediction:
                    conn.execute(
                        """
                        UPDATE predictions
                        SET dp = 0
                        WHERE player_id = ?
                          AND fixture_id IN (
                              SELECT id
                              FROM fixtures
                              WHERE season = ?
                                AND matchday = ?
                          )
                        """,
                        (
                            session["player_id"],
                            SEASON,
                            matchday
                        )
                    )

                    conn.execute(
                        """
                        UPDATE predictions
                        SET dp = 1
                        WHERE player_id = ?
                          AND fixture_id = ?
                        """,
                        (
                            session["player_id"],
                            requested_dp_id
                        )
                    )

                else:
                    errors.append(
                        "Enter and save a score for your DP match."
                    )

        conn.commit()

        for error in errors:
            flash(
                error,
                "error"
            )

        if locked_attempts:
            flash(
                f"{locked_attempts} fixture(s) were already at or past kick-off and were not changed.",
                "error"
            )

        if saved:
            flash(
                f"{saved} prediction(s) saved.",
                "success"
            )

        conn.close()

        return redirect(
            f"/predict/{matchday}"
        )

    fixture_stats = build_fixture_stats(
        conn,
        fixtures
    )

    conn.close()

    return render_template(
        "predictions.html",
        fixtures=fixtures,
        matchday=matchday,
        locked_dp_fixture_id=locked_dp_fixture_id,
        fixture_stats=fixture_stats,
    )


@app.route(
    "/gameweek/<int:matchday>"
)
def gameweek(matchday):
    if not logged_in():
        return redirect("/")

    conn = get_db()

    fixtures = conn.execute(
        """
        SELECT *
        FROM fixtures
        WHERE season = ?
          AND matchday = ?
        ORDER BY utc_date
        """,
        (SEASON, matchday),
    ).fetchall()

    if not fixtures:
        conn.close()
        flash("That gameweek does not exist.", "error")
        return redirect("/dashboard")

    players = conn.execute(
        """
        SELECT id, name
        FROM players
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()

    predictions = conn.execute(
        """
        SELECT
            p.player_id,
            p.fixture_id,
            p.home_score,
            p.away_score,
            COALESCE(p.dp, 0) AS dp
        FROM predictions p
        JOIN fixtures f ON f.id = p.fixture_id
        WHERE f.season = ?
          AND f.matchday = ?
        """,
        (SEASON, matchday),
    ).fetchall()

    conn.close()

    prediction_map = {
        (p["player_id"], p["fixture_id"]): p
        for p in predictions
    }

    reveal_map = {
        fixture["id"]: fixture_is_locked(fixture)
        for fixture in fixtures
    }


    fixture_players = {
        fixture["id"]: order_players_for_fixture(
            players,
            fixture,
            prediction_map,
            reveal_map[fixture["id"]],
        )
        for fixture in fixtures
    }

    live_table = []

    for player in players:
        provisional = 0
        finished = 0
        live = 0

        for fixture in fixtures:
            pred = prediction_map.get(
                (player["id"], fixture["id"])
            )

            if not pred:
                continue

            # Future fixtures stay hidden and never contribute live points.
            if not reveal_map[fixture["id"]]:
                continue

            if (
                fixture["home_score"] is None
                or fixture["away_score"] is None
            ):
                continue

            provisional += calculate_prediction_points(
                pred["home_score"],
                pred["away_score"],
                fixture["home_score"],
                fixture["away_score"],
                bool(pred["dp"]),
            )

            if fixture["status"] == "FINISHED":
                finished += 1
            elif fixture["status"] in ("IN_PLAY", "PAUSED"):
                live += 1

        live_table.append({
            "id": player["id"],
            "name": player["name"],
            "points": provisional,
            "finished_matches": finished,
            "live_matches": live,
        })

    live_table.sort(
        key=lambda x: (-x["points"], x["name"].lower())
    )

    # Compare the live table with the same GW using only fully
    # finished fixtures. This makes arrows show movement caused by
    # matches currently in play rather than unrelated season position.
    completed_only_table = []

    for player in players:
        completed_points = 0

        for fixture in fixtures:
            if fixture["status"] != "FINISHED":
                continue

            pred = prediction_map.get(
                (
                    player["id"],
                    fixture["id"]
                )
            )

            if (
                not pred
                or fixture["home_score"] is None
                or fixture["away_score"] is None
            ):
                continue

            completed_points += (
                calculate_prediction_points(
                    pred["home_score"],
                    pred["away_score"],
                    fixture["home_score"],
                    fixture["away_score"],
                    bool(pred["dp"]),
                )
            )

        completed_only_table.append({
            "id": player["id"],
            "name": player["name"],
            "points": completed_points,
        })

    completed_only_table.sort(
        key=lambda x: (
            -x["points"],
            x["name"].lower()
        )
    )

    baseline_positions = (
        ranking_positions(
            completed_only_table
        )
    )

    for position, player in enumerate(
        live_table,
        start=1
    ):
        player["position"] = position
        player["position_change"] = (
            table_position_change(
                position,
                baseline_positions.get(
                    player["id"]
                )
            )
        )

    return render_template(
        "gameweek.html",
        matchday=matchday,
        fixtures=fixtures,
        players=players,
        fixture_players=fixture_players,
        prediction_map=prediction_map,
        reveal_map=reveal_map,
        live_table=live_table,
    )


@app.route("/leaderboard")
def leaderboard():
    if not logged_in():
        return redirect("/")

    conn = get_db()

    refresh_points(conn)
    archive_completed_season(conn, SEASON)
    conn.commit()

    players = conn.execute(
        """
        SELECT
            pl.id,
            pl.name,

            COALESCE(
                SUM(p.points),
                0
            ) AS points,

            COALESCE(
                SUM(
                    CASE
                    WHEN
                        f.status = 'FINISHED'
                        AND p.home_score = f.home_score
                        AND p.away_score = f.away_score
                        AND f.home_score = f.away_score
                    THEN 1 ELSE 0
                    END
                ),
                0
            ) AS exact_draws,

            COALESCE(
                SUM(
                    CASE
                    WHEN
                        f.status = 'FINISHED'
                        AND p.home_score = f.home_score
                        AND p.away_score = f.away_score
                        AND f.home_score != f.away_score
                    THEN 1 ELSE 0
                    END
                ),
                0
            ) AS exact_scores,

            COALESCE(
                SUM(
                    CASE
                    WHEN
                        f.status = 'FINISHED'
                        AND NOT (
                            p.home_score = f.home_score
                            AND p.away_score = f.away_score
                        )
                        AND (
                            (
                                f.home_score = f.away_score
                                AND p.home_score = p.away_score
                            )
                            OR (
                                f.home_score > f.away_score
                                AND p.home_score > p.away_score
                            )
                            OR (
                                f.home_score < f.away_score
                                AND p.home_score < p.away_score
                            )
                        )
                    THEN 1 ELSE 0
                    END
                ),
                0
            ) AS correct_results

        FROM players pl
        LEFT JOIN predictions p
          ON p.player_id = pl.id
        LEFT JOIN fixtures f
          ON f.id = p.fixture_id

        GROUP BY pl.id

        ORDER BY
            points DESC,
            exact_draws DESC,
            exact_scores DESC,
            pl.name COLLATE NOCASE
        """
    ).fetchall()

    # Establish the season-table baseline.
    # During a live/partial GW this is the table at the end of the
    # previous GW. Between GWs it is the table one completed GW back.
    unfinished_row = conn.execute(
        """
        SELECT MIN(matchday) AS matchday
        FROM fixtures
        WHERE season = ?
          AND status NOT IN (
              'FINISHED',
              'CANCELLED'
          )
        """,
        (SEASON,),
    ).fetchone()

    unfinished_matchday = (
        unfinished_row["matchday"]
        if unfinished_row
        and unfinished_row["matchday"]
        is not None
        else None
    )

    if unfinished_matchday is not None:
        baseline_matchday = max(
            0,
            unfinished_matchday - 1
        )
    else:
        completed_row = conn.execute(
            """
            SELECT MAX(matchday) AS matchday
            FROM fixtures
            WHERE season = ?
              AND matchday IN (
                  SELECT matchday
                  FROM fixtures
                  WHERE season = ?
                  GROUP BY matchday
                  HAVING SUM(
                      CASE
                      WHEN status NOT IN (
                          'FINISHED',
                          'CANCELLED'
                      )
                      THEN 1 ELSE 0
                      END
                  ) = 0
              )
            """,
            (
                SEASON,
                SEASON
            ),
        ).fetchone()

        latest_completed = (
            completed_row["matchday"]
            if completed_row
            and completed_row["matchday"]
            is not None
            else 0
        )

        baseline_matchday = max(
            0,
            latest_completed - 1
        )

    previous_table = overall_table_at_matchday(
        conn,
        baseline_matchday
    )

    previous_positions = ranking_positions(
        previous_table
    )

    players = [
        dict(player)
        for player in players
    ]

    for position, player in enumerate(
        players,
        start=1
    ):
        player["position"] = position
        player["position_change"] = (
            table_position_change(
                position,
                previous_positions.get(
                    player["id"]
                )
            )
        )

    completed_matchdays = [
        row["matchday"]
        for row in conn.execute(
            """
            SELECT matchday
            FROM fixtures
            WHERE season = ? AND matchday IS NOT NULL
            GROUP BY matchday
            HAVING SUM(
                CASE WHEN status NOT IN ('FINISHED', 'CANCELLED')
                     THEN 1 ELSE 0 END
            ) = 0
            ORDER BY matchday
            """,
            (SEASON,),
        ).fetchall()
    ]
    chart_players = {
        player["id"]: {"name": player["name"], "positions": []}
        for player in players
    }
    for matchday in completed_matchdays:
        positions = ranking_positions(overall_table_at_matchday(conn, matchday))
        for player_id, series in chart_players.items():
            series["positions"].append(positions.get(player_id))

    conn.close()

    return render_template(
        "leaderboard.html",
        players=players,
        position_chart={
            "matchdays": completed_matchdays,
            "players": list(chart_players.values()),
        },
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()

    return redirect("/")



@app.route("/admin/signal", methods=["GET", "POST"])
def admin_signal():
    if not is_admin():
        return redirect("/")

    if request.method == "POST":
        api_url = request.form.get(
            "api_url",
            ""
        ).strip().rstrip("/")

        number = request.form.get(
            "number",
            ""
        ).strip()

        group_id = request.form.get(
            "group_id",
            ""
        ).strip()

        group_name = request.form.get(
            "group_name",
            ""
        ).strip()

        enabled = (
            "1"
            if request.form.get("enabled") == "1"
            else "0"
        )

        notify_gw_open = (
            "1"
            if request.form.get("notify_gw_open") == "1"
            else "0"
        )

        notify_reminder = (
            "1"
            if request.form.get("notify_reminder") == "1"
            else "0"
        )

        notify_results = (
            "1"
            if request.form.get("notify_results") == "1"
            else "0"
        )

        if not (
            api_url.startswith("http://")
            or api_url.startswith("https://")
        ):
            flash(
                "Signal API URL must start with http:// or https://.",
                "error"
            )
            return redirect("/admin/signal")

        if not number.startswith("+"):
            flash(
                "Signal number must use international format, e.g. +44...",
                "error"
            )
            return redirect("/admin/signal")

        if not group_id.startswith("group."):
            flash(
                "Signal group ID must start with group.",
                "error"
            )
            return redirect("/admin/signal")

        set_setting("signal_api_url", api_url)
        set_setting("signal_number", number)
        set_setting("signal_group_id", group_id)
        set_setting("signal_group_name", group_name)
        set_setting("signal_enabled", enabled)
        set_setting("signal_notify_gw_open", notify_gw_open)
        set_setting("signal_notify_reminder", notify_reminder)
        set_setting("signal_notify_results", notify_results)

        flash(
            "Signal settings saved.",
            "success"
        )

        return redirect("/admin/signal")

    return render_template(
        "signal.html",
        signal=signal_settings(),
        signal_status=signal_connection_status(),
        last_notification_error=get_setting(
            "signal_last_notification_error"
        ),
    )



@app.route("/admin/signal/send-open", methods=["POST"])
def admin_signal_send_open():
    if not is_admin():
        return redirect("/")

    conn = get_db()

    try:
        matchday = signal_current_gameweek(conn)

        if matchday is None:
            flash("No current Gameweek found.", "error")
        else:
            fixtures = signal_gameweek_fixtures(
                conn,
                matchday
            )

            message = signal_gw_open_message(
                matchday,
                fixtures
            )

            if message:
                send_signal_message(message)
                set_setting(
                    "signal_last_open_gw",
                    str(matchday)
                )
                flash(
                    f"GW{matchday} open message sent.",
                    "success"
                )
            else:
                flash("No fixtures found.", "error")

    except Exception as exc:
        flash(
            f"Signal message failed: {exc}",
            "error"
        )

    finally:
        conn.close()

    return redirect("/admin/signal")


@app.route("/admin/signal/send-reminder", methods=["POST"])
def admin_signal_send_reminder():
    if not is_admin():
        return redirect("/")

    conn = get_db()

    try:
        matchday = signal_current_gameweek(conn)

        if matchday is None:
            flash("No current Gameweek found.", "error")
        else:
            fixtures = signal_gameweek_fixtures(
                conn,
                matchday
            )

            statuses = signal_submission_status(
                conn,
                matchday
            )

            message = signal_reminder_message(
                matchday,
                fixtures,
                statuses,
                reminder_label="manual preview",
                include_missing_dp=True
            )

            if message:
                send_signal_message(message)
                reminder_key = signal_manual_reminder_key(
                    fixtures
                )
                if reminder_key:
                    set_setting(
                        reminder_key,
                        str(matchday)
                    )
                flash(
                    f"GW{matchday} reminder sent.",
                    "success"
                )
            else:
                flash(
                    "Everyone has completed their predictions.",
                    "success"
                )

    except Exception as exc:
        flash(
            f"Signal reminder failed: {exc}",
            "error"
        )

    finally:
        conn.close()

    return redirect("/admin/signal")


@app.route("/admin/signal/send-results", methods=["POST"])
def admin_signal_send_results():
    if not is_admin():
        return redirect("/")

    conn = get_db()

    try:
        matchday = signal_latest_completed_gameweek(
            conn
        )

        if matchday is None:
            flash(
                "No completed Gameweek found.",
                "error"
            )
        else:
            send_signal_message(
                signal_results_message(
                    matchday,
                    signal_gw_table(
                        conn,
                        matchday
                    ),
                    signal_overall_table(
                        conn
                    )
                )
            )
            set_setting(
                "signal_last_results_gw",
                str(matchday)
            )

            flash(
                f"GW{matchday} results sent.",
                "success"
            )

    except Exception as exc:
        flash(
            f"Signal results failed: {exc}",
            "error"
        )

    finally:
        conn.close()

    return redirect("/admin/signal")


@app.route("/admin/signal/test", methods=["POST"])
def admin_signal_test():
    if not is_admin():
        return redirect("/")
    try:
        send_signal_message("⚽ Premier League Predictor\n\nSignal integration is working! ✅")
        set_setting("last_signal_test", now_utc().isoformat())
        set_setting("last_signal_error", "")
        flash("Signal test message sent successfully.", "success")
    except Exception as exc:
        set_setting("last_signal_error", str(exc))
        flash(f"Signal test failed: {exc}", "error")
    return redirect("/admin/signal")


@app.route("/admin")
def admin():
    if not is_admin():
        return redirect("/")

    conn = get_db()

    player_count = conn.execute(
        "SELECT COUNT(*) FROM players"
    ).fetchone()[0]

    fixture_count = conn.execute(
        "SELECT COUNT(*) FROM fixtures"
    ).fetchone()[0]

    prediction_count = conn.execute(
        "SELECT COUNT(*) FROM predictions"
    ).fetchone()[0]

    conn.close()

    last_api_refresh = get_setting(
        "last_api_refresh"
    )

    last_auto_backup = get_setting(
        "last_auto_backup"
    )

    last_api_error = get_setting(
        "last_api_error"
    )

    last_api_error_at = get_setting(
        "last_api_error_at"
    )

    historical_results_last_refresh = get_setting(
        "historical_results_last_refresh"
    )

    historical_results_last_sources = get_setting(
        "historical_results_last_sources"
    )

    historical_results_last_error = get_setting(
        "historical_results_last_error"
    )

    signal = signal_settings()
    signal_status = signal_connection_status()

    return render_template(
        "admin.html",
        player_count=player_count,
        fixture_count=fixture_count,
        prediction_count=prediction_count,
        signal=signal,
        signal_status=signal_status,
        last_api_refresh=(
            local_timestamp(
                last_api_refresh
            )
            if last_api_refresh
            else None
        ),
        last_auto_backup=(
            local_timestamp(
                last_auto_backup
            )
            if last_auto_backup
            else None
        ),
        historical_results_last_refresh=(
            local_timestamp(
                historical_results_last_refresh
            )
            if historical_results_last_refresh
            else None
        ),
        historical_results_last_sources=(
            historical_results_last_sources
        ),
        historical_results_last_error=(
            historical_results_last_error
        ),
        last_api_error=last_api_error,
        last_api_error_at=(
            local_timestamp(
                last_api_error_at
            )
            if last_api_error_at
            else None
        ),
    )



@app.route(
    "/admin/google/connect",
    methods=["POST"]
)
def google_connect():
    if not is_admin():
        return redirect("/")

    client_id = request.form.get(
        "google_client_id",
        ""
    ).strip()

    client_secret = request.form.get(
        "google_client_secret",
        ""
    ).strip()

    public_base_url = request.form.get(
        "public_base_url",
        ""
    ).strip().rstrip("/")

    if (
        not client_id
        or not client_secret
        or not public_base_url
    ):
        flash(
            "Client ID, Client Secret "
            "and Public Base URL are required.",
            "error"
        )
        return redirect(
            "/admin/backup"
        )

    set_setting(
        "google_client_id",
        client_id
    )

    set_setting(
        "google_client_secret",
        client_secret
    )

    set_setting(
        "public_base_url",
        public_base_url
    )

    config = google_client_config()

    # Google OAuth is using PKCE. Generate the verifier here and persist it
    # because it must be sent back during the token exchange after Google
    # redirects the browser to our callback URL.
    flow = Flow.from_client_config(
        config,
        scopes=[GOOGLE_DRIVE_SCOPE],
        autogenerate_code_verifier=True
    )

    flow.redirect_uri = (
        google_redirect_uri()
    )

    authorization_url, state = (
        flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"
        )
    )

    session[
        "google_oauth_state"
    ] = state

    set_setting(
        "google_oauth_state",
        state
    )

    code_verifier = getattr(
        flow,
        "code_verifier",
        None
    )

    if not code_verifier:
        flash(
            "Google OAuth PKCE verifier "
            "could not be generated.",
            "error"
        )
        return redirect(
            "/admin/backup"
        )

    set_setting(
        "google_oauth_code_verifier",
        code_verifier
    )

    print(
        "[google-drive] Starting OAuth. "
        "PKCE verifier stored.",
        flush=True
    )

    return redirect(
        authorization_url
    )


@app.route(
    "/admin/google/callback"
)
def google_callback():
    def callback_page(title, message, success, status=200):
        icon = "✅" if success else "❌"
        button_url = "/admin/backup" if success else "/"
        button_text = "Continue to Backup & Restore" if success else "Return to Predictor"
        bg = "#dcfce7" if success else "#fee2e2"
        fg = "#166534" if success else "#991b1b"

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>{title}</title>
<style>
body {{ font-family: Arial, sans-serif; background:#eef2f7; margin:0; color:#0f172a; }}
.wrap {{ max-width:680px; margin:60px auto; padding:20px; }}
.card {{ background:white; border-radius:18px; padding:28px; box-shadow:0 10px 30px rgba(15,23,42,.10); }}
.msg {{ background:{bg}; color:{fg}; padding:14px; border-radius:12px; white-space:pre-wrap; word-break:break-word; }}
a {{ display:inline-block; margin-top:18px; background:#0f172a; color:white; text-decoration:none; padding:12px 16px; border-radius:10px; font-weight:bold; }}
small {{ color:#64748b; }}
</style>
</head>
<body>
<div class=\"wrap\"><div class=\"card\">
<div style=\"font-size:48px\">{icon}</div>
<h1>{title}</h1>
<div class=\"msg\">{message}</div>
<p><small>Premier League Predictor v{APP_VERSION}</small></p>
<a href=\"{button_url}\">{button_text}</a>
</div></div>
</body>
</html>"""

        return Response(html, status=status, mimetype="text/html")

    try:
        returned_state = request.args.get("state", "")
        expected_state = get_setting("google_oauth_state") or session.get("google_oauth_state", "")

        print(
            f"[google-drive] Callback reached. Returned state={returned_state}, Expected state={expected_state}",
            flush=True
        )

        if not returned_state or not expected_state or returned_state != expected_state:
            message = (
                "OAuth state was missing or did not match. "
                f"Expected: {expected_state or '<none>'}. "
                f"Returned: {returned_state or '<none>'}."
            )
            return callback_page("Google Drive connection failed", message, False, 400)

        error = request.args.get("error")
        if error:
            description = request.args.get("error_description", error)
            return callback_page("Google Drive connection failed", f"Google returned: {description}", False, 400)

        code = request.args.get("code", "")
        if not code:
            return callback_page("Google Drive connection failed", "Google did not return an authorization code.", False, 400)

        client_id = get_setting("google_client_id")
        client_secret = get_setting("google_client_secret")
        redirect_uri = google_redirect_uri()

        if not client_id or not client_secret:
            return callback_page("Google Drive connection failed", "Google Client ID or Client Secret is missing.", False, 500)

        print("[google-drive] Exchanging authorization code for token.", flush=True)

        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": get_setting(
                    "google_oauth_code_verifier"
                ),
            },
            timeout=20,
        )

        if token_response.status_code != 200:
            message = (
                f"Google token exchange failed. HTTP {token_response.status_code}. "
                + token_response.text[:1000]
            )
            return callback_page("Google Drive connection failed", message, False, 500)

        token_data = token_response.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")

        if not access_token:
            return callback_page("Google Drive connection failed", "Google's token response did not contain an access token.", False, 500)

        credentials = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=[GOOGLE_DRIVE_SCOPE],
        )

        save_google_credentials(credentials)
        set_setting("google_oauth_state", "")
        set_setting("google_oauth_code_verifier", "")
        session.pop("google_oauth_state", None)
        set_setting("last_google_backup_error", "")

        print("[google-drive] Token saved successfully.", flush=True)

        return callback_page(
            "Google Drive connected",
            "Authorization completed successfully. Your Google credentials were saved. Return to Backup & Restore and use Back up to Drive now to test the upload.",
            True,
            200
        )

    except Exception as e:
        error_text = f"{type(e).__name__}: {e}"
        set_setting("last_google_backup_error", error_text)
        print(f"[google-drive] Callback exception: {error_text}", flush=True)
        return callback_page("Google Drive connection failed", error_text, False, 500)

@app.route(
    "/admin/google/disconnect",
    methods=["POST"]
)
def google_disconnect():
    if not is_admin():
        return redirect("/")

    if os.path.exists(
        GOOGLE_TOKEN_FILE
    ):
        os.remove(
            GOOGLE_TOKEN_FILE
        )

    set_setting(
        "google_drive_folder_id",
        ""
    )

    set_setting(
        "last_google_backup_error",
        ""
    )

    flash(
        "Google Drive disconnected.",
        "success"
    )

    return redirect(
        "/admin/backup"
    )


@app.route(
    "/admin/backup/google-now",
    methods=["POST"]
)
def google_backup_now():
    if not is_admin():
        return redirect("/")

    try:
        backup_path = (
            create_automatic_backup()
        )

        upload_backup_to_google_drive(
            backup_path
        )

        flash(
            "Backup created and uploaded "
            "to Google Drive.",
            "success"
        )

    except Exception as e:
        set_setting(
            "last_google_backup_error",
            str(e)
        )

        flash(
            f"Google Drive backup failed: {e}",
            "error"
        )

    return redirect(
        "/admin/backup"
    )


@app.route("/admin/backup")
def backup_page():
    if not is_admin():
        return redirect("/")

    backups = []

    if os.path.exists(
        BACKUP_DIR
    ):
        for name in sorted(
            os.listdir(
                BACKUP_DIR
            ),
            reverse=True
        ):
            path = os.path.join(
                BACKUP_DIR,
                name
            )

            if (
                os.path.isfile(path)
                and name.endswith(".db")
            ):
                stat = os.stat(path)

                backups.append({
                    "name": name,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc
                    ).astimezone(
                        UK
                    ).strftime(
                        "%d %b %Y %H:%M"
                    )
                })

    last_auto = get_setting(
        "last_auto_backup"
    )

    last_google = get_setting(
        "last_google_backup"
    )

    google_error = get_setting(
        "last_google_backup_error"
    )

    return render_template(
        "backup.html",
        backups=backups,
        last_auto_backup=(
            local_timestamp(last_auto)
            if last_auto
            else None
        ),
        google_connected=google_drive_connected(),
        last_google_backup=(
            local_timestamp(last_google)
            if last_google
            else None
        ),
        google_backup_error=google_error,
        google_client_id=(
            get_setting("google_client_id")
            or ""
        ),
        public_base_url=(
            get_setting("public_base_url")
            or "https://battleship.live"
        ),
        google_redirect_uri=google_redirect_uri(),
        local_backup_limit=MAX_AUTO_BACKUPS,
        cloud_retention_days=GOOGLE_RETENTION_DAYS,
    )

@app.route(
    "/admin/backup/download"
)
def download_backup():
    if not is_admin():
        return redirect("/")

    (
        backup_path,
        backup_name
    ) = create_database_backup()

    return send_file(
        backup_path,
        as_attachment=True,
        download_name=backup_name,
        mimetype="application/octet-stream"
    )


@app.route(
    "/admin/backup/download/<path:filename>"
)
def download_existing_backup(
    filename
):
    if not is_admin():
        return redirect("/")

    safe_name = secure_filename(
        filename
    )

    path = os.path.join(
        BACKUP_DIR,
        safe_name
    )

    if not os.path.exists(path):
        flash(
            "Backup file not found.",
            "error"
        )

        return redirect(
            "/admin/backup"
        )

    return send_file(
        path,
        as_attachment=True,
        download_name=safe_name,
        mimetype="application/octet-stream"
    )


@app.route(
    "/admin/backup/restore",
    methods=["POST"]
)
def restore_backup():
    if not is_admin():
        return redirect("/")

    uploaded = request.files.get(
        "backup_file"
    )

    if (
        not uploaded
        or uploaded.filename == ""
    ):
        flash(
            "Choose a backup file first.",
            "error"
        )

        return redirect(
            "/admin/backup"
        )

    filename = secure_filename(
        uploaded.filename
    )

    if not filename.lower().endswith(
        ".db"
    ):
        flash(
            "Backup must be a .db file.",
            "error"
        )

        return redirect(
            "/admin/backup"
        )

    temp_path = os.path.join(
        UPLOAD_DIR,
        "restore-"
        + secrets.token_hex(8)
        + ".db"
    )

    uploaded.save(
        temp_path
    )

    try:
        validate_restore_database(
            temp_path
        )

        create_database_backup()

        install_database(temp_path, DB)

        init_db(seed_default_player=False)

        session.clear()

        flash(
            "Backup restored successfully. "
            "Please log in again.",
            "success"
        )

        return redirect("/")

    except Exception as e:
        flash(
            f"Restore failed: {e}",
            "error"
        )

        return redirect(
            "/admin/backup"
        )

    finally:
        if os.path.exists(
            temp_path
        ):
            os.remove(
                temp_path
            )


@app.route("/admin/players")
def players():
    if not is_admin():
        return redirect("/")

    conn = get_db()

    players = conn.execute(
        """
        SELECT
            id,
            name,
            admin
        FROM players
        ORDER BY name
        """
    ).fetchall()

    conn.close()

    return render_template(
        "players.html",
        players=players
    )


@app.route(
    "/admin/players/add",
    methods=["POST"]
)
def add_player():
    if not is_admin():
        return redirect("/")

    name = request.form.get(
        "name",
        ""
    ).strip()
    email = request.form.get("email", "").strip().casefold()

    pin = request.form.get(
        "pin",
        ""
    ).strip()

    if (
        not name
        or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
        or not pin
    ):
        flash(
            "A valid email, display name and PIN are required.",
            "error"
        )

        return redirect(
            "/admin/players"
        )

    if (
        not pin.isdigit()
        or not 4 <= len(pin) <= 8
    ):
        flash(
            "PIN must contain "
            "4 to 8 digits.",
            "error"
        )

        return redirect(
            "/admin/players"
        )

    conn = get_db()

    try:
        conn.execute(
            """
            INSERT INTO players(
                name,
                login_name,
                email,
                pin_hash,
                admin
            )
            VALUES (?, ?, ?, ?, 0)
            """,
            (
                name,
                name,
                email,
                hash_pin(pin)
            ),
        )

        conn.commit()

        flash(
            f"{name} added.",
            "success"
        )

    except Exception:
        flash(
            "A player with that "
            "name already exists.",
            "error"
        )

    finally:
        conn.close()

    return redirect(
        "/admin/players"
    )



@app.route(
    "/admin/players/edit/<int:player_id>",
    methods=["GET", "POST"]
)
def edit_player(player_id):
    if not is_admin():
        return redirect("/")

    conn = get_db()

    player = conn.execute(
        """
        SELECT id, name, email, admin
        FROM players
        WHERE id = ?
        """,
        (player_id,)
    ).fetchone()

    if not player:
        conn.close()
        flash(
            "Player not found.",
            "error"
        )
        return redirect(
            "/admin/players"
        )

    if request.method == "POST":
        name = request.form.get(
            "name",
            ""
        ).strip()
        email = request.form.get("email", "").strip().casefold()

        pin = request.form.get(
            "pin",
            ""
        ).strip()

        admin_value = (
            1
            if request.form.get("admin") == "1"
            else 0
        )

        if len(name) < 2 or len(name) > 30:
            conn.close()
            flash(
                "Name must be between 2 and 30 characters.",
                "error"
            )

        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            conn.close()
            flash("Enter a valid email address.", "error")
            return redirect(f"/admin/players/edit/{player_id}")
            return redirect(
                f"/admin/players/edit/{player_id}"
            )

        if pin and (
            not pin.isdigit()
            or not 4 <= len(pin) <= 8
        ):
            conn.close()
            flash(
                "PIN must contain 4 to 8 digits.",
                "error"
            )
            return redirect(
                f"/admin/players/edit/{player_id}"
            )

        # Do not allow the logged-in admin to remove their own admin role.
        if player_id == session["player_id"]:
            admin_value = 1

        duplicate = conn.execute(
            """
            SELECT id
            FROM players
            WHERE LOWER(name) = LOWER(?)
              AND id != ?
            """,
            (
                name,
                player_id
            )
        ).fetchone()

        if duplicate:
            conn.close()
            flash(
                "Another player already uses that name.",
                "error"
            )

        duplicate_email = conn.execute(
            "SELECT id FROM players WHERE LOWER(email)=LOWER(?) AND id != ?",
            (email, player_id),
        ).fetchone()
        if duplicate_email:
            conn.close()
            flash("Another player already uses that email address.", "error")
            return redirect(f"/admin/players/edit/{player_id}")
            return redirect(
                f"/admin/players/edit/{player_id}"
            )

        if pin:
            conn.execute(
                """
                UPDATE players
                SET
                    name = ?,
                    email = ?,
                    pin_hash = ?,
                    admin = ?
                WHERE id = ?
                """,
                (
                    name,
                    email,
                    hash_pin(pin),
                    admin_value,
                    player_id
                )
            )
        else:
            conn.execute(
                """
                UPDATE players
                SET
                    name = ?,
                    email = ?,
                    admin = ?
                WHERE id = ?
                """,
                (
                    name,
                    email,
                    admin_value,
                    player_id
                )
            )

        conn.commit()
        conn.close()

        # Keep current session display name in sync.
        if player_id == session["player_id"]:
            session["player_name"] = name
            session["admin"] = True

        flash(
            f"{name} updated.",
            "success"
        )

        return redirect(
            "/admin/players"
        )

    conn.close()

    return render_template(
        "edit_player.html",
        player=player
    )


@app.route(
    "/admin/players/delete/<int:player_id>",
    methods=["POST"]
)
def delete_player(player_id):
    if not is_admin():
        return redirect("/")

    if (
        player_id
        == session["player_id"]
    ):
        flash(
            "You cannot delete yourself.",
            "error"
        )

        return redirect(
            "/admin/players"
        )

    conn = get_db()

    conn.execute(
        """
        DELETE FROM predictions
        WHERE player_id = ?
        """,
        (player_id,)
    )

    conn.execute(
        """
        DELETE FROM players
        WHERE id = ?
        """,
        (player_id,)
    )

    conn.commit()
    conn.close()

    flash(
        "Player deleted.",
        "success"
    )

    return redirect(
        "/admin/players"
    )


@app.route(
    "/admin/settings",
    methods=["GET", "POST"]
)
def settings():
    if not is_admin():
        return redirect("/")

    if request.method == "POST":
        action = request.form.get(
            "action",
            "api"
        )

        if action == "registration":
            enabled = (
                "1"
                if request.form.get(
                    "registration_enabled"
                ) == "1"
                else "0"
            )

            set_setting(
                "registration_enabled",
                enabled
            )

            flash(
                "Player registration is now enabled."
                if enabled == "1"
                else
                "Player registration is now disabled.",
                "success"
            )

            return redirect(
                "/admin/settings"
            )

        token = request.form.get(
            "api_token",
            ""
        ).strip()

        if token:
            set_setting(
                "football_api_token",
                token
            )

            flash(
                "API token saved. "
                "It will be kept across "
                "future app updates.",
                "success"
            )

        else:
            flash(
                "Please enter an API token.",
                "error"
            )

        return redirect(
            "/admin/settings"
        )

    return render_template(
        "settings.html",
        configured=bool(
            get_setting(
                "football_api_token"
            )
        ),
        last_sportscore_refresh=(
            local_timestamp(get_setting("last_sportscore_refresh"))
            if get_setting("last_sportscore_refresh")
            else None
        ),
        registration_enabled=(
            get_setting(
                "registration_enabled"
            ) == "1"
        ),
        last_api_refresh=(
            local_timestamp(
                get_setting(
                    "last_api_refresh"
                )
            )
            if get_setting(
                "last_api_refresh"
            )
            else None
        ),
    )


@app.route(
    "/admin/settings/test",
    methods=["POST"]
)
def test_api():
    if not is_admin():
        return redirect("/")

    token = get_setting(
        "football_api_token"
    )

    try:
        data = test_connection(
            token
        )

        flash(
            "Connection successful — "
            f"{data.get('name', 'Premier League')} "
            "API is working.",
            "success",
        )

    except FootballAPIError as e:
        flash(
            str(e),
            "error"
        )

    return redirect(
        "/admin/settings"
    )








@app.route("/admin/sportscore/scorers", methods=["POST"])
def admin_refresh_current_scorers():
    if not is_admin():
        return redirect("/")

    try:
        updated = import_live_matches_from_sportscore(
            force_current_gameweek=True
        )
        flash(
            f"SportScore checked the current gameweek and updated "
            f"{updated} fixture(s).",
            "success",
        )
    except Exception as exc:
        flash(f"SportScore scorer refresh failed: {exc}", "error")

    return redirect("/admin")


@app.route(
    "/admin/match-stats/refresh",
    methods=["POST"]
)
def admin_refresh_match_stats():
    if not is_admin():
        return redirect("/")

    try:
        imported = import_historical_results()

        flash(
            f"Match Stats history refreshed: "
            f"{imported} completed match result(s) processed.",
            "success"
        )

    except Exception as exc:
        flash(
            f"Historical match import failed: {exc}",
            "error"
        )

    return redirect("/admin")


@app.route("/admin/fixtures")
def fixtures():
    if not is_admin():
        return redirect("/")

    conn = get_db()

    fixtures = conn.execute(
        """
        SELECT *
        FROM fixtures
        WHERE season = ?
        ORDER BY
            matchday,
            utc_date
        """,
        (SEASON,),
    ).fetchall()

    conn.close()

    return render_template(
        "fixtures.html",
        fixtures=fixtures
    )


@app.route(
    "/admin/fixtures/import",
    methods=["POST"]
)
def import_fixtures():
    if not is_admin():
        return redirect("/")

    if not get_setting(
        "football_api_token"
    ):
        flash(
            "Configure the football "
            "API token first.",
            "error"
        )

        return redirect(
            "/admin/settings"
        )

    try:
        imported = import_matches_from_api()

        flash(
            "Successfully imported/updated "
            f"{imported} fixtures.",
            "success"
        )

    except FootballAPIError as e:
        flash(
            str(e),
            "error"
        )

    except Exception as e:
        flash(
            f"Import failed: {e}",
            "error"
        )

    return redirect(
        "/admin/fixtures"
    )


@app.route(
    "/admin/fixtures/tv",
    methods=["POST"]
)
def refresh_fixture_tv():
    if not is_admin():
        return redirect("/")

    conn = get_db()

    try:
        updated = refresh_tv_broadcasters(
            conn
        )
        conn.commit()

        flash(
            f"TV listings refreshed. {updated} fixture(s) updated.",
            "success"
        )

    except Exception as exc:
        flash(
            f"TV listings refresh failed: {exc}",
            "error"
        )

    finally:
        conn.close()

    return redirect(
        "/admin/fixtures"
    )


if __name__ == "__main__":

    threading.Thread(
        target=api_refresh_worker,
        daemon=True
    ).start()

    threading.Thread(
        target=auto_backup_worker,
        daemon=True
    ).start()

    threading.Thread(
        target=signal_notification_worker,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=8099
    )
