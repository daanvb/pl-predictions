"""Small client for the optional Champions League Live Football API trial.

This module deliberately has no knowledge of the app database or the Premier
League feeds.  Keeping it here makes it possible to turn the CL trial off by
removing its key without affecting the normal live-data path.
"""

from datetime import date

import requests


API_BASE = "https://live-football-api.com/api/v1"


class LiveFootballAPIError(Exception):
    """A safe, user-facing Live Football API error."""


def _payload_data(payload):
    if not isinstance(payload, dict):
        raise LiveFootballAPIError("Live Football API returned an invalid response.")
    if payload.get("success") is False:
        raise LiveFootballAPIError(
            payload.get("message") or "Live Football API rejected the request."
        )
    return payload.get("data", payload)


def _get(api_key, path, params=None):
    if not api_key:
        raise LiveFootballAPIError("No Live Football API key is configured.")
    try:
        response = requests.get(
            f"{API_BASE}{path}",
            params={"api_key": api_key, **(params or {})},
            timeout=20,
        )
    except requests.Timeout as exc:
        raise LiveFootballAPIError(
            "Live Football API did not respond within 20 seconds."
        ) from exc
    except requests.RequestException as exc:
        raise LiveFootballAPIError(
            "Live Football API could not be reached."
        ) from exc
    if response.status_code == 429:
        raise LiveFootballAPIError("Live Football API rate limit reached.")
    if response.status_code in (401, 403):
        raise LiveFootballAPIError("Live Football API key was rejected.")
    if response.status_code == 503:
        raise LiveFootballAPIError(
            "Live Football API's upstream data source is temporarily unavailable."
        )
    if response.status_code != 200:
        raise LiveFootballAPIError(
            f"Live Football API returned HTTP {response.status_code}."
        )
    try:
        return _payload_data(response.json())
    except ValueError as exc:
        raise LiveFootballAPIError("Live Football API returned invalid JSON.") from exc


def get_matches(api_key, match_date):
    """Return every match supplied for one UTC date (one API credit)."""
    data = _get(api_key, "/matches", {"date": str(match_date)})
    if isinstance(data, list):
        return data
    return data.get("matches") or data.get("data") or []


def get_live_match_details(api_key, match_id):
    """Return detailed events and live status for one provider match."""
    return _get(api_key, "/live_match_details", {"match_id": str(match_id)})


def search_teams(api_key, query):
    """Return provider team records matching a club name."""
    data = _get(api_key, "/team_search", {"q": str(query)})
    return data.get("teams") or [] if isinstance(data, dict) else []


def get_team_matches(api_key, team_id, season):
    """Return one club's completed and scheduled matches for a provider season."""
    data = _get(api_key, "/team_matches", {
        "team_id": str(team_id), "season": str(season),
    })
    return data.get("matches") or [] if isinstance(data, dict) else []


def get_head_to_head(api_key, match_id):
    """Return the provider's historical meetings for a fixture (one credit)."""
    return _get(api_key, "/h2h", {"match_id": str(match_id)})


def test_connection(api_key):
    """Use the documented match-list endpoint to validate a saved key."""
    return get_matches(api_key, date.today().isoformat())
