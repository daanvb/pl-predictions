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
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlparse
from html.parser import HTMLParser
from html import unescape

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from database import (
    DB,
    append_prediction_audit_event,
    get_db,
    get_setting,
    harden_path_permissions,
    hash_pin,
    init_db,
    is_legacy_pin_hash,
    set_setting,
    verify_prediction_audit_chain,
    verify_pin,
)
from database_restore import (
    database_has_users,
    install_database,
    validate_predictor_database,
)
from football_api import (
    FootballAPIError,
    get_champions_league_matches as get_football_champions_league_matches,
    get_match,
    get_matches,
    test_connection,
)
from sportscore import (
    SportScoreError,
    get_champions_league_matches as get_sportscore_champions_league_matches,
    get_match_details as get_sportscore_match_details,
    get_team_logo as get_sportscore_team_logo,
    goal_events as sportscore_goal_events,
)
from scoring import calculate_points, calculate_prediction_points
from bigballs_api import (
    BigBallsAPIError,
    get_match_events as get_bigballs_match_events,
    get_premier_league_matches as get_bigballs_premier_league_matches,
    get_stored_premier_league_matches as get_bigballs_stored_premier_league_matches,
    normalize_match as normalize_bigballs_match,
    test_connection as test_bigballs_connection,
)

APP_VERSION = "1.2.7"
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
GOOGLE_BACKUP_LIMIT = 10
SPORTSCORE_TEAM_LOGO_FALLBACKS = {
    "arsenal": (
        "https://img.thesports.com/football/team/"
        "d6f5debc456da1119256ab66462ab510.png"
    ),
    "chelsea": (
        "https://img.thesports.com/football/team/"
        "a0cf8f551e9440acb3f4ff533dcc58a4.png"
    ),
}

QUIET_REFRESH_SECONDS = 6 * 60 * 60
# SportScore caches its live feed for 60 seconds, so polling more often would
# add load without producing fresher data.
LIVE_REFRESH_SECONDS = 60
FINAL_SCORER_BACKFILL_PER_REFRESH = 8
LIVE_WINDOW_BEFORE_SECONDS = 20 * 60
LIVE_WINDOW_AFTER_SECONDS = 3 * 60 * 60
MIN_REFRESH_SLEEP_SECONDS = 60

LOGIN_ATTEMPT_WINDOW_SECONDS = 10 * 60
LOGIN_ATTEMPT_LIMIT = 5
login_attempts = {}
login_attempts_lock = threading.Lock()

AUTO_BACKUP_SECONDS = 6 * 60 * 60
MAX_AUTO_BACKUPS = 5

SIGNAL_NOTIFICATION_CHECK_SECONDS = 15 * 60
SIGNAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF = 24
SIGNAL_FINAL_REMINDER_HOURS_BEFORE_FIRST_KICKOFF = 2

PREMIER_LEAGUE_NEWS_FEED = (
    "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"
)
NEWS_CACHE_SECONDS = 15 * 60
NEWS_MAX_HEADLINES = 10
NEWS_MAX_AGE = timedelta(hours=36)
news_cache = {"headlines": [], "fetched_at": 0.0, "error": None}
news_cache_lock = threading.Lock()

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

TNT_SPORTS_DARK_LOGO = (
    "https://upload.wikimedia.org/wikipedia/commons/a/a2/"
    "TNT_Sports_%282023%29_alt.svg"
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
harden_path_permissions(DATA_DIR, 0o700)
harden_path_permissions(BACKUP_DIR, 0o700)
harden_path_permissions(UPLOAD_DIR, 0o700)

if os.path.exists(SECRET_FILE):
    harden_path_permissions(SECRET_FILE)
    with open(SECRET_FILE, "r") as f:
        app.secret_key = f.read().strip()
else:
    secret = secrets.token_hex(32)

    with open(SECRET_FILE, "w") as f:
        f.write(secret)

    harden_path_permissions(SECRET_FILE)
    app.secret_key = secret

init_db(seed_default_player=False)
database_restore_lock = threading.Lock()


def _parse_premier_league_news(xml_text, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    oldest_allowed = now - NEWS_MAX_AGE
    headlines = []
    seen_links = set()
    root = ET.fromstring(xml_text)
    for item in root.findall("./channel/item"):
        title = " ".join((item.findtext("title") or "").split())
        link = (item.findtext("link") or "").strip()
        published_text = (item.findtext("pubDate") or "").strip()
        parsed_link = urlparse(link)
        if (
            not title
            or parsed_link.scheme != "https"
            or parsed_link.hostname not in {"www.bbc.co.uk", "www.bbc.com"}
            or link in seen_links
        ):
            continue
        try:
            published_dt = parsedate_to_datetime(published_text)
            if published_dt.tzinfo is None:
                published_dt = published_dt.replace(tzinfo=timezone.utc)
            published_dt = published_dt.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
        if published_dt < oldest_allowed or published_dt > now:
            continue
        seen_links.add(link)
        headlines.append({
            "title": title,
            "url": link,
            "published": published_dt.astimezone(UK).strftime("%a %H:%M"),
            "published_at": published_dt.isoformat(),
        })
        if len(headlines) >= NEWS_MAX_HEADLINES:
            break
    return headlines


def _recent_news_headlines(headlines, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    oldest_allowed = now - NEWS_MAX_AGE
    recent = []
    for headline in headlines:
        try:
            published_dt = datetime.fromisoformat(headline["published_at"])
            if published_dt.tzinfo is None:
                published_dt = published_dt.replace(tzinfo=timezone.utc)
            published_dt = published_dt.astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if oldest_allowed <= published_dt <= now:
            recent.append(headline)
    return recent


def premier_league_news():
    now = time.monotonic()
    with news_cache_lock:
        if news_cache["fetched_at"] > 0 and now - news_cache["fetched_at"] < NEWS_CACHE_SECONDS:
            cached = dict(news_cache)
            cached["headlines"] = _recent_news_headlines(cached["headlines"])
            return cached
        if app.config.get("TESTING"):
            return {
                "headlines": [{
                    "title": "Premier League news ticker test headline",
                    "url": "https://www.bbc.co.uk/sport/football",
                    "published": "Tue 21:35",
                    "published_at": datetime.now(timezone.utc).isoformat(),
                }],
                "fetched_at": now,
                "error": None,
            }
        try:
            response = requests.get(
                PREMIER_LEAGUE_NEWS_FEED,
                timeout=8,
                headers={"User-Agent": "Preddies/1.2 (+news ticker)"},
            )
            response.raise_for_status()
            if len(response.content) > 512 * 1024:
                raise ValueError("News feed response was unexpectedly large.")
            headlines = _parse_premier_league_news(response.text)
            if not headlines:
                raise ValueError("News feed did not contain any usable headlines.")
            news_cache.update({
                "headlines": headlines,
                "fetched_at": now,
                "error": None,
            })
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            # Keep the last good headlines if a refresh fails. The diagnostic
            # page must never depend on the availability of an external feed.
            news_cache.update({"fetched_at": now, "error": str(exc)})
        cached = dict(news_cache)
        cached["headlines"] = _recent_news_headlines(cached["headlines"])
        return cached


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


def login_attempt_key():
    return request.remote_addr or "unknown"


def login_is_rate_limited(key):
    cutoff = time.monotonic() - LOGIN_ATTEMPT_WINDOW_SECONDS
    with login_attempts_lock:
        recent = [stamp for stamp in login_attempts.get(key, []) if stamp > cutoff]
        if recent:
            login_attempts[key] = recent
        else:
            login_attempts.pop(key, None)
        return len(recent) >= LOGIN_ATTEMPT_LIMIT


def record_failed_login(key):
    cutoff = time.monotonic() - LOGIN_ATTEMPT_WINDOW_SECONDS
    with login_attempts_lock:
        recent = [stamp for stamp in login_attempts.get(key, []) if stamp > cutoff]
        recent.append(time.monotonic())
        login_attempts[key] = recent


def clear_failed_logins(key):
    with login_attempts_lock:
        login_attempts.pop(key, None)


def is_admin():
    return bool(session.get("admin"))


def is_treasurer():
    if not logged_in():
        return False
    conn = get_db()
    row = conn.execute(
        "SELECT treasurer FROM players WHERE id = ?", (session["player_id"],)
    ).fetchone()
    conn.close()
    return bool(row and row["treasurer"])


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


def parse_live_minute(value):
    """Return normal and added minutes from SportScore live-clock text."""
    if value is None:
        return None, None
    matches = list(re.finditer(
        r"(?<!\d)(\d{1,3})(?:\s*\+\s*(\d{1,2}))?"
        r"\s*[\'’′]?(?![a-z0-9])",
        str(value).casefold(),
    ))
    if not matches:
        return None, None
    match = matches[-1]
    return int(match.group(1)), (
        int(match.group(2)) if match.group(2) is not None else None
    )


def sportscore_live_clock(details):
    """Combine SportScore's base minute with richer added-time status text."""
    clock = details.get("clock") or {}
    if not isinstance(clock, dict):
        clock = {"display": clock}
    minute, injury_time = (None, None)
    for value in (
        details.get("live_minute"), details.get("minute"),
        details.get("elapsed"), clock.get("display"),
        clock.get("label"), clock.get("minute"), clock.get("elapsed"),
    ):
        parsed_minute, parsed_injury_time = parse_live_minute(value)
        if parsed_minute is not None:
            minute, injury_time = parsed_minute, parsed_injury_time
            break
    status_minute, status_injury_time = parse_live_minute(
        details.get("status_text")
    )
    explicit_injury_time = None
    for value in (
        details.get("injury_time"), details.get("injuryTime"),
        details.get("added_time"), details.get("addedTime"),
        details.get("stoppage_time"), clock.get("injury_time"),
        clock.get("injuryTime"), clock.get("added_time"),
    ):
        if value is None or isinstance(value, bool):
            continue
        match = re.search(r"\d{1,2}", str(value))
        if match:
            explicit_injury_time = int(match.group())
            break
    if minute is not None and explicit_injury_time is not None:
        return minute, explicit_injury_time
    if (
        status_injury_time is not None
        and (minute is None or status_minute == minute)
    ):
        return status_minute, status_injury_time
    if minute is not None:
        return minute, injury_time
    return status_minute, status_injury_time


def sportscore_fixture_status(details, fallback=None):
    """Map SportScore's live state and secondary phase text to app statuses."""
    raw_status = str(details.get("status") or "").strip().casefold()
    phase = re.sub(
        r"[^a-z]+",
        " ",
        str(details.get("status_text") or "").strip().casefold(),
    ).strip()

    if raw_status == "finished" or phase in ("ft", "full time", "finished"):
        return "FINISHED"
    if phase in (
        "ht", "half time", "halftime", "interval", "extra time half time",
        "extra time halftime", "extra time interval", "et half time",
    ):
        return "PAUSED"
    if raw_status == "live":
        return "IN_PLAY"
    return fallback


def provider_match_phase(details):
    """Return the knockout phase without changing the app's core status model."""
    score = details.get("score") or {}
    if not isinstance(score, dict):
        score = {}
    values = (
        details.get("phase"), details.get("period"),
        details.get("status_text"), details.get("status"),
        score.get("duration"),
    )
    phase = " ".join(str(value) for value in values if value)
    phase = re.sub(r"[^a-z0-9]+", " ", phase.casefold()).strip()
    if "penalty shootout" in phase or "penalties" in phase or "shootout" in phase:
        return "PENALTIES"
    if "extra time" in phase or re.search(r"\bet\b", phase):
        if any(marker in phase for marker in ("half time", "halftime", "interval", "break")):
            return "EXTRA_TIME_HALF_TIME"
        return "EXTRA_TIME"
    return None


def provider_penalty_scores(details):
    """Read shootout scores from common provider response shapes."""
    score = details.get("score") or {}
    if not isinstance(score, dict):
        score = {}
    shootout = (
        score.get("penalties") or score.get("penaltyShootout")
        or details.get("penalties") or details.get("penalty_score") or {}
    )
    if not isinstance(shootout, dict):
        shootout = {}

    def parsed(*values):
        for value in values:
            if value is None or isinstance(value, bool):
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    return (
        parsed(
            shootout.get("home"), shootout.get("home_score"),
            details.get("home_penalties"), details.get("homePenaltyScore"),
        ),
        parsed(
            shootout.get("away"), shootout.get("away_score"),
            details.get("away_penalties"), details.get("awayPenaltyScore"),
        ),
    )


def stored_fixture_for_teams(home_team, away_team):
    """Find the imported Predictor fixture matching a diagnostic feed match."""
    wanted = (
        normalized_team_name(home_team),
        normalized_team_name(away_team),
    )
    conn = get_db()
    try:
        fixtures = conn.execute(
            "SELECT * FROM fixtures WHERE season = ? ORDER BY utc_date DESC",
            (SEASON,),
        ).fetchall()
        for fixture in fixtures:
            candidate = (
                normalized_team_name(fixture["home_team"]),
                normalized_team_name(fixture["away_team"]),
            )
            if candidate == wanted:
                return dict(fixture)
    finally:
        conn.close()
    return None


def football_data_fixture_for_teams(matches, home_team, away_team):
    wanted = (
        normalized_team_name(home_team),
        normalized_team_name(away_team),
    )
    for match in matches or []:
        candidate = (
            normalized_team_name((match.get("homeTeam") or {}).get("name")),
            normalized_team_name((match.get("awayTeam") or {}).get("name")),
        )
        if candidate == wanted:
            return match
    return None


def football_data_diagnostic_details(match):
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}
    score = (match.get("score") or {}).get("fullTime") or {}
    provider_status = (match.get("status") or "").upper()
    if provider_status == "FINISHED":
        status = "finished"
    elif provider_status in ("SCHEDULED", "TIMED", "POSTPONED"):
        status = "upcoming"
    else:
        status = "live"
    minute = match.get("minute")
    injury_time = match.get("injuryTime")
    live_minute = None
    if minute is not None:
        live_minute = str(minute)
        if injury_time is not None:
            live_minute += f"+{injury_time}"
    return {
        "home": home.get("name") or "Home",
        "away": away.get("name") or "Away",
        "home_logo": home.get("crest"),
        "away_logo": away.get("crest"),
        "home_score": score.get("home"),
        "away_score": score.get("away"),
        "status": status,
        "status_text": provider_status.replace("_", " ").title(),
        "competition": "UEFA Champions League",
        "live_minute": live_minute,
        "time": match.get("utcDate"),
        "incidents": match.get("goals") or [],
        "_details_loaded": True,
        "_diagnostic_sources": ["football-data.org"],
        "url": (
            "/football/match/"
            f"{sportscore_team_slug(home.get('name'))}-vs-"
            f"{sportscore_team_slug(away.get('name'))}/"
        ),
    }


def format_file_size(byte_count):
    value = float(max(0, byte_count or 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def database_health(conn=None):
    owns_connection = conn is None
    conn = conn or get_db()
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        if owns_connection:
            conn.close()
    database_bytes = os.path.getsize(DB) if os.path.exists(DB) else 0
    wal_path = f"{DB}-wal"
    wal_bytes = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
    reclaimable_bytes = free_pages * page_size
    return {
        "database_bytes": database_bytes,
        "database_size": format_file_size(database_bytes),
        "wal_bytes": wal_bytes,
        "wal_size": format_file_size(wal_bytes),
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_size": format_file_size(reclaimable_bytes),
        "free_pages": free_pages,
        "page_count": page_count,
    }


def fixture_scorers(goals_json, home_team, away_team):
    try:
        goals = json.loads(goals_json or "[]")
    except (TypeError, ValueError):
        goals = []

    grouped = {"home": [], "away": []}
    scorer_index = {"home": {}, "away": {}}

    home_key = normalized_team_name(home_team)
    away_key = normalized_team_name(away_team)

    for goal in goals:
        scorer = (goal.get("scorer") or {}).get("name")
        team = (goal.get("team") or {}).get("name")
        if not scorer or not team:
            continue

        team_key = normalized_team_name(team)
        if team_key == home_key:
            side = "home"
        elif team_key == away_key:
            side = "away"
        else:
            continue

        goal_type = (goal.get("type") or "").upper()
        marker = goal_minute_label(goal)
        if goal_type == "PENALTY":
            marker = f"{marker} (Pen)".strip()
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


def fixture_red_cards(incidents_value, home_team, away_team):
    if isinstance(incidents_value, str):
        try:
            incidents = json.loads(incidents_value or "[]")
        except (TypeError, ValueError):
            incidents = []
    else:
        incidents = incidents_value or []

    grouped = {"home": [], "away": []}
    home_key = normalized_team_name(home_team)
    away_key = normalized_team_name(away_team)
    for incident in incidents:
        incident_type = (incident.get("type") or "").casefold()
        if "red" not in incident_type and incident.get("type_id") not in (4, 5):
            continue
        side = incident.get("side")
        if side not in grouped:
            team = (incident.get("team") or {}).get("name")
            team_key = normalized_team_name(team) if team else ""
            if team_key and team_key == home_key:
                side = "home"
            elif team_key and team_key == away_key:
                side = "away"
            else:
                continue
        minute = incident.get("time")
        minute_label = f"{minute}'" if minute is not None else ""
        grouped[side].append({
            "name": incident.get("player") or "Red card",
            "minute": minute_label,
        })
    return grouped



def ranking_positions(rows):
    return {
        row["id"]: index
        for index, row in enumerate(
            rows,
            start=1
        )
    }


def gameweek_progress_label(fixtures):
    completed = sum(
        1 for fixture in fixtures
        if fixture["status"] == "FINISHED"
    )
    in_progress = sum(
        1 for fixture in fixtures
        if fixture["status"] in ("LIVE", "IN_PLAY", "PAUSED")
    )
    if not completed and not in_progress:
        return ""
    if in_progress:
        live_word = "game" if in_progress == 1 else "games"
        if not completed:
            return f"{in_progress} {live_word} in progress"
        completed_word = "game" if completed == 1 else "games"
        return (
            f"After {completed} completed {completed_word} · "
            f"{in_progress} {live_word} in progress"
        )
    game_word = "game" if completed == 1 else "games"
    return f"After {completed} {game_word}"


def live_gameweek_visible(fixtures):
    """Show live standings only after this gameweek's first kick-off."""
    kickoffs = [
        parse_utc(fixture["utc_date"])
        for fixture in fixtures
        if fixture["status"] != "CANCELLED" and fixture["utc_date"]
    ]
    kickoffs = [kickoff for kickoff in kickoffs if kickoff]
    return bool(kickoffs and now_utc() >= min(kickoffs))


def gameweek_predictions_open(fixtures):
    """Keep the dashboard action until the final active fixture kicks off."""
    kickoffs = [
        parse_utc(fixture["utc_date"])
        for fixture in fixtures
        if fixture["status"] != "CANCELLED" and fixture["utc_date"]
    ]
    kickoffs = [kickoff for kickoff in kickoffs if kickoff]
    return bool(kickoffs and now_utc() < max(kickoffs))


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
                 correct_results DESC,
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
                0 AS exact_scores,
                0 AS correct_results
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
            ) AS exact_scores,

            COALESCE(
                SUM(
                    CASE
                    WHEN f.status = 'FINISHED'
                     AND f.matchday <= ?
                     AND NOT (
                        p.home_score = f.home_score
                        AND p.away_score = f.away_score
                     )
                     AND (
                        (f.home_score = f.away_score AND p.home_score = p.away_score)
                        OR (f.home_score > f.away_score AND p.home_score > p.away_score)
                        OR (f.home_score < f.away_score AND p.home_score < p.away_score)
                     )
                    THEN 1
                    ELSE 0
                    END
                ),
                0
            ) AS correct_results

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
            correct_results DESC,
            pl.name COLLATE NOCASE
        """,
        (
            matchday,
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


def build_live_table(fixtures, players, predictions, previous_league):
    prediction_map = {
        (prediction["player_id"], prediction["fixture_id"]): prediction
        for prediction in predictions
    }
    reveal_map = {
        fixture["id"]: fixture_is_locked(fixture)
        for fixture in fixtures
    }
    previous_by_player = {
        row["id"]: row
        for row in previous_league
    }
    baseline_positions = ranking_positions(previous_league)
    live_table = []

    for player in players:
        provisional = 0
        live_exact_draws = 0
        live_exact_scores = 0
        live_correct_results = 0

        for fixture in fixtures:
            prediction = prediction_map.get(
                (player["id"], fixture["id"])
            )
            if not prediction or not reveal_map[fixture["id"]]:
                continue
            if (
                fixture["home_score"] is None
                or fixture["away_score"] is None
            ):
                continue

            provisional += calculate_prediction_points(
                prediction["home_score"],
                prediction["away_score"],
                fixture["home_score"],
                fixture["away_score"],
                bool(prediction["dp"]),
            )
            if (
                prediction["home_score"] == fixture["home_score"]
                and prediction["away_score"] == fixture["away_score"]
            ):
                if fixture["home_score"] == fixture["away_score"]:
                    live_exact_draws += 1
                else:
                    live_exact_scores += 1
            elif (
                (fixture["home_score"] == fixture["away_score"] and prediction["home_score"] == prediction["away_score"])
                or (fixture["home_score"] > fixture["away_score"] and prediction["home_score"] > prediction["away_score"])
                or (fixture["home_score"] < fixture["away_score"] and prediction["home_score"] < prediction["away_score"])
            ):
                live_correct_results += 1

        previous = previous_by_player.get(player["id"])
        live_table.append({
            "id": player["id"],
            "name": player["name"],
            "points": provisional,
            "season_points": (
                previous["points"] if previous else 0
            ) + provisional,
            "exact_draws": (
                previous["exact_draws"] if previous else 0
            ) + live_exact_draws,
            "exact_scores": (
                previous["exact_scores"] if previous else 0
            ) + live_exact_scores,
            "correct_results": (
                previous["correct_results"] if previous else 0
            ) + live_correct_results,
        })

    live_table.sort(
        key=lambda row: (
            -row["season_points"],
            -row["exact_draws"],
            -row["exact_scores"],
            -row["correct_results"],
            row["name"].lower(),
        )
    )
    for position, player in enumerate(live_table, start=1):
        player["position"] = position
        player["position_change"] = table_position_change(
            position,
            baseline_positions.get(player["id"]),
        )
    return live_table


def _snapshot_rows(conn, matchday):
    fixtures = conn.execute(
        """SELECT * FROM fixtures
           WHERE season = ? AND matchday = ?
           ORDER BY utc_date""",
        (SEASON, matchday),
    ).fetchall()
    players = conn.execute(
        "SELECT id, name FROM players ORDER BY name COLLATE NOCASE"
    ).fetchall()
    predictions = conn.execute(
        """SELECT p.player_id, p.fixture_id, p.home_score, p.away_score,
                  COALESCE(p.dp, 0) AS dp
           FROM predictions p
           JOIN fixtures f ON f.id = p.fixture_id
           WHERE f.season = ? AND f.matchday = ?""",
        (SEASON, matchday),
    ).fetchall()
    previous_league = overall_table_at_matchday(conn, matchday - 1)
    return (
        fixtures,
        build_live_table(fixtures, players, predictions, previous_league),
        previous_league,
    )


def _insert_position_snapshot(
    conn, matchday, captured_at, signature, rows,
    cause_fixture_id=None, cause_label=None,
):
    cursor = conn.execute(
        """INSERT OR IGNORE INTO live_position_snapshots
           (season, matchday, captured_at, state_signature,
            cause_fixture_id, cause_label)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            SEASON, matchday, captured_at, signature,
            cause_fixture_id, cause_label,
        ),
    )
    if cursor.rowcount != 1:
        return False
    conn.executemany(
        """INSERT INTO live_position_snapshot_rows
           (snapshot_id, player_id, player_name, position,
            season_points, gameweek_points)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            (
                cursor.lastrowid,
                row["id"],
                row["name"],
                row["position"],
                row["season_points"],
                row.get("points", 0),
            )
            for row in rows
        ],
    )
    return True


def chart_team_code(name):
    key = canonical_team_name(name)
    codes = {
        "manchester city": "MCI", "manchester united": "MUN",
        "nottingham forest": "NFO", "crystal palace": "CRY",
        "tottenham hotspur": "TOT", "newcastle united": "NEW",
        "aston villa": "AVL", "afc bournemouth": "BOU",
        "brighton hove albion": "BHA", "west ham united": "WHU",
        "wolverhampton wanderers": "WOL", "leeds united": "LEE",
    }
    if key in codes:
        return codes[key]
    letters = re.sub(r"[^a-z]", "", key).upper()
    return letters[:3] or "TEAM"


def _position_snapshot_cause(fixtures):
    candidates = []
    for fixture in fixtures:
        updated = parse_utc(fixture["last_updated"])
        if updated:
            candidates.append((updated, fixture))
    if candidates:
        fixture = max(candidates, key=lambda item: item[0])[1]
    else:
        fixture = next(
            (
                item for item in fixtures
                if item["status"] in ("LIVE", "IN_PLAY", "PAUSED", "FINISHED")
            ),
            None,
        )
    if not fixture:
        return None, "Position change"
    score = ""
    if fixture["home_score"] is not None and fixture["away_score"] is not None:
        score = f" {fixture['home_score']}–{fixture['away_score']}"
    return (
        fixture["id"],
        f"{chart_team_code(fixture['home_team'])}{score} "
        f"{chart_team_code(fixture['away_team'])}",
    )


def record_live_position_snapshot(conn, matchday):
    fixtures, live_table, previous_league = _snapshot_rows(conn, matchday)
    if not fixtures or not any(
        fixture["status"] in ("LIVE", "IN_PLAY", "PAUSED", "FINISHED")
        for fixture in fixtures
    ):
        return False

    existing = conn.execute(
        """SELECT COUNT(*) FROM live_position_snapshots
           WHERE season = ? AND matchday = ?""",
        (SEASON, matchday),
    ).fetchone()[0]
    if not existing:
        baseline = []
        for position, row in enumerate(previous_league, start=1):
            baseline.append({
                "id": row["id"],
                "name": row["name"],
                "position": position,
                "season_points": row["points"],
                "points": 0,
            })
        _insert_position_snapshot(
            conn,
            matchday,
            fixtures[0]["utc_date"],
            "baseline",
            baseline,
            cause_label="Kick-off",
        )

    signature = json.dumps(
        [
            [row["id"], row["position"]]
            for row in live_table
        ],
        separators=(",", ":"),
    )

    # This is a position chart, so points changes that leave everybody in the
    # same place must not create another dot. Compare against the actual rows
    # (including the KO baseline), rather than the stored signature format.
    latest = conn.execute(
        """SELECT id, state_signature FROM live_position_snapshots
           WHERE season = ? AND matchday = ?
           ORDER BY captured_at DESC, id DESC LIMIT 1""",
        (SEASON, matchday),
    ).fetchone()
    if latest:
        latest_positions = [
            [row["player_id"], row["position"]]
            for row in conn.execute(
                """SELECT player_id, position
                   FROM live_position_snapshot_rows
                   WHERE snapshot_id = ? ORDER BY player_id""",
                (latest["id"],),
            ).fetchall()
        ]
        current_positions = sorted(
            [[row["id"], row["position"]] for row in live_table]
        )
        if latest_positions == current_positions:
            return False

    stored_signature = signature
    if conn.execute(
        """SELECT 1 FROM live_position_snapshots
           WHERE season = ? AND matchday = ? AND state_signature = ?""",
        (SEASON, matchday, signature),
    ).fetchone():
        stored_signature = f"{signature}\noccurrence:{now_utc().isoformat()}"

    captured_at = now_utc()
    if not any(
        fixture["status"] in ("LIVE", "IN_PLAY", "PAUSED")
        for fixture in fixtures
    ):
        finished_updates = [
            parse_utc(fixture["last_updated"])
            for fixture in fixtures
            if fixture["status"] == "FINISHED" and fixture["last_updated"]
        ]
        finished_updates = [value for value in finished_updates if value]
        if finished_updates:
            captured_at = max(finished_updates)

    cause_fixture_id, cause_label = _position_snapshot_cause(fixtures)
    return _insert_position_snapshot(
        conn,
        matchday,
        captured_at.isoformat(),
        stored_signature,
        live_table,
        cause_fixture_id=cause_fixture_id,
        cause_label=cause_label,
    )


def _position_snapshot_state(snapshot):
    return tuple(sorted(
        (
            row["player_id"], row["position"],
            row["season_points"], row["gameweek_points"],
        )
        for row in snapshot["rows"]
    ))


def _smooth_transient_position_snapshots(snapshots, max_seconds=180):
    """Remove only brief provider wobble when the prior state returns."""
    smoothed = list(snapshots)
    changed = True
    while changed and len(smoothed) >= 3:
        changed = False
        for index in range(1, len(smoothed) - 1):
            previous = smoothed[index - 1]
            transient = smoothed[index]
            following = smoothed[index + 1]
            if (
                _position_snapshot_state(previous)
                != _position_snapshot_state(following)
                or _position_snapshot_state(transient)
                == _position_snapshot_state(previous)
            ):
                continue
            previous_at = parse_utc(previous["captured_at"])
            following_at = parse_utc(following["captured_at"])
            if (
                previous_at
                and following_at
                and 0 <= (following_at - previous_at).total_seconds() <= max_seconds
            ):
                del smoothed[index]
                changed = True
                break
    return smoothed


def _compact_position_snapshots(snapshots):
    """Keep one settled position state for each football event/cause."""
    compacted = []
    for snapshot in snapshots:
        is_baseline = snapshot.get("state_signature") == "baseline"
        cause = (snapshot.get("cause_label") or "").strip()
        if (
            compacted
            and not is_baseline
            and compacted[-1].get("state_signature") != "baseline"
            and cause == (compacted[-1].get("cause_label") or "").strip()
        ):
            # Multiple providers can recalculate the table repeatedly for the
            # same score. Only the final settled ordering is useful.
            compacted[-1] = snapshot
        else:
            compacted.append(snapshot)

    deduplicated = []
    for snapshot in compacted:
        positions = tuple(sorted(
            (row["player_id"], row["position"])
            for row in snapshot["rows"]
        ))
        previous = tuple(sorted(
            (row["player_id"], row["position"])
            for row in deduplicated[-1]["rows"]
        )) if deduplicated else None
        if positions == previous:
            continue
        deduplicated.append(snapshot)
    return deduplicated


def _reconstruct_finished_position_snapshots(conn, matchday):
    """Replay completed fixtures when the live snapshot history is absent.

    This deliberately reconstructs only settled, result-based position
    changes. It cannot recreate unsaved goal-by-goal provider observations.
    """
    fixtures = conn.execute(
        """SELECT * FROM fixtures
           WHERE season = ? AND matchday = ? AND status = 'FINISHED'
             AND home_score IS NOT NULL AND away_score IS NOT NULL
           ORDER BY COALESCE(last_updated, utc_date), utc_date, id""",
        (SEASON, matchday),
    ).fetchall()
    if not fixtures:
        return []

    players = conn.execute(
        "SELECT id, name FROM players ORDER BY name COLLATE NOCASE"
    ).fetchall()
    predictions = conn.execute(
        """SELECT p.player_id, p.fixture_id, p.home_score, p.away_score,
                  COALESCE(p.dp, 0) AS dp
           FROM predictions p
           JOIN fixtures f ON f.id = p.fixture_id
           WHERE f.season = ? AND f.matchday = ?""",
        (SEASON, matchday),
    ).fetchall()
    previous_league = overall_table_at_matchday(conn, matchday - 1)
    baseline = [
        {
            "player_id": row["id"],
            "position": position,
            "season_points": row["points"],
            "gameweek_points": 0,
        }
        for position, row in enumerate(previous_league, start=1)
    ]
    snapshots = [{
        "captured_at": fixtures[0]["utc_date"],
        "state_signature": "reconstructed-baseline",
        "cause_fixture_id": None,
        "cause_label": "Kick-off",
        "label": "",
        "milestone": "KO",
        "rows": baseline,
    }]
    previous_positions = tuple(sorted(
        (row["player_id"], row["position"]) for row in baseline
    ))
    completed = []
    for fixture in fixtures:
        completed.append(fixture)
        table = build_live_table(
            completed, players, predictions, previous_league
        )
        rows = [
            {
                "player_id": row["id"],
                "position": row["position"],
                "season_points": row["season_points"],
                "gameweek_points": row["points"],
            }
            for row in table
        ]
        positions = tuple(sorted(
            (row["player_id"], row["position"]) for row in rows
        ))
        if positions == previous_positions:
            continue
        captured = parse_utc(fixture["last_updated"] or fixture["utc_date"])
        score = f"{fixture['home_score']}–{fixture['away_score']}"
        snapshots.append({
            "captured_at": captured.isoformat() if captured else fixture["utc_date"],
            "state_signature": f"reconstructed:{fixture['id']}",
            "cause_fixture_id": fixture["id"],
            "cause_label": (
                f"{chart_team_code(fixture['home_team'])} {score} "
                f"{chart_team_code(fixture['away_team'])}"
            ),
            "label": captured.astimezone(UK).strftime("%H:%M") if captured else "",
            "rows": rows,
        })
        previous_positions = positions
    return snapshots


def live_position_chart(conn, matchday):
    rows = conn.execute(
        """SELECT s.id AS snapshot_id, s.captured_at, s.state_signature,
                  s.cause_fixture_id, s.cause_label,
                  r.player_id, r.player_name, r.position,
                  r.season_points, r.gameweek_points
           FROM live_position_snapshots s
           JOIN live_position_snapshot_rows r ON r.snapshot_id = s.id
           WHERE s.season = ? AND s.matchday = ?
           ORDER BY s.captured_at, s.id, r.position""",
        (SEASON, matchday),
    ).fetchall()
    snapshots = []
    by_snapshot = {}
    players = {}
    for row in rows:
        snapshot = by_snapshot.get(row["snapshot_id"])
        if snapshot is None:
            captured = parse_utc(row["captured_at"])
            snapshot = {
                "captured_at": row["captured_at"],
                "state_signature": row["state_signature"],
                "cause_fixture_id": row["cause_fixture_id"],
                "cause_label": row["cause_label"] or "",
                "label": (
                    captured.astimezone(UK).strftime("%H:%M")
                    if captured else ""
                ),
                "rows": [],
            }
            by_snapshot[row["snapshot_id"]] = snapshot
            snapshots.append(snapshot)
        point = {
            "player_id": row["player_id"],
            "position": row["position"],
            "season_points": row["season_points"],
            "gameweek_points": row["gameweek_points"],
        }
        snapshot["rows"].append(point)
        players[row["player_id"]] = {
            "id": row["player_id"],
            "name": row["player_name"],
        }

    # A pair of providers can publish competing states only seconds apart.
    # Keep the final settled state for each displayed minute so a transient
    # correction does not create duplicate timestamps or a false zig-zag.
    coalesced = []
    for snapshot in snapshots:
        minute_key = snapshot["label"]
        is_baseline = snapshot["state_signature"] == "baseline"
        if (
            coalesced
            and not is_baseline
            and coalesced[-1]["state_signature"] != "baseline"
            and coalesced[-1]["label"] == minute_key
        ):
            coalesced[-1] = snapshot
        else:
            coalesced.append(snapshot)
    snapshots = coalesced

    # Older releases stored points-only refreshes even when nobody moved.
    # Hide those legacy duplicates so each displayed dot is a position change.
    position_changes = []
    for snapshot in snapshots:
        current_positions = tuple(sorted(
            (row["player_id"], row["position"])
            for row in snapshot["rows"]
        ))
        previous_positions = tuple(sorted(
            (row["player_id"], row["position"])
            for row in position_changes[-1]["rows"]
        )) if position_changes else None
        if current_positions == previous_positions:
            continue
        position_changes.append(snapshot)
    snapshots = position_changes

    if snapshots:
        snapshots[0]["milestone"] = "KO"
        finished_updates = [
            parse_utc(row["last_updated"])
            for row in conn.execute(
                """SELECT last_updated FROM fixtures
                   WHERE season = ? AND matchday = ?
                     AND status = 'FINISHED'
                     AND last_updated IS NOT NULL""",
                (SEASON, matchday),
            ).fetchall()
        ]
        finished_updates = [value for value in finished_updates if value]
        for snapshot in snapshots[1:]:
            captured = parse_utc(snapshot["captured_at"])
            if captured and any(
                abs((captured - finished_at).total_seconds()) <= 180
                for finished_at in finished_updates
            ):
                snapshot["milestone"] = "FT"

    # The table above the chart is calculated from the current fixture state.
    # Reconcile the rendered final point with that same state, even if an old
    # or conflicting database snapshot survived an earlier provider wobble.
    current_fixtures, current_table, _ = _snapshot_rows(conn, matchday)
    if current_table:
        current_rows = [
            {
                "player_id": row["id"],
                "position": row["position"],
                "season_points": row["season_points"],
                "gameweek_points": row["points"],
            }
            for row in current_table
        ]
        current_state = sorted(
            (row["player_id"], row["position"])
            for row in current_rows
        )
        last_state = sorted(
            (row["player_id"], row["position"])
            for row in (snapshots[-1]["rows"] if snapshots else [])
        )
        if current_state != last_state:
            update_times = [
                parse_utc(fixture["last_updated"])
                for fixture in current_fixtures
                if fixture["last_updated"]
            ]
            update_times = [value for value in update_times if value]
            captured = max(update_times) if update_times else now_utc()
            reconciled = {
                "captured_at": captured.isoformat(),
                "state_signature": "current-reconciled",
                "label": captured.astimezone(UK).strftime("%H:%M"),
                "rows": current_rows,
            }
            cause_fixture_id, cause_label = _position_snapshot_cause(
                current_fixtures
            )
            reconciled["cause_fixture_id"] = cause_fixture_id
            reconciled["cause_label"] = cause_label
            if (
                not any(
                    fixture["status"] in ("LIVE", "IN_PLAY", "PAUSED")
                    for fixture in current_fixtures
                )
                and any(fixture["status"] == "FINISHED" for fixture in current_fixtures)
            ):
                reconciled["milestone"] = "FT"
            if snapshots and snapshots[-1]["label"] == reconciled["label"]:
                snapshots[-1] = reconciled
            else:
                snapshots.append(reconciled)
        for row in current_table:
            players[row["id"]] = {"id": row["id"], "name": row["name"]}
    snapshots = _smooth_transient_position_snapshots(snapshots)
    snapshots = _compact_position_snapshots(snapshots)
    if len(snapshots) <= 1:
        reconstructed = _reconstruct_finished_position_snapshots(
            conn, matchday
        )
        if len(reconstructed) > len(snapshots):
            snapshots = reconstructed
    return {
        "players": list(players.values()),
        "snapshots": snapshots,
    }


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
    fixture_keys = set(fixture.keys())
    match_phase = (
        fixture["match_phase"] if "match_phase" in fixture_keys else None
    )
    home_penalties = (
        fixture["home_penalty_score"]
        if "home_penalty_score" in fixture_keys else None
    )
    away_penalties = (
        fixture["away_penalty_score"]
        if "away_penalty_score" in fixture_keys else None
    )

    if match_phase == "PENALTIES":
        shootout = (
            f" {home_penalties}–{away_penalties}"
            if home_penalties is not None and away_penalties is not None else ""
        )
        return (
            f"FT (PENS{shootout})" if status == "FINISHED"
            else f"PENS{shootout}"
        )

    if status in ("LIVE", "IN_PLAY"):
        minute = fixture["minute"]
        injury_time = (
            fixture["injury_time"]
            if "injury_time" in fixture.keys()
            else None
        )

        if minute:
            prefix = "ET" if match_phase == "EXTRA_TIME" else "LIVE"
            if injury_time:
                return f"{prefix} {minute}+{injury_time}'"
            return f"{prefix} {minute}'"

        return "ET" if match_phase == "EXTRA_TIME" else "LIVE"

    if status == "PAUSED":
        if match_phase in ("EXTRA_TIME", "EXTRA_TIME_HALF_TIME"):
            return "ET HT"
        return "HT"

    if status == "FINISHED":
        if match_phase in ("EXTRA_TIME", "EXTRA_TIME_HALF_TIME"):
            return "AET"
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


def broadcaster_dark_logo_url(broadcaster):
    if broadcaster == "TNT Sports":
        return TNT_SPORTS_DARK_LOGO

    return None


def refresh_points(conn):
    predictions = conn.execute(
        """
        SELECT
            p.id,
            COALESCE(p.points, 0) AS stored_points,
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

    changed_points = []
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

        if points != prediction["stored_points"]:
            changed_points.append((points, prediction["id"]))

    if changed_points:
        conn.executemany(
            "UPDATE predictions SET points = ? WHERE id = ?",
            changed_points,
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
        "coventry": "coventry city",
        "coventry city": "coventry city",
        "hull": "hull city",
        "hull city": "hull city",
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
    """SportScore-style display names; does not alter stored/API team names."""
    key = canonical_team_name(name)

    names = {
        "arsenal": "Arsenal",
        "aston villa": "Aston Villa",
        "bournemouth": "AFC Bournemouth",
        "brentford": "Brentford",
        "brighton": "Brighton & Hove Albion",
        "burnley": "Burnley",
        "chelsea": "Chelsea",
        "crystal palace": "Crystal Palace",
        "everton": "Everton",
        "fulham": "Fulham",
        "leeds": "Leeds United",
        "liverpool": "Liverpool",
        "manchester city": "Manchester City",
        "manchester united": "Manchester United",
        "newcastle": "Newcastle United",
        "nottingham forest": "Nottingham Forest",
        "sunderland": "Sunderland",
        "tottenham": "Tottenham Hotspur",
        "west ham": "West Ham United",
        "wolves": "Wolverhampton Wanderers",
        "leicester": "Leicester City",
        "ipswich": "Ipswich Town",
        "southampton": "Southampton",
        "luton": "Luton Town",
        "sheffield united": "Sheffield United",
        "coventry city": "Coventry City",
        "queens park rangers": "Queens Park Rangers",
        "west brom": "West Bromwich Albion",
        "norwich": "Norwich City",
        "watford": "Watford",
    }

    if key in names:
        return names[key]

    # Safe generic fallback: remove common suffixes while retaining readable case.
    value = (name or "").strip()
    value = re.sub(r"\s+(?:FC|AFC)$", "", value, flags=re.I)
    return value


def mobile_prediction_team_name(name):
    """Fixed compact labels for the current season's mobile prediction grid."""
    key = canonical_team_name(name)
    names = {
        "arsenal": "Arsenal",
        "aston villa": "Aston Villa",
        "bournemouth": "B'mouth",
        "brentford": "Brentford",
        "brighton": "Brighton",
        "burnley": "Burnley",
        "chelsea": "Chelsea",
        "crystal palace": "Palace",
        "everton": "Everton",
        "fulham": "Fulham",
        "leeds": "Leeds",
        "liverpool": "Liverpool",
        "manchester city": "Man City",
        "manchester united": "Man United",
        "newcastle": "Newcastle",
        "nottingham forest": "Forest",
        "sunderland": "Sunderland",
        "tottenham": "Spurs",
        "west ham": "West Ham",
        "wolves": "Wolves",
        "ipswich": "Ipswich",
        "coventry city": "Coventry",
    }
    return names.get(key, short_team_name(name))



def football_data_co_uk_season_code(season):
    return (
        f"{season % 100:02d}"
        f"{(season + 1) % 100:02d}"
    )


def import_historical_csv_season(conn, season, division="E0"):
    season_code = football_data_co_uk_season_code(
        season
    )

    url = (
        "https://www.football-data.co.uk/"
        f"mmz4281/{season_code}/{division}.csv"
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
            f"{division}|{season}|{date_value}|"
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
                status,
                competition
            )
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'FINISHED', ?)

            ON CONFLICT(id)
            DO UPDATE SET
                season = excluded.season,
                utc_date = excluded.utc_date,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                status = 'FINISHED',
                competition = excluded.competition
            """,
            (
                match_id,
                season,
                match_date.isoformat(),
                home,
                away,
                hs,
                aas,
                division,
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
        WHERE home_score IS NOT NULL
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


def _is_current_season_result(row):
    """Accept correctly tagged rows and date-valid current-season fallbacks."""
    if row["season"] == SEASON:
        return True
    played_at = parse_utc(row["utc_date"])
    if not played_at:
        return False
    season_start = datetime(SEASON, 7, 1, tzinfo=timezone.utc)
    season_end = datetime(SEASON + 1, 7, 1, tzinfo=timezone.utc)
    return season_start <= played_at < season_end


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

    # Venue records remain the pre-match numbers for the whole gameweek. The
    # current result may update recent form, but it must not enter home/away
    # aggregates until a later fixture is opened.
    form_rows = list(all_prior)
    current_result = conn.execute(
        """SELECT id, season, matchday, utc_date, home_team, away_team,
                  home_score, away_score
           FROM fixtures
           WHERE id = ? AND utc_date <= ?
             AND home_score IS NOT NULL AND away_score IS NOT NULL""",
        (fixture["id"], now_utc().isoformat()),
    ).fetchone()
    if current_result and not any(
        row["id"] == current_result["id"] for row in form_rows
    ):
        form_rows = [current_result] + form_rows

    home_key = canonical_team_name(
        home_team
    )
    away_key = canonical_team_name(
        away_team
    )

    # Current-season Premier League records for both teams, regardless of
    # whether their previous matches were played home or away. "Home" and
    # "away" here identify the teams in this fixture, not venue-specific form.
    # Use canonical names because different deterministic data sources
    # use variants such as "Arsenal" and "Arsenal FC".
    home_record_rows = [
        row
        for row in all_prior
        if _is_current_season_result(row)
        and (
            canonical_team_name(row["home_team"]) == home_key
            or canonical_team_name(row["away_team"]) == home_key
        )
    ]

    away_record_rows = [
        row
        for row in all_prior
        if _is_current_season_result(row)
        and (
            canonical_team_name(row["home_team"]) == away_key
            or canonical_team_name(row["away_team"]) == away_key
        )
    ]

    home_form_rows = [
        row
        for row in form_rows
        if _is_current_season_result(row)
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
        for row in form_rows
        if _is_current_season_result(row)
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
    seasons=None,
    include_championship=True,
):
    """
    Import previous Premier League and Championship results.
    Championship matches broaden cross-division H2H without affecting the
    Premier League-only form and season records.
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

            if include_championship:
                try:
                    championship_imported = import_historical_csv_season(
                        conn,
                        season,
                        "E1"
                    )
                    imported += championship_imported

                    if championship_imported:
                        sources.append(
                            f"{season}/{season + 1}: "
                            "football-data.co.uk Championship"
                        )

                except Exception as exc:
                    errors.append(
                        f"{season}/{season + 1}: Championship CSV {exc}"
                    )

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
        home_penalty_score, away_penalty_score = provider_penalty_scores(match)

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
                match_phase,
                home_penalty_score,
                away_penalty_score,
                goals_json,
                live_data_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(id)
            DO UPDATE SET
                matchday = excluded.matchday,
                utc_date = excluded.utc_date,
                status = CASE
                    WHEN fixtures.status = 'FINISHED'
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.status
                    WHEN fixtures.live_data_source = 'SportScore'
                      AND fixtures.status IN ('LIVE', 'IN_PLAY', 'PAUSED')
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.status
                    ELSE excluded.status
                END,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                home_score = CASE
                    WHEN fixtures.status = 'FINISHED'
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.home_score
                    WHEN fixtures.live_data_source = 'SportScore'
                      AND fixtures.status IN ('LIVE', 'IN_PLAY', 'PAUSED')
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.home_score
                    ELSE COALESCE(excluded.home_score, fixtures.home_score)
                END,
                away_score = CASE
                    WHEN fixtures.status = 'FINISHED'
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.away_score
                    WHEN fixtures.live_data_source = 'SportScore'
                      AND fixtures.status IN ('LIVE', 'IN_PLAY', 'PAUSED')
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.away_score
                    ELSE COALESCE(excluded.away_score, fixtures.away_score)
                END,
                last_updated = CASE
                    WHEN fixtures.status = 'FINISHED'
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.last_updated
                    WHEN fixtures.live_data_source = 'SportScore'
                      AND fixtures.status IN ('LIVE', 'IN_PLAY', 'PAUSED')
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.last_updated
                    ELSE COALESCE(excluded.last_updated, fixtures.last_updated)
                END,
                minute = CASE
                    WHEN fixtures.live_data_source = 'SportScore'
                      AND fixtures.status IN ('LIVE', 'IN_PLAY', 'PAUSED')
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.minute
                    ELSE COALESCE(excluded.minute, fixtures.minute)
                END,
                injury_time = CASE
                    WHEN fixtures.live_data_source = 'SportScore'
                      AND fixtures.status IN ('LIVE', 'IN_PLAY', 'PAUSED')
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.injury_time
                    ELSE COALESCE(excluded.injury_time, fixtures.injury_time)
                END,
                match_phase = COALESCE(excluded.match_phase, fixtures.match_phase),
                home_penalty_score = COALESCE(
                    excluded.home_penalty_score, fixtures.home_penalty_score
                ),
                away_penalty_score = COALESCE(
                    excluded.away_penalty_score, fixtures.away_penalty_score
                ),
                goals_json = CASE
                    WHEN fixtures.status = 'FINISHED'
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.goals_json
                    WHEN fixtures.live_data_source = 'SportScore'
                      AND fixtures.status IN ('LIVE', 'IN_PLAY', 'PAUSED')
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.goals_json
                    WHEN excluded.goals_json IS NULL
                      OR excluded.goals_json = '[]'
                    THEN fixtures.goals_json
                    ELSE excluded.goals_json
                END,
                live_data_source = CASE
                    WHEN fixtures.status = 'FINISHED'
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.live_data_source
                    WHEN fixtures.live_data_source = 'SportScore'
                      AND fixtures.status IN ('LIVE', 'IN_PLAY', 'PAUSED')
                      AND excluded.status != 'FINISHED'
                    THEN fixtures.live_data_source
                    ELSE excluded.live_data_source
                END
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
                provider_match_phase(match),
                home_penalty_score,
                away_penalty_score,
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
    # football-data.org can be the first provider to publish the final score.
    # Capture the resulting final table state even when the live SportScore
    # polling loop has already stopped because every fixture is now finished.
    snapshot_matchday = dashboard_current_gameweek(conn)
    if snapshot_matchday is not None:
        record_live_position_snapshot(conn, snapshot_matchday)
    archive_completed_season(conn, SEASON)

    conn.commit()
    conn.close()

    set_setting(
        "last_api_refresh",
        now_utc().isoformat()
    )

    return imported


def repair_missing_completed_results():
    """Recover blank completed scores from the retained goal-event archive."""
    conn = get_db()
    repaired = 0
    repaired_matchdays = set()
    try:
        played_before = (now_utc() - timedelta(hours=3)).isoformat()
        fixtures = conn.execute(
            """SELECT id, matchday, home_team, away_team, goals_json
               FROM fixtures
               WHERE season = ?
                 AND status NOT IN ('LIVE', 'IN_PLAY', 'PAUSED', 'CANCELLED')
                 AND utc_date <= ?
                 AND (home_score IS NULL OR away_score IS NULL)
                 AND goals_json IS NOT NULL""",
            (SEASON, played_before),
        ).fetchall()

        for fixture in fixtures:
            try:
                goals = json.loads(fixture["goals_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(goals, list):
                continue

            home_key = normalized_team_name(fixture["home_team"])
            away_key = normalized_team_name(fixture["away_team"])
            home_score = away_score = 0
            complete = True
            for goal in goals:
                team = (goal.get("team") or {}).get("name")
                team_key = normalized_team_name(team)
                if team_key == home_key:
                    home_score += 1
                elif team_key == away_key:
                    away_score += 1
                else:
                    complete = False
                    break
            if not complete:
                continue

            conn.execute(
                """UPDATE fixtures
                   SET status = 'FINISHED', home_score = ?, away_score = ?,
                       minute = NULL, injury_time = NULL, last_updated = ?
                   WHERE id = ?""",
                (home_score, away_score, now_utc().isoformat(), fixture["id"]),
            )
            repaired += 1
            if fixture["matchday"] is not None:
                repaired_matchdays.add(fixture["matchday"])

        if repaired:
            refresh_points(conn)
            for matchday in sorted(repaired_matchdays):
                record_live_position_snapshot(conn, matchday)
            archive_completed_season(conn, SEASON)
        conn.commit()
        return repaired
    finally:
        conn.close()


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
        "brighton": "brighton-hove-albion",
        "man city": "manchester-city",
        "man united": "manchester-united",
        "newcastle": "newcastle-united",
        "nott m forest": "nottingham-forest",
        "nottm forest": "nottingham-forest",
        "tottenham": "tottenham-hotspur",
        "west ham": "west-ham-united",
        "wolves": "wolverhampton-wanderers",
    }
    return aliases.get(normalized, normalized.replace(" ", "-"))


def safe_team_logo_url(value):
    """Only persist normal web image URLs supplied by SportScore."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return value


def team_badge_url(value):
    """Serve SportScore badges through Predictor for reliable mobile display."""
    value = safe_team_logo_url(value)
    if not value:
        return None
    if (urlparse(value).hostname or "").casefold() == "img.thesports.com":
        return f"/team-badge?url={quote(value, safe='')}"
    return value


def compact_record_name(name):
    """Use the first name segment in narrow League Records cards only."""
    parts = str(name or "").strip().split()
    return parts[0] if parts else "—"


def populate_missing_team_logos():
    """Fill missing fixture badges independently of live-match updates."""
    conn = get_db()
    updated = 0
    try:
        teams = conn.execute(
            """
            SELECT team FROM (
                SELECT home_team AS team
                FROM fixtures
                WHERE season = ? AND COALESCE(home_logo, '') = ''
                UNION
                SELECT away_team AS team
                FROM fixtures
                WHERE season = ? AND COALESCE(away_logo, '') = ''
            )
            ORDER BY team
            """,
            (SEASON, SEASON),
        ).fetchall()

        for index, row in enumerate(teams):
            team_name = row["team"]
            team_slug = sportscore_team_slug(team_name)
            try:
                discovered_logo = get_sportscore_team_logo(team_slug)
            except SportScoreError as exc:
                print(
                    f"[SportScore] Badge lookup failed for {team_name}: {exc}",
                    flush=True,
                )
                discovered_logo = None

            logo = safe_team_logo_url(
                discovered_logo
                or SPORTSCORE_TEAM_LOGO_FALLBACKS.get(team_slug)
            )

            if logo:
                home = conn.execute(
                    """
                    UPDATE fixtures SET home_logo = ?
                    WHERE season = ? AND home_team = ?
                      AND COALESCE(home_logo, '') = ''
                    """,
                    (logo, SEASON, team_name),
                ).rowcount
                away = conn.execute(
                    """
                    UPDATE fixtures SET away_logo = ?
                    WHERE season = ? AND away_team = ?
                      AND COALESCE(away_logo, '') = ''
                    """,
                    (logo, SEASON, team_name),
                ).rowcount
                updated += home + away
                conn.commit()

            # Keep badge discovery gentle on SportScore.
            if index < len(teams) - 1:
                time.sleep(1)
    finally:
        conn.close()

    return updated


def team_logo_worker():
    time.sleep(30)
    while True:
        try:
            updated = populate_missing_team_logos()
            print(
                f"[SportScore] Populated {updated} missing fixture badge(s)",
                flush=True,
            )
        except Exception as exc:
            print(f"[SportScore] Badge refresh failed: {exc}", flush=True)
        time.sleep(QUIET_REFRESH_SECONDS)


def import_live_matches_from_sportscore(force_current_gameweek=False):
    conn = get_db()
    updated = 0

    try:
        fixtures = conn.execute(
            """
            SELECT id, matchday, home_team, away_team, home_score, away_score,
                   status, goals_json, utc_date, home_logo, away_logo
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
        current_time = now_utc()
        for stored in fixtures:
            kickoff = parse_utc(stored["utc_date"])
            in_live_window = bool(
                kickoff
                and kickoff - timedelta(seconds=LIVE_WINDOW_BEFORE_SECONDS)
                <= current_time
                <= kickoff + timedelta(seconds=LIVE_WINDOW_AFTER_SECONDS)
            )
            already_live = stored["status"] in ("LIVE", "IN_PLAY", "PAUSED")
            needs_scorers = bool(
                not stored["goals_json"]
                and ((stored["home_score"] or 0) > 0 or (stored["away_score"] or 0) > 0)
            )
            needs_result_repair = bool(
                kickoff
                and current_time - timedelta(hours=48) <= kickoff <= current_time
                and (stored["home_score"] is None or stored["away_score"] is None)
            )
            if not (
                in_live_window
                or already_live
                or (
                    force_current_gameweek
                    and (needs_scorers or needs_result_repair)
                )
            ):
                continue

            home_slug = sportscore_team_slug(stored["home_team"])
            away_slug = sportscore_team_slug(stored["away_team"])
            stored_key = (
                normalized_team_name(stored["home_team"]),
                normalized_team_name(stored["away_team"]),
            )
            details = None
            for match_slug in (
                f"{home_slug}-vs-{away_slug}",
                f"{away_slug}-vs-{home_slug}",
            ):
                try:
                    candidate = get_sportscore_match_details({
                        "url": f"/football/match/{match_slug}/"
                    })
                except SportScoreError as exc:
                    if "HTTP 404" in str(exc):
                        continue
                    raise
                candidate_key = (
                    normalized_team_name(candidate.get("home")),
                    normalized_team_name(candidate.get("away")),
                )
                if candidate_key == stored_key:
                    details = candidate
                    break

            # A missing scorer archive must not stop live matches later in the
            # gameweek from refreshing.
            if details is None:
                continue

            raw_incidents = details.get("incidents")
            incidents = raw_incidents if isinstance(raw_incidents, list) else []
            goals = sportscore_goal_events(details)
            # An explicit empty incident list is authoritative. Persisting []
            # clears a goal that the provider has withdrawn after VAR rather
            # than retaining the earlier scorer and score indefinitely.
            goals_json = (
                json.dumps(goals)
                if isinstance(raw_incidents, list)
                else None
            )
            incidents_json = (
                json.dumps(incidents)
                if isinstance(raw_incidents, list)
                else None
            )
            minute_value, injury_time_value = sportscore_live_clock(details)
            home_penalty_score, away_penalty_score = provider_penalty_scores(details)

            conn.execute(
                """
                UPDATE fixtures
                SET status = ?,
                    home_score = COALESCE(?, home_score),
                    away_score = COALESCE(?, away_score),
                    minute = COALESCE(?, minute),
                    injury_time = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE injury_time
                    END,
                    match_phase = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE match_phase
                    END,
                    home_penalty_score = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE home_penalty_score
                    END,
                    away_penalty_score = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE away_penalty_score
                    END,
                    goals_json = COALESCE(?, goals_json),
                    incidents_json = COALESCE(?, incidents_json),
                    home_logo = COALESCE(?, home_logo),
                    away_logo = COALESCE(?, away_logo),
                    last_updated = ?,
                    live_data_source = 'SportScore'
                WHERE id = ?
                """,
                (
                    sportscore_fixture_status(details, stored["status"]),
                    details.get("home_score"),
                    details.get("away_score"),
                    minute_value,
                    minute_value,
                    injury_time_value,
                    provider_match_phase(details),
                    provider_match_phase(details),
                    home_penalty_score,
                    home_penalty_score,
                    away_penalty_score,
                    away_penalty_score,
                    goals_json,
                    incidents_json,
                    safe_team_logo_url(details.get("home_logo")),
                    safe_team_logo_url(details.get("away_logo")),
                    now_utc().isoformat(),
                    stored["id"],
                ),
            )
            updated += 1

        if updated:
            refresh_points(conn)
            record_live_position_snapshot(
                conn,
                fixtures[0]["matchday"],
            )
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


def current_gameweek_needs_result_repair():
    """Return true when a recently played fixture has lost either score."""
    conn = get_db()
    try:
        matchday = dashboard_current_gameweek(conn)
        if matchday is None:
            return False
        cutoff = (now_utc() - timedelta(hours=48)).isoformat()
        current = now_utc().isoformat()
        return bool(conn.execute(
            """SELECT 1 FROM fixtures
               WHERE season = ? AND matchday = ?
                 AND status != 'CANCELLED'
                 AND utc_date BETWEEN ? AND ?
                 AND (home_score IS NULL OR away_score IS NULL)
               LIMIT 1""",
            (SEASON, matchday, cutoff, current),
        ).fetchone())
    finally:
        conn.close()


def live_window_active():
    """
    Backwards-compatible helper used by tests/diagnostics.
    """
    return (
        next_api_refresh_delay()
        == LIVE_REFRESH_SECONDS
    )


def refresh_bigballs_shadow(force_events=False):
    """Record changed EPL provider states without touching live app data."""
    api_key = get_setting("bigballs_api_key")
    if not api_key:
        return 0
    conn = get_db()
    stored_count = 0
    try:
        matchday = dashboard_current_gameweek(conn)
        fixtures = conn.execute(
            """SELECT * FROM fixtures
               WHERE season = ? AND matchday = ?""",
            (SEASON, matchday),
        ).fetchall() if matchday is not None else []
        predictor_captured_at = now_utc().isoformat()
        for fixture in fixtures:
            previous_live = conn.execute(
                """SELECT status, home_score, away_score
                   FROM predictor_live_samples
                   WHERE fixture_id = ? ORDER BY id DESC LIMIT 1""",
                (fixture["id"],),
            ).fetchone()
            current_live_state = (
                fixture["status"], fixture["home_score"], fixture["away_score"]
            )
            previous_live_state = tuple(previous_live) if previous_live else None
            if current_live_state != previous_live_state:
                conn.execute(
                    """INSERT INTO predictor_live_samples(
                           fixture_id, captured_at, status,
                           home_score, away_score
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        fixture["id"], predictor_captured_at,
                        fixture["status"], fixture["home_score"],
                        fixture["away_score"],
                    ),
                )
        conn.commit()
        matches, meta = get_bigballs_premier_league_matches(api_key)
        if force_events and fixtures:
            archived, archived_meta = get_bigballs_stored_premier_league_matches(
                api_key,
                [str(row["utc_date"])[:10] for row in fixtures],
            )
            matches_by_id = {
                str(item.get("id")): item
                for item in matches
                if isinstance(item, dict) and item.get("id")
            }
            for item in archived:
                if isinstance(item, dict) and item.get("id"):
                    matches_by_id[str(item["id"])] = item
            matches = list(matches_by_id.values())
            if archived_meta:
                meta = archived_meta
        fixture_keys = {
            (
                normalized_team_name(row["home_team"]),
                normalized_team_name(row["away_team"]),
            ): row
            for row in fixtures
        }
        captured_at = now_utc().isoformat()
        for raw_match in matches:
            match = normalize_bigballs_match(raw_match)
            key = (
                normalized_team_name(match["home"]),
                normalized_team_name(match["away"]),
            )
            if key not in fixture_keys or not match["id"]:
                continue
            previous = conn.execute(
                """SELECT * FROM bigballs_shadow_samples
                   WHERE provider_match_id = ?
                   ORDER BY captured_at DESC, id DESC LIMIT 1""",
                (match["id"],),
            ).fetchone()
            current_state = (
                match["status"], match["home_score"], match["away_score"]
            )
            previous_state = (
                previous["status"], previous["home_score"], previous["away_score"]
            ) if previous else None
            events_json = previous["events_json"] if previous else None
            score_changed = previous is None or current_state[1:] != previous_state[1:]
            is_live = match["status"] in (
                "live", "in_progress", "in_play", "paused"
            )
            if force_events or is_live or (
                score_changed
                and any(value is not None for value in current_state[1:])
            ):
                try:
                    events, _ = get_bigballs_match_events(
                        api_key, match["id"], raw_match
                    )
                    events_json = json.dumps(events, separators=(",", ":"))
                except BigBallsAPIError as exc:
                    events_json = json.dumps({"error": str(exc)})
            previous_events = previous["events_json"] if previous else None
            if current_state == previous_state and events_json == previous_events:
                continue
            conn.execute(
                """INSERT INTO bigballs_shadow_samples(
                       provider_match_id, captured_at, home_team, away_team,
                       kickoff_utc, status, home_score, away_score,
                       events_json, raw_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    match["id"], captured_at, match["home"], match["away"],
                    match["kickoff_utc"], match["status"],
                    match["home_score"], match["away_score"], events_json,
                    json.dumps(raw_match, separators=(",", ":")),
                ),
            )
            stored_count += 1
        conn.commit()
        set_setting("last_bigballs_shadow_refresh", captured_at)
        set_setting("last_bigballs_shadow_source", str(meta.get("source") or ""))
        set_setting("last_bigballs_shadow_error", "")
        return stored_count
    finally:
        conn.close()


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

                stats_conn = get_db()
                try:
                    stats_gameweek = dashboard_current_gameweek(stats_conn)
                finally:
                    stats_conn.close()

                stats_marker = str(stats_gameweek or "")
                if (
                    stats_marker
                    and get_setting("match_stats_refreshed_gameweek")
                    != stats_marker
                ):
                    stats_imported = import_historical_results(
                        seasons=[SEASON],
                        include_championship=False,
                    )
                    set_setting("match_stats_refreshed_gameweek", stats_marker)
                    print(
                        f"[match-stats] Refreshed {stats_imported} "
                        f"Premier League result(s) for GW {stats_marker}",
                        flush=True,
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

        try:
            repaired = repair_missing_completed_results()
            if repaired:
                print(
                    f"[result-repair] Restored {repaired} fixture result(s)",
                    flush=True,
                )
        except Exception as exc:
            print(f"[result-repair] {exc}", flush=True)

        delay = next_api_refresh_delay()

        repair_results = current_gameweek_needs_result_repair()

        if delay == LIVE_REFRESH_SECONDS or repair_results:
            try:
                live_updates = import_live_matches_from_sportscore(
                    force_current_gameweek=repair_results,
                )
                set_setting("last_sportscore_error", "")
                print(
                    f"[SportScore] Updated {live_updates} live fixture(s)",
                    flush=True,
                )
            except Exception as exc:
                set_setting("last_sportscore_error", str(exc))
                set_setting("last_sportscore_error_at", now_utc().isoformat())
                print(f"[SportScore] {exc}", flush=True)

        if delay == LIVE_REFRESH_SECONDS and get_setting("bigballs_api_key"):
            try:
                shadow_updates = refresh_bigballs_shadow()
                print(
                    f"[BigBalls shadow] Recorded {shadow_updates} changed state(s)",
                    flush=True,
                )
            except Exception as exc:
                set_setting("last_bigballs_shadow_error", str(exc))
                set_setting("last_bigballs_shadow_error_at", now_utc().isoformat())
                print(f"[BigBalls shadow] {exc}", flush=True)

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

    harden_path_permissions(backup_path)
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

    harden_path_permissions(backup_path)
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
    query = (
        "trashed = false "
        "and appProperties has "
        "{ key='backupType' "
        "and value='pl-predictor-db' }"
    )

    page_token = None
    backups = []

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

        backups.extend(result.get("files", []))

        page_token = result.get(
            "nextPageToken"
        )

        if not page_token:
            break

    backups.sort(
        key=lambda item: (
            parse_utc(item.get("createdTime"))
            or datetime.min.replace(tzinfo=timezone.utc)
        ),
        reverse=True,
    )

    for item in backups[GOOGLE_BACKUP_LIMIT:]:
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



def next_unfinished_gameweek(conn):
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


def gameweek_open_at(conn, matchday):
    """Open a GW at 09:00 UK time the day after the previous GW ends."""
    previous = conn.execute(
        """
        SELECT matchday, MAX(utc_date) AS final_kickoff
        FROM fixtures
        WHERE season = ?
          AND matchday < ?
          AND matchday IS NOT NULL
        GROUP BY matchday
        ORDER BY matchday DESC
        LIMIT 1
        """,
        (SEASON, matchday),
    ).fetchone()
    if not previous or not previous["final_kickoff"]:
        return None

    final_kickoff = parse_utc(previous["final_kickoff"])
    if final_kickoff is None:
        return None

    final_local = final_kickoff.astimezone(UK)
    return (
        final_local.replace(hour=9, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )


def signal_current_gameweek(conn):
    matchday = next_unfinished_gameweek(conn)
    if matchday is None:
        return None
    opens_at = gameweek_open_at(conn, matchday)
    if opens_at is not None and now_utc() < opens_at.astimezone(timezone.utc):
        return None
    return matchday


def dashboard_current_gameweek(conn):
    """Keep the completed GW visible until the next GW opens."""
    matchday = next_unfinished_gameweek(conn)
    if matchday is None:
        row = conn.execute(
            """
            SELECT MAX(matchday) AS matchday
            FROM fixtures
            WHERE season = ? AND matchday IS NOT NULL
            """,
            (SEASON,),
        ).fetchone()
        return row["matchday"] if row else None

    opens_at = gameweek_open_at(conn, matchday)
    if opens_at is None or now_utc() >= opens_at.astimezone(timezone.utc):
        return matchday

    previous = conn.execute(
        """
        SELECT MAX(matchday) AS matchday
        FROM fixtures
        WHERE season = ? AND matchday < ?
        """,
        (SEASON, matchday),
    ).fetchone()
    return previous["matchday"] if previous and previous["matchday"] else matchday


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
        f"GW {matchday} - Put Your Pre-Dicks In",
        "",
        f"First Kick Off: {local_datetime(fixtures[0]['utc_date'])}",
        "",
        "Preddies: https://predictions.battleship.live",
    ])


def signal_next_gameweek_open_ready(matchday):
    """Announce the next GW from 09:00 UK time on the following day."""
    previous_matchday = matchday - 1
    if previous_matchday < 1:
        return True
    if get_setting("signal_last_results_gw") != str(previous_matchday):
        return False
    conn = get_db()
    try:
        opens_at = gameweek_open_at(conn, matchday)
    finally:
        conn.close()
    return (
        opens_at is None
        or now_utc() >= opens_at.astimezone(timezone.utc)
    )


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
                THEN 1 ELSE 0 END), 0) AS exact_scores,
            COALESCE(SUM(CASE
                WHEN f.season = ? AND f.matchday = ?
                 AND f.status = 'FINISHED'
                 AND NOT (p.home_score = f.home_score AND p.away_score = f.away_score)
                 AND (
                    (f.home_score = f.away_score AND p.home_score = p.away_score)
                    OR (f.home_score > f.away_score AND p.home_score > p.away_score)
                    OR (f.home_score < f.away_score AND p.home_score < p.away_score)
                 )
                THEN 1 ELSE 0 END), 0) AS correct_results
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
            correct_results DESC,
            pl.name COLLATE NOCASE
        """,
        (
            SEASON, matchday,
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
                THEN 1 ELSE 0 END), 0) AS exact_scores,
            COALESCE(SUM(CASE
                WHEN f.status = 'FINISHED'
                 AND NOT (p.home_score = f.home_score AND p.away_score = f.away_score)
                 AND (
                    (f.home_score = f.away_score AND p.home_score = p.away_score)
                    OR (f.home_score > f.away_score AND p.home_score > p.away_score)
                    OR (f.home_score < f.away_score AND p.home_score < p.away_score)
                 )
                THEN 1 ELSE 0 END), 0) AS correct_results
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
            correct_results DESC,
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
            else "💩" if index == 4
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
        prefix = (
            "🥇" if index == 1
            else "🥈" if index == 2
            else "🥉" if index == 3
            else "💩" if index == 4
            else f"{index}."
        )
        lines.append(
            f"{prefix} {row['name']} — {row['points']} pts"
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
                if (
                    settings["notify_gw_open"]
                    and signal_next_gameweek_open_ready(current_gw)
                ):
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
                    "signal_last_results_at",
                    now_utc().isoformat()
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


def resolve_reigning_champion_name(winner_name, players):
    """Map an archived display name to the champion's current player name."""
    archived = " ".join(str(winner_name or "").casefold().split())
    if not archived:
        return None
    exact = []
    compatible = []
    for player in players:
        current_name = str(player["name"] or "").strip()
        login_name = str(player["login_name"] or "").strip()
        aliases = {
            " ".join(current_name.casefold().split()),
            " ".join(login_name.casefold().split()),
        }
        if archived in aliases:
            exact.append(current_name)
        elif any(
            alias.startswith(f"{archived} ") or archived.startswith(f"{alias} ")
            for alias in aliases if alias
        ):
            compatible.append(current_name)
    matches = exact or compatible
    return matches[0] if len(matches) == 1 else winner_name


@app.context_processor
def inject_globals():
    reigning_premier_league_champion = None
    reigning_champions_league_champion = None
    reigning_head_to_head_champion = None
    conn = None
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT winner_name FROM season_archives ORDER BY season DESC LIMIT 1"
        ).fetchone()
        if row:
            players = conn.execute(
                "SELECT name, login_name FROM players ORDER BY id"
            ).fetchall()
            reigning_premier_league_champion = resolve_reigning_champion_name(
                row["winner_name"], players
            )
            side_champions = {}
            for winner in conn.execute(
                """SELECT competition, winner_name FROM competition_winners
                   ORDER BY id DESC"""
            ).fetchall():
                side_champions.setdefault(
                    winner["competition"],
                    resolve_reigning_champion_name(winner["winner_name"], players),
                )
            reigning_champions_league_champion = side_champions.get(
                "champions_league"
            )
            reigning_head_to_head_champion = side_champions.get("head_to_head")
    except sqlite3.Error:
        # First-run pages can render before the archive tables are available.
        reigning_premier_league_champion = None
    finally:
        if conn is not None:
            conn.close()

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
        "broadcaster_dark_logo_url": broadcaster_dark_logo_url,
        "team_badge_url": team_badge_url,
        "compact_record_name": compact_record_name,
        "is_logged_in": logged_in(),
        "reigning_premier_league_champion": reigning_premier_league_champion,
        "reigning_champions_league_champion": reigning_champions_league_champion,
        "reigning_head_to_head_champion": reigning_head_to_head_champion,
    }


@app.route("/team-badge")
def team_badge():
    if not logged_in():
        return Response(status=401)

    source = safe_team_logo_url(request.args.get("url"))
    parsed = urlparse(source or "")
    if parsed.scheme != "https" or parsed.hostname != "img.thesports.com":
        return Response(status=404)

    try:
        upstream = requests.get(
            source,
            headers={"User-Agent": "PremierLeaguePredictor/1.0"},
            timeout=10,
        )
        content_type = (upstream.headers.get("Content-Type") or "").split(";", 1)[0]
        if (
            upstream.status_code != 200
            or not content_type.startswith("image/")
            or len(upstream.content) > 1024 * 1024
        ):
            return Response(status=404)
    except requests.RequestException:
        return Response(status=404)

    response = Response(upstream.content, mimetype=content_type)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response



@app.context_processor
def inject_short_team_name():
    return {
        "short_team_name": short_team_name,
        "mobile_prediction_team_name": mobile_prediction_team_name,
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
        attempt_key = login_attempt_key()
        if login_is_rate_limited(attempt_key):
            flash(
                "Too many unsuccessful attempts. Please wait 10 minutes and try again.",
                "error",
            )
            return render_template(
                "login.html",
                registration_enabled=registration_enabled,
            ), 429

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
                OR LOWER(COALESCE(login_name, name)) = LOWER(?)
            )
            """,
            (
                identifier,
                identifier,
            ),
        ).fetchone()

        if player and verify_pin(pin, player["pin_hash"]):
            if is_legacy_pin_hash(player["pin_hash"]):
                conn.execute(
                    "UPDATE players SET pin_hash = ? WHERE id = ?",
                    (hash_pin(pin), player["id"]),
                )
                conn.commit()
            conn.close()
            clear_failed_logins(attempt_key)
            session.clear()

            session["player_id"] = player["id"]
            session["player_name"] = player["name"]
            session["admin"] = bool(
                player["admin"]
            )

            return redirect(
                "/dashboard"
            )

        conn.close()
        record_failed_login(attempt_key)

        flash(
            "Incorrect email, username or PIN.",
            "error"
        )

    return render_template(
        "login.html",
        registration_enabled=registration_enabled
    )


@app.route("/side-events")
def side_events():
    if not logged_in():
        return redirect("/")
    return redirect("/champions-league")


@app.route("/champions-league")
def champions_league():
    if not logged_in():
        return redirect("/")
    return render_template("side_events.html")


@app.route("/head-to-head")
def head_to_head():
    if not logged_in():
        return redirect("/")
    return render_template("head_to_head.html")


@app.route("/tegrity")
def tegrity():
    if not logged_in():
        return redirect("/")

    conn = get_db()
    chain_status = verify_prediction_audit_chain(conn)
    rows = []
    if not chain_status["valid"]:
        rows = conn.execute(
            """SELECT e.*
               FROM prediction_audit_events e
               JOIN fixtures f ON f.id = e.fixture_id
               WHERE f.season = ?
               ORDER BY e.id DESC
               LIMIT 300""",
            (SEASON,),
        ).fetchall()
    conn.close()

    action_labels = {
        "baseline": "Existing prediction registered",
        "submitted": "Prediction submitted",
        "updated": "Prediction changed",
        "dp_changed": "Double Points changed",
    }
    events = []
    for row in rows:
        event = dict(row)
        event["revealed"] = kickoff_passed(event["kickoff_utc"])
        event["changed_local"] = local_timestamp(event["changed_at"])
        event["action_label"] = action_labels.get(
            event["action"], "Prediction recorded"
        )
        event["commitment_short"] = event["score_commitment"][:16]
        event["event_hash_short"] = event["event_hash"][:16]
        if not event["revealed"]:
            event["home_score"] = None
            event["away_score"] = None
            event["commitment_salt"] = None
        events.append(event)

    return render_template(
        "tegrity.html",
        events=events,
        chain_status=chain_status,
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
    league_positions=None,
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

        league_position = (league_positions or {}).get(player["id"], 10**6)
        return (-points, league_position, player["name"].casefold())

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
        """SELECT id, name, login_name, email, admin,
                  COALESCE(hide_news_ticker, 0) AS hide_news_ticker
           FROM players WHERE id = ?""",
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
        hide_news_ticker = 1 if request.form.get("hide_news_ticker") == "1" else 0

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
                """UPDATE players SET name=?, email=?, pin_hash=?,
                          hide_news_ticker=? WHERE id=?""",
                (
                    name, email, hash_pin(pin), hide_news_ticker,
                    session["player_id"],
                )
            )
        else:
            conn.execute(
                """UPDATE players SET name=?, email=?, hide_news_ticker=?
                   WHERE id=?""",
                (name, email, hide_news_ticker, session["player_id"])
            )

        conn.commit()
        conn.close()
        session["player_name"] = name
        flash("Your account has been updated.", "success")
        return redirect("/account")

    conn.close()
    return render_template("account.html", player=player)



CHANGELOG_SECTION_ORDER = ("Important", "New", "Changes", "Fixes")
CHANGELOG_FIX_ORDER = (
    "UI", "Calculations", "Database", "Live Data",
    "Notifications", "Security", "Backups", "General",
)


def changelog_section_name(title):
    value = (title or "").strip().casefold()
    if value in {
        "important", "safety", "security / integrity",
        "locking / integrity", "privacy / processing",
    }:
        return "Important"
    if value in {"new", "added", "what's new since gameweek 1"}:
        return "New"
    if value in {"fixes", "fixed", "reliability", "diagnostics", "audit"}:
        return "Fixes"
    return "Changes"


def changelog_fix_category(item):
    value = (item or "").casefold()
    categories = (
        ("Backups", ("backup", "google drive", "restore backup")),
        ("Notifications", ("signal", "notification", "reminder", "message")),
        ("Security", (
            "security", "login", "password", "pin", "session",
            "rate limit", "authentication", "permission", "locked",
        )),
        ("Calculations", (
            "point", "scoring", "calculation", "tie-break", "tie break",
            "double points", " dp", "league ordering", "winner",
        )),
        ("Database", (
            "database", "sqlite", "migration", "query", "stored row",
            "historical fixture", "archive",
        )),
        ("Live Data", (
            "live", "provider", "sportscore", "football-data", "fixture",
            "score", "result", "scorer", "goal", "kick-off", "kickoff",
            "match clock", "red card",
        )),
        ("UI", (
            "layout", "display", "screen", "page", "card", "graph", "chart",
            "label", "button", "logo", "mobile", "alignment", "heading",
            "navigation", "typography", "highlight", "spacing", "colour",
        )),
    )
    for category, keywords in categories:
        if any(keyword in value for keyword in keywords):
            return category
    return "General"


def normalise_changelog_sections(sections):
    buckets = {title: [] for title in CHANGELOG_SECTION_ORDER}
    for section in sections:
        title = changelog_section_name(section.get("title"))
        buckets[title].extend(section.get("items") or [])

    normalised = []
    for title in CHANGELOG_SECTION_ORDER:
        items = buckets[title]
        if not items:
            continue
        section = {"title": title, "items": items, "groups": []}
        if title == "Fixes":
            grouped = {category: [] for category in CHANGELOG_FIX_ORDER}
            for item in items:
                grouped[changelog_fix_category(item)].append(item)
            section["groups"] = [
                {"title": category, "items": grouped[category]}
                for category in CHANGELOG_FIX_ORDER
                if grouped[category]
            ]
        normalised.append(section)
    return normalised


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

    for parsed_release in releases:
        parsed_release["sections"] = normalise_changelog_sections(
            parsed_release["sections"]
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

    conn = get_db()
    fee_players = conn.execute(
        "SELECT id, name, entry_fee_paid FROM players ORDER BY name COLLATE NOCASE"
    ).fetchall()
    conn.close()
    return render_template(
        "prize_structure.html",
        fee_players=fee_players,
        pot_total=len(fee_players) * 30,
        paid_total=sum(30 for player in fee_players if player["entry_fee_paid"]),
        can_manage_payments=is_treasurer(),
    )


@app.route("/prize-structure/payment/<int:player_id>", methods=["POST"])
def update_prize_payment(player_id):
    if not is_treasurer():
        return redirect("/")
    paid = 1 if request.form.get("paid") == "1" else 0
    conn = get_db()
    player = conn.execute("SELECT name FROM players WHERE id = ?", (player_id,)).fetchone()
    if player:
        conn.execute("UPDATE players SET entry_fee_paid = ? WHERE id = ?", (paid, player_id))
        conn.commit()
        flash(f"{player['name']} marked as {'paid' if paid else 'not paid'}.", "success")
    conn.close()
    return redirect("/prize-structure")


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
    side_winners = conn.execute(
        """SELECT competition, season_label, winner_name
           FROM competition_winners
           ORDER BY id DESC"""
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
        champions_league_winners=[row for row in side_winners if row["competition"] == "champions_league"],
        head_to_head_winners=[row for row in side_winners if row["competition"] == "head_to_head"],
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
@app.route("/league-stats")
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

    if request.path == "/stats":
        conn.close()
        avg_points = (
            round(personal["total_points"] / personal["predictions_made"], 2)
            if personal["predictions_made"]
            else 0
        )
        return render_template(
            "stats.html",
            personal=personal,
            best_gameweek=best_gameweek,
            avg_points=avg_points,
        )

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

    completed_gameweeks = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM (
            SELECT matchday
            FROM fixtures
            WHERE season = ? AND status != 'CANCELLED'
            GROUP BY matchday
            HAVING SUM(CASE WHEN status = 'FINISHED' THEN 0 ELSE 1 END) = 0
               AND SUM(CASE WHEN status = 'FINISHED' THEN 1 ELSE 0 END) > 0
        ) completed_rounds
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
        "league_stats.html",
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
        completed_gameweeks=completed_gameweeks["total"],
    )



@app.route("/dashboard")
def dashboard():
    if not logged_in():
        return redirect("/")

    conn = get_db()

    automatic_matchday = dashboard_current_gameweek(conn)

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
    dashboard_sources = []
    dashboard_live_table = []
    dashboard_position_chart = {"players": [], "snapshots": []}
    dashboard_gameweek_progress = ""
    dashboard_players = []
    dashboard_prediction_map = {}
    dashboard_reveal_map = {}
    dashboard_fixture_players = {}

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
            fixture["red_cards"] = fixture_red_cards(
                fixture.get("incidents_json"),
                fixture["home_team"],
                fixture["away_team"],
            )
            current_fixtures.append(fixture)

        # The stored fixture list is supplied by football-data.org when its
        # feed is configured; SportScore enriches matching fixtures with the
        # live clock, scorers and incidents. Keep this as one list-level
        # attribution rather than repeating it on every card.
        if get_setting("football_api_token"):
            dashboard_sources.append("football-data.org")
        if any(
            fixture.get("live_data_source") == "SportScore"
            or fixture.get("status") in ("LIVE", "IN_PLAY", "PAUSED")
            for fixture in current_fixtures
        ):
            dashboard_sources.insert(0, "SportScore")

        refresh_points(conn)
        record_live_position_snapshot(conn, current_matchday)
        conn.commit()
        players = conn.execute(
            "SELECT id, name FROM players ORDER BY name COLLATE NOCASE"
        ).fetchall()
        predictions = conn.execute(
            """SELECT p.player_id, p.fixture_id, p.home_score, p.away_score,
                      COALESCE(p.dp, 0) AS dp
               FROM predictions p
               JOIN fixtures f ON f.id = p.fixture_id
               WHERE f.season = ? AND f.matchday = ?""",
            (SEASON, current_matchday),
        ).fetchall()
        previous_league = overall_table_at_matchday(
            conn, current_matchday - 1
        )
        dashboard_live_table = build_live_table(
            fixture_rows, players, predictions, previous_league
        )
        dashboard_position_chart = live_position_chart(
            conn, current_matchday
        )
        dashboard_gameweek_progress = gameweek_progress_label(fixture_rows)
        dashboard_players = players
        dashboard_prediction_map = {
            (prediction["player_id"], prediction["fixture_id"]): prediction
            for prediction in predictions
        }
        dashboard_reveal_map = {
            fixture["id"]: fixture_is_locked(fixture)
            for fixture in current_fixtures
        }
        dashboard_fixture_players = {
            fixture["id"]: order_players_for_fixture(
                players,
                fixture,
                dashboard_prediction_map,
                dashboard_reveal_map[fixture["id"]],
                {
                    row["id"]: row["position"]
                    for row in dashboard_live_table
                },
            )
            for fixture in current_fixtures
        }

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
            ) AS exact_scores,

            COALESCE(
                SUM(
                    CASE
                    WHEN f.status = 'FINISHED'
                     AND NOT (
                        p.home_score = f.home_score
                        AND p.away_score = f.away_score
                     )
                     AND (
                        (f.home_score = f.away_score AND p.home_score = p.away_score)
                        OR (f.home_score > f.away_score AND p.home_score > p.away_score)
                        OR (f.home_score < f.away_score AND p.home_score < p.away_score)
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
            correct_results DESC,
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

    news_preference = conn.execute(
        """SELECT COALESCE(hide_news_ticker, 0) AS hide_news_ticker
           FROM players WHERE id = ?""",
        (session["player_id"],),
    ).fetchone()
    show_news_ticker = bool(
        news_preference and not news_preference["hide_news_ticker"]
    )

    conn.close()

    return render_template(
        "dashboard.html",
        news=premier_league_news() if show_news_ticker else None,
        show_news_ticker=show_news_ticker,
        current_matchday=current_matchday,
        current_fixtures=current_fixtures,
        total_points=row["total"],
        league_position=league_position,
        league_size=league_size,
        dashboard_has_live_fixtures=any(
            fixture["status"] in ("LIVE", "IN_PLAY", "PAUSED")
            for fixture in current_fixtures
        ),
        dashboard_sources=dashboard_sources,
        live_table=dashboard_live_table,
        position_chart=dashboard_position_chart,
        gameweek_progress=dashboard_gameweek_progress,
        live_gameweek_visible=live_gameweek_visible(current_fixtures),
        gameweek_predictions_open=gameweek_predictions_open(current_fixtures),
        players=dashboard_players,
        fixture_players=dashboard_fixture_players,
        prediction_map=dashboard_prediction_map,
        reveal_map=dashboard_reveal_map,
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
        ORDER BY matchday ASC
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

        fixture_ids_for_audit = [fixture["id"] for fixture in fixtures]
        audit_placeholders = ",".join("?" for _ in fixture_ids_for_audit)
        before_audit_rows = conn.execute(
            f"""SELECT fixture_id, home_score, away_score, COALESCE(dp, 0) AS dp
                FROM predictions
                WHERE player_id = ? AND fixture_id IN ({audit_placeholders})""",
            (session["player_id"], *fixture_ids_for_audit),
        ).fetchall()
        before_audit = {
            row["fixture_id"]: (
                row["home_score"], row["away_score"], row["dp"]
            )
            for row in before_audit_rows
        }

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
                    f"{short_team_name(fixture['home_team'])} v "
                    f"{short_team_name(fixture['away_team'])}."
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
                    f"{short_team_name(fixture['home_team'])} v "
                    f"{short_team_name(fixture['away_team'])}."
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

        after_audit_rows = conn.execute(
            f"""SELECT fixture_id, home_score, away_score, COALESCE(dp, 0) AS dp
                FROM predictions
                WHERE player_id = ? AND fixture_id IN ({audit_placeholders})""",
            (session["player_id"], *fixture_ids_for_audit),
        ).fetchall()
        audit_changed_at = now_utc().isoformat()
        for row in after_audit_rows:
            after_state = (row["home_score"], row["away_score"], row["dp"])
            before_state = before_audit.get(row["fixture_id"])
            if before_state == after_state:
                continue
            if before_state is None:
                audit_action = "submitted"
            elif before_state[:2] == after_state[:2]:
                audit_action = "dp_changed"
            else:
                audit_action = "updated"
            append_prediction_audit_event(
                conn,
                player_id=session["player_id"],
                fixture_id=row["fixture_id"],
                home_score=row["home_score"],
                away_score=row["away_score"],
                dp=row["dp"],
                action=audit_action,
                changed_at=audit_changed_at,
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

    show_match_stats = request.args.get("history") != "1"
    fixture_stats = (
        build_fixture_stats(conn, fixtures)
        if show_match_stats
        else {}
    )

    conn.close()

    return render_template(
        "predictions.html",
        fixtures=fixtures,
        matchday=matchday,
        locked_dp_fixture_id=locked_dp_fixture_id,
        fixture_stats=fixture_stats,
        show_match_stats=show_match_stats,
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

    # Reconcile the graph with the table on every view. This catches a final
    # provider update even if the background worker crossed directly from a
    # live refresh into its quiet interval before storing the last snapshot.
    refresh_points(conn)
    record_live_position_snapshot(conn, matchday)
    conn.commit()

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

    previous_league = overall_table_at_matchday(
        conn,
        matchday - 1,
    )

    prediction_map = {
        (p["player_id"], p["fixture_id"]): p
        for p in predictions
    }

    reveal_map = {
        fixture["id"]: fixture_is_locked(fixture)
        for fixture in fixtures
    }


    live_table = build_live_table(
        fixtures,
        players,
        predictions,
        previous_league,
    )
    league_positions = {
        row["id"]: row["position"]
        for row in live_table
    }
    fixture_players = {
        fixture["id"]: order_players_for_fixture(
            players,
            fixture,
            prediction_map,
            reveal_map[fixture["id"]],
            league_positions,
        )
        for fixture in fixtures
    }
    position_chart = live_position_chart(conn, matchday)
    conn.close()

    return render_template(
        "gameweek.html",
        matchday=matchday,
        fixtures=fixtures,
        players=players,
        fixture_players=fixture_players,
        prediction_map=prediction_map,
        reveal_map=reveal_map,
        live_table=live_table,
        gameweek_progress=gameweek_progress_label(fixtures),
        live_gameweek_visible=live_gameweek_visible(fixtures),
        position_chart=position_chart,
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
            correct_results DESC,
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
        player["id"]: {
            "id": player["id"],
            "name": compact_record_name(player["name"]),
            "positions": [],
        }
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
        send_signal_message("⚽ Preddies\n\nSignal integration is working! ✅")
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

    db_health = database_health(conn)

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
        db_health=db_health,
        last_database_optimize=(
            local_timestamp(get_setting("last_database_optimize"))
            if get_setting("last_database_optimize")
            else None
        ),
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
<p><small>Preddies v{APP_VERSION}</small></p>
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
        cloud_backup_limit=GOOGLE_BACKUP_LIMIT,
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
    harden_path_permissions(temp_path)

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
            email,
            admin,
            treasurer
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
        SELECT id, name, email, admin, treasurer
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
        treasurer_value = 1 if request.form.get("treasurer") == "1" else 0

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

        if treasurer_value:
            conn.execute("UPDATE players SET treasurer = 0")

        if pin:
            conn.execute(
                """
                UPDATE players
                SET
                    name = ?,
                    email = ?,
                    pin_hash = ?,
                    admin = ?,
                    treasurer = ?
                WHERE id = ?
                """,
                (
                    name,
                    email,
                    hash_pin(pin),
                    admin_value,
                    treasurer_value,
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
                    admin = ?,
                    treasurer = ?
                WHERE id = ?
                """,
                (
                    name,
                    email,
                    admin_value,
                    treasurer_value,
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

        if action == "bigballs_api":
            api_key = request.form.get("bigballs_api_key", "").strip()
            if api_key:
                set_setting("bigballs_api_key", api_key)
                flash(
                    "Big Balls Sports Data key saved for the read-only "
                    "Premier League shadow feed.",
                    "success",
                )
            else:
                flash("Please enter a Big Balls Sports Data API key.", "error")
            return redirect("/admin/settings")

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
        bigballs_configured=bool(get_setting("bigballs_api_key")),
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


@app.route("/admin/settings/bigballs-test", methods=["POST"])
def test_bigballs_api():
    if not is_admin():
        return redirect("/")
    try:
        data = test_bigballs_connection(get_setting("bigballs_api_key"))
        limits = data.get("limits") or {}
        daily = limits.get("per_day")
        suffix = f" Daily allowance: {daily}." if daily else ""
        flash(f"Big Balls Sports Data connection successful.{suffix}", "success")
    except BigBallsAPIError as exc:
        flash(str(exc), "error")
    return redirect("/admin/settings")








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


def shadow_feed_milestone(status):
    value = str(status or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if value in {"finished", "final", "ft", "full_time", "ended", "complete", "completed"}:
        return "FT"
    if value in {"ht", "half_time", "halftime", "interval", "paused"}:
        return "HT"
    if value in {"live", "in_play", "in_progress", "playing", "started"}:
        return "KO"
    return None


def shadow_timing_label(live_value, shadow_value):
    live_at = parse_utc(live_value)
    shadow_at = parse_utc(shadow_value)
    if live_at and shadow_at:
        lag_seconds = round((shadow_at - live_at).total_seconds())
        absolute_seconds = abs(lag_seconds)
        if absolute_seconds < 2:
            return "Same check"
        minutes, seconds = divmod(absolute_seconds, 60)
        duration = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        relation = "after" if lag_seconds > 0 else "before"
        return f"Big Balls {duration} {relation} Live"
    if live_at:
        return "Awaiting Big Balls"
    if shadow_at:
        return "Awaiting Live"
    return "Not observed"


@app.route("/admin/live-feed-test")
def admin_bigballs_shadow_test():
    if not is_admin():
        return redirect("/")
    conn = get_db()
    try:
        current_matchday = dashboard_current_gameweek(conn)
        available_matchdays = [
            row["matchday"] for row in conn.execute(
                """SELECT DISTINCT matchday FROM fixtures
                   WHERE season = ? AND matchday IS NOT NULL
                   ORDER BY matchday""",
                (SEASON,),
            ).fetchall()
        ]
        requested_matchday = request.args.get("matchday", type=int)
        matchday = (
            requested_matchday
            if requested_matchday in available_matchdays
            else current_matchday
        )
        previous_matchday = next(
            (value for value in reversed(available_matchdays) if value < matchday),
            None,
        ) if matchday is not None else None
        next_matchday = next(
            (value for value in available_matchdays if value > matchday),
            None,
        ) if matchday is not None else None
        fixtures = conn.execute(
            """SELECT * FROM fixtures
               WHERE season = ? AND matchday = ? ORDER BY utc_date""",
            (SEASON, matchday),
        ).fetchall() if matchday is not None else []
        latest_samples = conn.execute(
            """SELECT sample.* FROM bigballs_shadow_samples sample
               JOIN (
                   SELECT provider_match_id, MAX(id) AS latest_id
                   FROM bigballs_shadow_samples GROUP BY provider_match_id
               ) latest ON latest.latest_id = sample.id"""
        ).fetchall()
        event_samples = conn.execute(
            """SELECT * FROM bigballs_shadow_samples
               WHERE events_json IS NOT NULL
                 AND TRIM(events_json) NOT IN ('', '[]')
               ORDER BY id DESC"""
        ).fetchall()
        shadow_score_samples = conn.execute(
            """SELECT * FROM bigballs_shadow_samples
               WHERE home_score IS NOT NULL AND away_score IS NOT NULL
               ORDER BY captured_at, id"""
        ).fetchall()
        predictor_score_samples = conn.execute(
            """SELECT * FROM predictor_live_samples
               WHERE home_score IS NOT NULL AND away_score IS NOT NULL
               ORDER BY captured_at, id"""
        ).fetchall()
    finally:
        conn.close()
    samples_by_key = {
        (
            normalized_team_name(row["home_team"]),
            normalized_team_name(row["away_team"]),
        ): row
        for row in latest_samples
    }
    event_samples_by_key = {}
    for row in event_samples:
        row_key = (
            normalized_team_name(row["home_team"]),
            normalized_team_name(row["away_team"]),
        )
        event_samples_by_key.setdefault(row_key, row)
    shadow_milestones = {}
    for row in shadow_score_samples:
        milestone = shadow_feed_milestone(row["status"])
        if not milestone:
            continue
        row_key = (
            normalized_team_name(row["home_team"]),
            normalized_team_name(row["away_team"]),
        )
        shadow_milestones.setdefault((row_key, milestone), row["captured_at"])
    predictor_milestones = {}
    for row in predictor_score_samples:
        milestone = shadow_feed_milestone(row["status"])
        if milestone:
            predictor_milestones.setdefault(
                (row["fixture_id"], milestone), row["captured_at"]
            )
    shadow_changed_at = {}
    previous_shadow_score = {}
    for row in shadow_score_samples:
        row_key = (
            normalized_team_name(row["home_team"]),
            normalized_team_name(row["away_team"]),
        )
        score = (row["home_score"], row["away_score"])
        if row_key in previous_shadow_score and score != previous_shadow_score[row_key]:
            shadow_changed_at.setdefault((row_key, score), row["captured_at"])
        previous_shadow_score[row_key] = score
    predictor_changed_at = {}
    previous_predictor_score = {}
    for row in predictor_score_samples:
        fixture_id = row["fixture_id"]
        score = (row["home_score"], row["away_score"])
        if fixture_id in previous_predictor_score and score != previous_predictor_score[fixture_id]:
            predictor_changed_at.setdefault(
                (fixture_id, score), row["captured_at"]
            )
        previous_predictor_score[fixture_id] = score
    monitored = []
    for fixture in fixtures:
        key = (
            normalized_team_name(fixture["home_team"]),
            normalized_team_name(fixture["away_team"]),
        )
        sample = samples_by_key.get(key)
        events = []
        event_sample = event_samples_by_key.get(key) or sample
        if event_sample and event_sample["events_json"]:
            try:
                parsed = json.loads(event_sample["events_json"])
                events = parsed if isinstance(parsed, list) else []
            except (TypeError, ValueError):
                events = []
        provider_home_id = None
        provider_away_id = None
        if event_sample and event_sample["raw_json"]:
            try:
                raw_sample = json.loads(event_sample["raw_json"])
                raw_home = raw_sample.get("home") or raw_sample.get("home_team") or {}
                raw_away = raw_sample.get("away") or raw_sample.get("away_team") or {}
                if isinstance(raw_home, dict):
                    provider_home_id = raw_home.get("id") or raw_home.get("team_id")
                if isinstance(raw_away, dict):
                    provider_away_id = raw_away.get("id") or raw_away.get("team_id")
            except (AttributeError, TypeError, ValueError):
                pass
        display_events = {"home": [], "away": [], "other": []}
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(
                event.get("type") or event.get("event_type")
                or event.get("category") or event.get("kind") or ""
            ).casefold()
            description = str(
                event.get("description") or event.get("detail")
                or event.get("text") or ""
            ).strip()
            player = (
                event.get("player") or event.get("scorer")
                or event.get("participant") or event.get("athlete") or {}
            )
            if isinstance(player, dict):
                player = player.get("name") or player.get("display_name")
            if not player:
                player = (
                    event.get("player_name") or event.get("playerName")
                    or event.get("scorer_name") or event.get("name")
                )
            if not description:
                event_name = event_type.replace("_", " ").title() or "Match event"
                description = f"{event_name} — {player}" if player else event_name
            clock = event.get("clock")
            if isinstance(clock, dict):
                clock = (
                    clock.get("display") or clock.get("label")
                    or clock.get("minute") or clock.get("elapsed")
                    or clock.get("match_minute") or clock.get("value")
                )
            if clock is None:
                clock = (
                    event.get("minute") or event.get("match_minute")
                    or event.get("elapsed") or event.get("elapsed_time")
                    or event.get("time")
                )
            if isinstance(clock, dict):
                clock = (
                    clock.get("display") or clock.get("label")
                    or clock.get("minute") or clock.get("elapsed")
                    or clock.get("match_minute") or clock.get("value")
                )
            if clock is not None:
                clock = str(clock).strip().rstrip("'")

            event_text = " ".join(str(value) for value in (
                event_type,
                description,
                event.get("event_detail"),
                event.get("goal_type"),
                event.get("subtype"),
            ) if value).casefold().replace("_", " ").replace("-", " ")
            is_red = "red" in event_text and (
                "card" in event_text or "red card" in event_text
            )
            is_goal = "goal" in event_text or event_type == "score"
            is_penalty = any(event.get(key) is True for key in (
                "is_penalty", "penalty", "penalty_goal"
            )) or "penalty" in event_text or " pen " in f" {event_text} "
            is_own_goal = any(event.get(key) is True for key in (
                "is_own_goal", "own_goal", "ownGoal"
            )) or "own goal" in event_text or " og " in f" {event_text} "

            team = (
                event.get("team") or event.get("club")
                or event.get("participant_team") or {}
            )
            team_name = ""
            team_id = event.get("team_id") or event.get("teamId")
            if isinstance(team, dict):
                nested_team = team.get("team")
                if isinstance(nested_team, dict):
                    team = nested_team
                team_name = str(
                    team.get("name") or team.get("display_name")
                    or team.get("short_name") or ""
                )
                team_id = team.get("id") or team.get("team_id") or team_id
            elif team:
                team_name = str(team)
            team_name = str(
                event.get("team_name") or event.get("teamName") or team_name
            ).strip()
            side = str(
                event.get("side") or event.get("home_away")
                or event.get("homeAway") or event.get("team_side")
                or (team.get("side") if isinstance(team, dict) else "")
                or ""
            ).casefold()
            is_home = event.get("is_home")
            if is_home is None:
                is_home = event.get("is_home_team")
            if not side and is_home is not None:
                side = "home" if is_home else "away"
            fixture_keys = set(fixture.keys())
            home_id = fixture["home_team_id"] if "home_team_id" in fixture_keys else None
            away_id = fixture["away_team_id"] if "away_team_id" in fixture_keys else None
            if side not in {"home", "away"}:
                if (
                    team_id is not None and provider_home_id is not None
                    and str(team_id) == str(provider_home_id)
                ):
                    side = "home"
                elif (
                    team_id is not None and provider_away_id is not None
                    and str(team_id) == str(provider_away_id)
                ):
                    side = "away"
                elif team_id is not None and home_id is not None and str(team_id) == str(home_id):
                    side = "home"
                elif team_id is not None and away_id is not None and str(team_id) == str(away_id):
                    side = "away"
                elif team_name and normalized_team_name(team_name) == normalized_team_name(fixture["home_team"]):
                    side = "home"
                elif team_name and normalized_team_name(team_name) == normalized_team_name(fixture["away_team"]):
                    side = "away"
                else:
                    side = "other"

            if is_goal or is_red:
                label = str(player or description or "Match event").strip()
                # Some payloads put a ready-made "Goal — Player" label in
                # description. Keep only the useful player portion.
                if "—" in label:
                    label = label.rsplit("—", 1)[-1].strip()
                marker = ""
                if is_own_goal:
                    marker = "og"
                elif is_penalty:
                    marker = "(Pen)"
                display_events[side].append({
                    "icon": "🟥" if is_red else "⚽",
                    "player": label,
                    "clock": clock,
                    "marker": marker,
                })
        known_goal_counts = {
            side: sum(
                1 for event in display_events[side]
                if event["icon"] == "⚽"
            )
            for side in ("home", "away")
        }
        goal_targets = {
            "home": sample["home_score"] if sample else None,
            "away": sample["away_score"] if sample else None,
        }
        unresolved_goals = [
            event for event in display_events["other"]
            if event["icon"] == "⚽"
        ]
        for event in list(unresolved_goals):
            deficits = {
                side: max(0, goal_targets[side] - known_goal_counts[side])
                for side in ("home", "away")
                if goal_targets[side] is not None
            }
            possible_sides = [
                side for side, deficit in deficits.items() if deficit > 0
            ]
            if len(possible_sides) != 1:
                break
            inferred_side = possible_sides[0]
            display_events["other"].remove(event)
            display_events[inferred_side].append(event)
            known_goal_counts[inferred_side] += 1
        for side in ("home", "away", "other"):
            grouped_events = []
            grouped_by_player = {}
            for event in display_events[side]:
                group_key = (event["icon"], event["player"].casefold())
                grouped = grouped_by_player.get(group_key)
                if grouped is None:
                    grouped = {
                        "icon": event["icon"],
                        "player": event["player"],
                        "moments": [],
                    }
                    grouped_by_player[group_key] = grouped
                    grouped_events.append(grouped)
                moment = ""
                if event["clock"]:
                    moment = f"{event['clock']}'"
                if event["marker"]:
                    moment = f"{moment} {event['marker']}".strip()
                if moment and moment not in grouped["moments"]:
                    grouped["moments"].append(moment)
            display_events[side] = grouped_events
        score_matches = bool(
            sample
            and sample["home_score"] == fixture["home_score"]
            and sample["away_score"] == fixture["away_score"]
        )
        update_lag_label = None
        if score_matches:
            score = (sample["home_score"], sample["away_score"])
            live_changed = parse_utc(
                predictor_changed_at.get((fixture["id"], score))
            )
            shadow_changed = parse_utc(shadow_changed_at.get((key, score)))
            if live_changed and shadow_changed:
                lag_seconds = round((shadow_changed - live_changed).total_seconds())
                absolute_seconds = abs(lag_seconds)
                if absolute_seconds < 2:
                    update_lag_label = "Updated on the same check as Live"
                else:
                    minutes, seconds = divmod(absolute_seconds, 60)
                    duration = (
                        f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
                    )
                    relation = "after" if lag_seconds > 0 else "before"
                    update_lag_label = f"Big Balls updated {duration} {relation} Live"
        timing_rows = []
        for milestone in ("KO", "HT", "FT"):
            live_at = predictor_milestones.get((fixture["id"], milestone))
            shadow_at = shadow_milestones.get((key, milestone))
            timing_rows.append({
                "milestone": milestone,
                "live_at": local_timestamp(live_at) if live_at else None,
                "shadow_at": local_timestamp(shadow_at) if shadow_at else None,
                "label": shadow_timing_label(live_at, shadow_at),
            })
        monitored.append({
            "fixture": dict(fixture),
            "sample": dict(sample) if sample else None,
            "score_matches": score_matches,
            "update_lag_label": update_lag_label,
            "timing_rows": timing_rows,
            "events": display_events,
        })
    return render_template(
        "live_feed_test.html",
        monitored=monitored,
        configured=bool(get_setting("bigballs_api_key")),
        matchday=matchday,
        current_matchday=current_matchday,
        previous_matchday=previous_matchday,
        next_matchday=next_matchday,
        last_refresh=(
            local_timestamp(get_setting("last_bigballs_shadow_refresh"))
            if get_setting("last_bigballs_shadow_refresh") else None
        ),
        last_error=get_setting("last_bigballs_shadow_error"),
        source=get_setting("last_bigballs_shadow_source"),
        local_timestamp=local_timestamp,
    )


@app.route("/admin/live-feed-test/refresh", methods=["POST"])
def admin_bigballs_shadow_refresh():
    if not is_admin():
        return redirect("/")
    try:
        changed = refresh_bigballs_shadow(force_events=True)
        flash(f"Shadow feed checked; {changed} changed state(s) recorded.", "success")
    except BigBallsAPIError as exc:
        flash(str(exc), "error")
    return redirect("/admin/live-feed-test")


def _retired_champions_league_live_feed_test():
    if not is_admin():
        return redirect("/")

    manual_match = request.args.get("match", "").strip()
    requested_slugs = [
        value.strip().casefold()
        for value in request.args.getlist("slug")
        if value.strip()
    ][:6]
    discovery_error = None
    football_data_error = None
    football_data_matches = []
    manual_requested = bool(manual_match or requested_slugs)
    if manual_match:
        parsed = urlparse(manual_match)
        candidate = None
        if parsed.scheme or parsed.netloc:
            allowed_hosts = {"sportscore.com", "www.sportscore.com"}
            if parsed.scheme not in ("http", "https") or parsed.hostname not in allowed_hosts:
                discovery_error = "Enter a SportScore match URL or a match slug."
            else:
                candidate = parsed.path.rstrip("/").split("/")[-1]
        else:
            candidate = manual_match.strip("/").split("/")[-1]
        candidate = candidate.casefold() if candidate else ""
        if candidate and re.fullmatch(r"[a-z0-9-]+-vs-[a-z0-9-]+", candidate):
            requested_slugs.insert(0, candidate)
            requested_slugs = list(dict.fromkeys(requested_slugs))[:6]
        elif discovery_error is None:
            discovery_error = "Enter a valid match slug such as lask-vs-celtic."

    football_token = get_setting("football_api_token")
    if football_token:
        try:
            football_data_matches = get_football_champions_league_matches(
                football_token, season=SEASON
            )
        except FootballAPIError as exc:
            football_data_error = str(exc)

    if requested_slugs:
        configured = [
            (
                slug,
                slug.replace("-vs-", " v ").replace("-", " ").title(),
                None,
            )
            for slug in requested_slugs
        ]
    elif manual_requested:
        configured = []
    else:
        try:
            sportscore_discovered = [
                match for match in get_sportscore_champions_league_matches()
                if "uefa champions league" in (
                    match.get("competition") or ""
                ).casefold()
            ]
        except Exception as exc:
            sportscore_discovered = []
            discovery_error = str(exc)
        discovered = list(sportscore_discovered)
        discovered_keys = {
            (
                normalized_team_name(match.get("home")),
                normalized_team_name(match.get("away")),
            )
            for match in discovered
        }
        for football_match in football_data_matches:
            converted = football_data_diagnostic_details(football_match)
            key = (
                normalized_team_name(converted.get("home")),
                normalized_team_name(converted.get("away")),
            )
            if key not in discovered_keys:
                discovered.append(converted)
                discovered_keys.add(key)
        live = [match for match in discovered if match.get("status") == "live"]
        upcoming = [
            match for match in discovered
            if match.get("status") == "upcoming"
        ]
        active = live + upcoming
        selected = active or discovered
        configured = []
        for match in selected:
            slug = (match.get("url") or "").rstrip("/").split("/")[-1]
            label = (
                f"{match.get('home') or 'Home'} v "
                f"{match.get('away') or 'Away'}"
            )
            if slug:
                configured.append((slug, label, match))
    monitored = []

    for slug, label, discovered_match in configured:
        item = {"slug": slug, "label": label, "error": None}
        if not re.fullmatch(r"[a-z0-9-]+-vs-[a-z0-9-]+", slug):
            item["error"] = "Enter a valid match slug such as lask-vs-celtic."
            monitored.append(item)
            continue
        try:
            details = (
                discovered_match
                if discovered_match and discovered_match.get("_details_loaded")
                else get_sportscore_match_details(
                    discovered_match
                    or {"url": f"/football/match/{slug}/"}
                )
            )
            competition = details.get("competition") or ""
            if (
                not manual_requested
                and "uefa champions league" not in competition.casefold()
            ):
                raise SportScoreError(
                    "This match is not listed as UEFA Champions League by SportScore."
                )
            stored_fixture = stored_fixture_for_teams(
                details.get("home") or "",
                details.get("away") or "",
            )
            football_fixture = football_data_fixture_for_teams(
                football_data_matches,
                details.get("home") or "",
                details.get("away") or "",
            )
            football_details = (
                football_data_diagnostic_details(football_fixture)
                if football_fixture else None
            )
            minute, injury_time = sportscore_live_clock(details)
            if football_details:
                football_minute, football_injury_time = sportscore_live_clock(
                    football_details
                )
                if minute is None:
                    minute = football_minute
                    injury_time = football_injury_time
                elif injury_time is None and football_minute == minute:
                    injury_time = football_injury_time
            if stored_fixture:
                if minute is None:
                    minute = stored_fixture.get("minute")
                    injury_time = stored_fixture.get("injury_time")
                elif (
                    injury_time is None
                    and stored_fixture.get("minute") == minute
                ):
                    injury_time = stored_fixture.get("injury_time")
            raw_status = (details.get("status") or "unknown").casefold()
            normalized_status = sportscore_fixture_status(
                details,
                stored_fixture.get("status") if stored_fixture else None,
            )
            match_phase = provider_match_phase(details) or (
                stored_fixture.get("match_phase") if stored_fixture else None
            )
            if normalized_status == "PAUSED":
                status_text = (
                    "ET HT" if match_phase in (
                        "EXTRA_TIME", "EXTRA_TIME_HALF_TIME"
                    ) else "HT"
                )
            elif raw_status == "live":
                if match_phase == "PENALTIES":
                    status_text = "PENS"
                else:
                    status_text = "ET" if match_phase == "EXTRA_TIME" else "LIVE"
                    if minute is not None:
                        status_text += f" {minute}"
                        if injury_time is not None:
                            status_text += f"+{injury_time}"
                        status_text += "'"
            elif raw_status == "finished":
                status_text = "FT (PENS)" if match_phase == "PENALTIES" else (
                    "AET" if match_phase in (
                        "EXTRA_TIME", "EXTRA_TIME_HALF_TIME"
                    ) else "FT"
                )
            else:
                status_text = details.get("status_text") or raw_status.upper()
            goals = sportscore_goal_events(details)
            if not goals and football_details:
                goals = football_details.get("incidents") or []
            goals_json = json.dumps(goals) if goals else (
                stored_fixture.get("goals_json") if stored_fixture else None
            )
            incidents = details.get("incidents") or []
            if not incidents and football_details:
                incidents = football_details.get("incidents") or []
            if not incidents and stored_fixture and stored_fixture.get("incidents_json"):
                try:
                    incidents = json.loads(stored_fixture["incidents_json"])
                except (TypeError, ValueError):
                    incidents = []
            home_score = details.get("home_score")
            away_score = details.get("away_score")
            if football_details:
                if home_score is None:
                    home_score = football_details.get("home_score")
                if away_score is None:
                    away_score = football_details.get("away_score")
            if stored_fixture:
                if home_score is None:
                    home_score = stored_fixture.get("home_score")
                if away_score is None:
                    away_score = stored_fixture.get("away_score")
            sources = list(details.get("_diagnostic_sources") or ["SportScore"])
            if football_details and "football-data.org" not in sources:
                sources.append("football-data.org")
            if stored_fixture:
                stored_source = (
                    stored_fixture.get("live_data_source")
                    or "Predictor stored fixture"
                )
                if stored_source not in sources:
                    sources.append(stored_source)
            kickoff_text = (
                local_datetime(details.get("time"))
                if details.get("time")
                else "Kickoff time unavailable"
            )
            item.update({
                "home": details.get("home") or "Home",
                "away": details.get("away") or "Away",
                "home_logo": safe_team_logo_url(details.get("home_logo")),
                "away_logo": safe_team_logo_url(details.get("away_logo")),
                "home_score": home_score,
                "away_score": away_score,
                "status": raw_status,
                "display_status": normalized_status,
                "status_label": status_text,
                "submeta": kickoff_text if raw_status == "upcoming" else None,
                "broadcaster": (
                    stored_fixture.get("broadcaster")
                    if stored_fixture else None
                ),
                "raw_minute": details.get("live_minute"),
                "competition": competition,
                "sources": sources,
                "scorers": fixture_scorers(
                    goals_json,
                    details.get("home") or "",
                    details.get("away") or "",
                ),
                "red_cards": fixture_red_cards(
                    incidents,
                    details.get("home") or "",
                    details.get("away") or "",
                ),
            })
        except Exception as exc:
            item["error"] = str(exc)
        monitored.append(item)

    monitored_sources = []
    for item in monitored:
        for source in item.get("sources") or []:
            if source in ("SportScore", "football-data.org") and source not in monitored_sources:
                monitored_sources.append(source)

    return render_template(
        "live_feed_test.html",
        monitored=monitored,
        discovery_error=discovery_error,
        football_data_error=football_data_error,
        football_data_enabled=bool(football_token),
        automatic_discovery=not manual_requested,
        manual_match=manual_match,
        checked_at=local_datetime(now_utc().isoformat()),
        monitored_sources=monitored_sources,
    )


@app.route("/admin/database/optimize", methods=["POST"])
def admin_optimize_database():
    if not is_admin():
        return redirect("/")

    try:
        backup_path = create_automatic_backup()
        validate_predictor_database(backup_path, require_users=True)
    except Exception as exc:
        flash(
            "Database optimization cancelled because its safety backup "
            f"could not be created and verified: {exc}",
            "error",
        )
        return redirect("/admin")

    before = database_health()
    conn = sqlite3.connect(DB, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout = 60000")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Integrity check failed: {integrity}")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
        conn.commit()
    except Exception as exc:
        flash(f"Database optimization failed: {exc}", "error")
        return redirect("/admin")
    finally:
        conn.close()

    after = database_health()
    reclaimed = max(0, before["database_bytes"] - after["database_bytes"])
    set_setting("last_database_optimize", now_utc().isoformat())
    flash(
        "Database optimization completed. "
        f"Safety backup: {os.path.basename(backup_path)}. "
        f"Active database: {before['database_size']} → {after['database_size']} "
        f"({format_file_size(reclaimed)} reclaimed).",
        "success",
    )
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
        target=team_logo_worker,
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
