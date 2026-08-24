import requests


API_BASE = "https://v3.football.api-sports.io"
PREMIER_LEAGUE_ID = 39


class APIFootballError(Exception):
    pass


def headers(token):
    return {
        "x-apisports-key": token,
        "Accept": "application/json",
    }


def _request(token, path, params=None):
    if not token:
        raise APIFootballError("No API-Football token has been configured.")

    try:
        response = requests.get(
            f"{API_BASE}{path}",
            headers=headers(token),
            params=params or {},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise APIFootballError("API-Football is temporarily unavailable.") from exc

    if response.status_code in (401, 403):
        raise APIFootballError("API-Football token was rejected.")
    if response.status_code == 429:
        raise APIFootballError("API-Football daily or rate limit has been reached.")
    if response.status_code != 200:
        raise APIFootballError(
            f"API-Football returned HTTP {response.status_code}."
        )

    payload = response.json()
    errors = payload.get("errors") or {}
    if errors:
        if isinstance(errors, dict):
            message = "; ".join(str(value) for value in errors.values())
        else:
            message = str(errors)
        raise APIFootballError(message or "API-Football rejected the request.")
    return payload


def test_connection(token, season):
    payload = _request(
        token,
        "/fixtures",
        {"league": PREMIER_LEAGUE_ID, "season": season, "next": 1},
    )
    return payload.get("results", 0)


def get_live_matches(token, season):
    payload = _request(
        token,
        "/fixtures",
        {"league": PREMIER_LEAGUE_ID, "season": season, "live": "all"},
    )
    return payload.get("response", [])


def goal_events(match):
    goals = []
    for event in match.get("events") or []:
        if (event.get("type") or "").casefold() != "goal":
            continue

        detail = (event.get("detail") or "").casefold()
        if "missed" in detail:
            continue
        if "penalty" in detail:
            goal_type = "PENALTY"
        elif "own" in detail:
            goal_type = "OWN_GOAL"
        else:
            goal_type = "REGULAR"

        time_data = event.get("time") or {}
        goals.append({
            "minute": time_data.get("elapsed"),
            "injuryTime": time_data.get("extra"),
            "type": goal_type,
            "team": {"name": (event.get("team") or {}).get("name")},
            "scorer": {"name": (event.get("player") or {}).get("name")},
        })
    return goals
