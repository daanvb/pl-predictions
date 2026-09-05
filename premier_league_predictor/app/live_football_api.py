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
    except requests.RequestException as exc:
        raise LiveFootballAPIError("Live Football API is temporarily unavailable.") from exc
    if response.status_code == 429:
        raise LiveFootballAPIError("Live Football API rate limit reached.")
    if response.status_code in (401, 403):
        raise LiveFootballAPIError("Live Football API key was rejected.")
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


def test_connection(api_key):
    """Use the documented match-list endpoint to validate a saved key."""
    return get_matches(api_key, date.today().isoformat())
