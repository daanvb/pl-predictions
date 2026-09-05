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
    try:
        payload = response.json()
    except ValueError as exc:
        raise BigBallsAPIError(
            "Big Balls Sports Data returned an invalid JSON response."
        ) from exc
    if not isinstance(payload, dict):
        raise BigBallsAPIError(
            "Big Balls Sports Data returned an unexpected response format."
        )
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
    return _match_list(payload.get("data")), _meta(payload)


def get_stored_premier_league_matches(api_key, dates, limit=200):
    """Fetch archived EPL fixtures for the supplied UTC calendar dates."""
    matches = []
    seen = set()
    meta = {}
    for match_date in sorted(set(dates)):
        payload = _request(
            api_key,
            "/stored/matches",
            params={
                "sport": "football",
                "date": match_date,
                "limit": limit,
            },
        )
        data = _match_list(payload.get("data"))
        for match in data if isinstance(data, list) else []:
            match_id = match.get("id") if isinstance(match, dict) else None
            if match_id and match_id in seen:
                continue
            if match_id:
                seen.add(match_id)
            matches.append(match)
        meta = _meta(payload) or meta
    return matches, meta


def get_match_events(api_key, match_id, match=None):
    # Some live list adapters include events directly on the match row. Use
    # them before spending another request, while retaining the dedicated
    # endpoint as the canonical source.
    embedded_events = _event_list(match)
    if embedded_events:
        return embedded_events, {"source": "match-list"}
    event_error = None
    try:
        payload = _request(
            api_key,
            f"/matches/{match_id}/events",
            params={"sport": "football"},
        )
    except BigBallsAPIError as exc:
        event_error = exc
        payload = {}
    data = _event_list(payload.get("data"))
    if isinstance(data, list) and data:
        return data, _meta(payload)

    # During a live match the multi-field detail envelope can contain events
    # even when the dedicated event collection has not been populated yet.
    try:
        live_payload = _request(
            api_key,
            f"/matches/{match_id}",
            params={"sport": "football", "fields": "scores,events"},
        )
        live_events = _event_list(live_payload.get("data"))
        if live_events:
            return live_events, _meta(live_payload)
    except BigBallsAPIError:
        pass

    # Finished matches can leave the live adapter, while their stored match
    # detail remains available. The bare detail route intentionally avoids the
    # sport parameter so the gateway can fall back to that stored record.
    try:
        stored_payload = _request(api_key, f"/matches/{match_id}")
        stored = stored_payload.get("data") or {}
        if isinstance(stored, dict) and isinstance(stored.get("match"), dict):
            stored = stored["match"]
        stored_events = _event_list(stored)
        if isinstance(stored_events, list):
            return stored_events, _meta(stored_payload)
    except BigBallsAPIError:
        if event_error:
            raise event_error
    return [], _meta(payload)


def _meta(payload):
    """Keep provider metadata safe even if an upstream emits a bare source."""
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        return {"source": meta.strip()}
    return {}


def _match_list(value):
    """Accept both the documented array and occasional list envelopes."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("matches", "items", "results"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _event_list(value):
    """Accept the live and archived event container shapes used by the API."""
    if isinstance(value, list):
        return [
            item if isinstance(item, dict) else {"description": str(item)}
            for item in value
            if isinstance(item, (dict, str))
        ]
    if not isinstance(value, dict):
        return []
    for key in (
        "events", "incidents", "timeline", "match_events", "plays", "items",
        "value", "data",
    ):
        nested = value.get(key)
        if isinstance(nested, list):
            return _event_list(nested)
        if isinstance(nested, dict):
            events = _event_list(nested)
            if events:
                return events
    match = value.get("match")
    return _event_list(match) if isinstance(match, dict) else []


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


def _team_value(match, side):
    team = next((match.get(key) for key in (
        side, f"{side}_team", f"{side}Team", f"{side}_team_name",
    ) if match.get(key)), {})
    if isinstance(team, dict):
        nested = team.get("team")
        if isinstance(nested, dict):
            team = nested
        return team.get("name") or team.get("display_name") or team.get("short_name") or ""
    return str(team)


def normalize_match(match):
    home = match.get("home") or match.get("home_team") or match.get("homeTeam") or {}
    away = match.get("away") or match.get("away_team") or match.get("awayTeam") or {}
    score = match.get("score") or match.get("linescore") or {}
    if not score:
        score = {
            "home": match.get("home_score"),
            "away": match.get("away_score"),
        }
    return {
        "id": match.get("id"),
        "home": _team_value(match, "home"),
        "away": _team_value(match, "away"),
        "home_logo": home.get("logo_url") if isinstance(home, dict) else None,
        "away_logo": away.get("logo_url") if isinstance(away, dict) else None,
        "home_score": _score_value(score, "home"),
        "away_score": _score_value(score, "away"),
        "kickoff_utc": match.get("kickoff_utc"),
        "status": str(match.get("status") or "unknown").lower(),
        "raw": match,
    }
