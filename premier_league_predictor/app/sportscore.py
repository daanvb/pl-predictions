import re
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


def get_champions_league_matches(limit=6):
    """Discover current UEFA Champions League matches from its competition page."""
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
    matches = []
    seen = set()
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
        details["_details_loaded"] = True
        matches.append(details)
        if len(matches) >= limit:
            break
    return matches


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


def get_match_details(match):
    slug = (match.get("url") or "").rstrip("/").split("/")[-1]
    if not slug or not re.fullmatch(r"[a-z0-9-]+", slug):
        return match
    return (_get("match", {"slug": slug}).get("match") or match)


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
