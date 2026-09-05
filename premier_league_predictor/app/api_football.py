"""Small API-Football client used only for targeted live-data fallback."""

import requests


API_BASE = "https://v3.football.api-sports.io"


class APIFootballError(Exception):
    pass


def _headers(api_key):
    if not api_key:
        raise APIFootballError("No API-Football key has been configured.")
    return {"x-apisports-key": api_key, "Accept": "application/json"}


def _get(api_key, path, params=None):
    try:
        response = requests.get(
            f"{API_BASE}{path}", headers=_headers(api_key), params=params,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise APIFootballError("API-Football is temporarily unavailable.") from exc

    if response.status_code == 401:
        raise APIFootballError("API-Football rejected the configured key.")
    if response.status_code == 429:
        raise APIFootballError("API-Football daily request allowance has been reached.")
    if response.status_code != 200:
        raise APIFootballError(
            f"API-Football returned HTTP {response.status_code}."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise APIFootballError("API-Football returned an invalid response.") from exc
    if payload.get("errors"):
        raise APIFootballError(str(payload["errors"]))
    return payload


def test_connection(api_key):
    return _get(api_key, "/status")


def get_live_fixtures(api_key):
    """Return all live football fixtures in one API-Football request."""
    return _get(api_key, "/fixtures", {"live": "all"}).get("response", [])


def get_fixture_events(api_key, fixture_id):
    """Return events only after a targeted live-data fallback is needed."""
    return _get(api_key, "/fixtures/events", {"fixture": fixture_id}).get(
        "response", []
    )
