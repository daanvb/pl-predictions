import requests


API_BASE = "https://api.bigballsdata.com/v1"


class BigBallsAPIError(Exception):
    pass


def _headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _request(api_key, path, params=None, timeout=20):
    if not api_key:
        raise BigBallsAPIError("No Big Balls Sports Data API key is configured.")
    try:
        response = requests.get(
            f"{API_BASE}{path}",
            headers=_headers(api_key),
            params=params,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise BigBallsAPIError("Big Balls Sports Data is temporarily unavailable.") from exc
    if response.status_code == 401:
        raise BigBallsAPIError("The Big Balls Sports Data API key was rejected.")
    if response.status_code == 429:
        raise BigBallsAPIError("The Big Balls Sports Data request limit has been reached.")
    if response.status_code != 200:
        raise BigBallsAPIError(
            f"Big Balls Sports Data returned HTTP {response.status_code}."
        )
    payload = response.json()
    if payload.get("error"):
        raise BigBallsAPIError(str(payload["error"]))
    return payload


def test_connection(api_key):
    return _request(api_key, "/user/me").get("data") or {}


def get_premier_league_matches(api_key, limit=200):
    payload = _request(
        api_key,
        "/matches",
        params={"sport": "football", "league": "epl", "limit": limit},
    )
    return payload.get("data") or [], payload.get("meta") or {}


def get_match_events(api_key, match_id):
    payload = _request(api_key, f"/matches/{match_id}/events")
    return payload.get("data") or [], payload.get("meta") or {}


def _score_value(score, side):
    if not isinstance(score, dict):
        return None
    candidates = (side, f"{side}_score", "home_score" if side == "home" else "away_score")
    for key in candidates:
        value = score.get(key)
        if isinstance(value, dict):
            value = value.get("total")
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def normalize_match(match):
    home = match.get("home") or {}
    away = match.get("away") or {}
    score = match.get("score") or match.get("linescore") or {}
    return {
        "id": match.get("id"),
        "home": home.get("name") if isinstance(home, dict) else str(home),
        "away": away.get("name") if isinstance(away, dict) else str(away),
        "home_logo": home.get("logo_url") if isinstance(home, dict) else None,
        "away_logo": away.get("logo_url") if isinstance(away, dict) else None,
        "home_score": _score_value(score, "home"),
        "away_score": _score_value(score, "away"),
        "kickoff_utc": match.get("kickoff_utc"),
        "status": str(match.get("status") or "unknown").lower(),
        "raw": match,
    }
