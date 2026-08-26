import re
import unicodedata
from datetime import datetime, timezone
from html import unescape
import requests

API_BASE = "https://sportscore.com/api/widget"
CHAMPIONS_LEAGUE_URL = (
    "https://sportscore.com/football/competition/world/"
    "uefa-champions-league/z8yomo4h7wq0j6l/"
)


class SportScoreError(Exception):
    pass


def _get(path, params):
    try:
        response = requests.get(
            f"{API_BASE}/{path}/",
            params={**params, "sport": "football", "src": "pl-predictions"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise SportScoreError("SportScore is temporarily unavailable.") from exc
    if response.status_code != 200:
        raise SportScoreError(f"SportScore returned HTTP {response.status_code}.")
    return response.json()


def get_live_matches():
    payload = _get("matches", {"limit": 50})
    return [
        match for match in payload.get("matches", [])
        if match.get("status") in ("upcoming", "live", "finished")
    ]


def _match_slug(home, away):
    def team_slug(value):
        value = (value or "").replace("ø", "o").replace("Ø", "O")
        value = unicodedata.normalize("NFKD", value or "")
        value = value.encode("ascii", "ignore").decode("ascii").casefold()
        return re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return f"{team_slug(home)}-vs-{team_slug(away)}"


def _competition_upcoming_matches(page_html):
    """Read future fixture rows that the public match API cannot disambiguate."""
    anchor_pattern = re.compile(
        r'<a\s+href="(?P<url>/football/match/[a-z0-9-]+/)"\s+'
        r'class="sc-stretched-link"\s+aria-label="(?P<label>[^"]+)"',
        re.I,
    )
    anchors = list(anchor_pattern.finditer(page_html or ""))
    upcoming = []
    seen = set()
    now = datetime.now(timezone.utc)
    for index, anchor in enumerate(anchors):
        label = unescape(anchor.group("label"))
        if "— UEFA Champions League" not in label:
            continue
        teams = label.rsplit(" — ", 1)[0]
        if " vs " not in teams:
            continue
        home, away = teams.split(" vs ", 1)
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(page_html)
        row_html = page_html[anchor.end():end]
        kickoff_match = re.search(r'data-utc="([^"]+)"', row_html, re.I)
        if not kickoff_match:
            continue
        kickoff_text = unescape(kickoff_match.group(1))
        try:
            kickoff = datetime.fromisoformat(kickoff_text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        if kickoff <= now:
            continue
        key = (home, away, kickoff.isoformat())
        if key in seen:
            continue
        seen.add(key)
        logo_urls = []
        for image_tag in re.findall(r"<img\b[^>]*>", row_html, re.I):
            if not re.search(r'alt="[^"]+ logo"', image_tag, re.I):
                continue
            source = re.search(r'src="([^"]+)"', image_tag, re.I)
            if source and source.group(1) not in logo_urls:
                logo_urls.append(unescape(source.group(1)))
        upcoming.append({
            "home": home,
            "away": away,
            "home_logo": logo_urls[0] if logo_urls else None,
            "away_logo": logo_urls[1] if len(logo_urls) > 1 else None,
            "home_score": None,
            "away_score": None,
            "status": "upcoming",
            "status_text": "Not started",
            "time": kickoff.isoformat(),
            "competition": "UEFA Champions League",
            "url": anchor.group("url"),
            "_details_loaded": True,
        })
    return upcoming


def get_champions_league_matches(limit=None):
    """Discover all of today's UEFA Champions League matches."""
    try:
        response = requests.get(
            CHAMPIONS_LEAGUE_URL,
            headers={"User-Agent": "PremierLeaguePredictor/1.0"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise SportScoreError("SportScore is temporarily unavailable.") from exc
    if response.status_code != 200:
        raise SportScoreError(f"SportScore returned HTTP {response.status_code}.")

    paths = re.findall(
        r'href="(/football/match/[a-z0-9-]+/)"\s*'
        r'[^>]*aria-label="[^"]+— UEFA Champions League"',
        response.text,
        flags=re.I,
    )

    # The competition page initially renders only part of its fixture list.
    # Its bracket feed contains the full current round, so use the latest
    # matchups to discover the remaining match-detail slugs.
    try:
        bracket = _get("bracket", {"slug": "uefa-champions-league"})
        matchups = [
            matchup
            for round_data in bracket.get("rounds", [])
            for matchup in round_data.get("matchups", [])
        ]
        for matchup in matchups[-16:]:
            for home, away in (
                (matchup.get("home"), matchup.get("away")),
                (matchup.get("away"), matchup.get("home")),
            ):
                slug = _match_slug(home, away)
                if slug:
                    paths.append(f"/football/match/{slug}/")
    except SportScoreError:
        pass

    matches = _competition_upcoming_matches(response.text)
    seen = set()
    seen_matches = {
        (match.get("home"), match.get("away"), match.get("time"))
        for match in matches
    }
    today = datetime.now(timezone.utc).date()
    for path in paths:
        slug = path.rstrip("/").split("/")[-1]
        if slug in seen:
            continue
        seen.add(slug)
        try:
            details = get_match_details({"url": path})
        except SportScoreError:
            continue
        if "uefa champions league" not in (
            details.get("competition") or ""
        ).casefold():
            continue
        try:
            match_date = datetime.fromisoformat(
                (details.get("time") or "").replace("Z", "+00:00")
            ).date()
        except ValueError:
            continue
        if match_date != today:
            continue
        match_key = (
            details.get("home"),
            details.get("away"),
            details.get("time"),
        )
        if match_key in seen_matches:
            continue
        seen_matches.add(match_key)
        details["_details_loaded"] = True
        matches.append(details)
        if limit is not None and len(matches) >= limit:
            break
    status_order = {"live": 0, "upcoming": 1, "finished": 2}
    return sorted(
        matches,
        key=lambda match: (
            status_order.get(match.get("status"), 3),
            match.get("time") or "",
        ),
    )


def get_team_matches(team_slug):
    payload = _get("team", {"slug": team_slug, "limit": 10})
    if isinstance(payload.get("matches"), list):
        return payload["matches"]
    if isinstance(payload.get("fixtures"), list):
        return payload["fixtures"]
    team = payload.get("team") or {}
    return team.get("matches") or team.get("fixtures") or []


def get_team_logo(team_slug):
    """Return a badge from SportScore's team record or one of its matches."""
    payload = _get("team", {"slug": team_slug, "limit": 10})
    team = payload.get("team") or {}
    if team.get("logo"):
        return team["logo"]

    team_name = (team.get("name") or "").strip().casefold()
    matches = payload.get("matches") or payload.get("fixtures") or []
    for match in matches:
        if (match.get("home") or "").strip().casefold() == team_name:
            logo = match.get("home_logo")
        elif (match.get("away") or "").strip().casefold() == team_name:
            logo = match.get("away_logo")
        else:
            continue
        if logo:
            return logo
    return None


def snapshot_live_minute(snapshot, now=None):
    """Mirror SportScore's browser clock using its live phase kickoff."""
    try:
        status_id = int(((snapshot or {}).get("status") or {}).get("id") or 0)
    except (TypeError, ValueError):
        return None
    if status_id not in (2, 4):
        return None
    kickoff = (snapshot or {}).get("kickoff")
    try:
        kickoff = float(kickoff)
    except (TypeError, ValueError):
        return None
    if kickoff < 1_000_000_000_000:
        kickoff *= 1000
    current = now or datetime.now(timezone.utc)
    elapsed = int((current.timestamp() * 1000 - kickoff) // 60000)
    if elapsed < 0:
        return None
    if status_id == 2:
        minute = elapsed + 1
        return f"45+{minute - 45}" if minute > 45 else str(minute)
    minute = elapsed + 46
    return f"90+{minute - 90}" if minute > 90 else str(minute)


def _live_snapshot(slug):
    try:
        response = requests.get(
            f"https://sportscore.com/football/match/{slug}/live/",
            headers={
                "User-Agent": "PremierLeaguePredictor/1.0",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15,
        )
        if response.status_code == 200:
            return response.json()
    except (requests.RequestException, ValueError, AttributeError):
        pass
    return None


def get_match_details(match):
    slug = (match.get("url") or "").rstrip("/").split("/")[-1]
    if not slug or not re.fullmatch(r"[a-z0-9-]+", slug):
        return match
    details = _get("match", {"slug": slug}).get("match") or match
    if (details.get("status") or "").casefold() == "live":
        snapshot = _live_snapshot(slug)
        if snapshot and snapshot.get("ok"):
            live_minute = snapshot_live_minute(snapshot)
            if live_minute:
                details["live_minute"] = live_minute
                details["status_text"] = live_minute
            score = snapshot.get("score") or {}
            if score.get("home") is not None:
                details["home_score"] = score["home"]
            if score.get("away") is not None:
                details["away_score"] = score["away"]
    return details


def goal_events(match):
    goals = []
    for incident in match.get("incidents") or []:
        if not incident.get("is_goal"):
            continue
        incident_type = (incident.get("type") or "").casefold()
        goal_type = "OWN_GOAL" if "own" in incident_type else (
            "PENALTY" if "pen" in incident_type else "REGULAR"
        )
        side = incident.get("side")
        team_name = match.get("home") if side == "home" else match.get("away")
        goals.append({
            "minute": incident.get("time"),
            "injuryTime": None,
            "type": goal_type,
            "team": {"name": team_name},
            "scorer": {"name": incident.get("player")},
        })
    return goals
