from datetime import datetime, timezone

import requests

API_BASE = "https://api.football-data.org/v4"
COMPETITION = "PL"


class FootballAPIError(Exception):
    pass


def headers(token):
    return {
        "X-Auth-Token": token,
        "Accept": "application/json",
    }


def test_connection(token):
    if not token:
        raise FootballAPIError("No API token has been configured.")

    response = requests.get(
        f"{API_BASE}/competitions/{COMPETITION}",
        headers=headers(token),
        timeout=15,
    )

    if response.status_code == 200:
        return response.json()

    if response.status_code == 401:
        raise FootballAPIError("API token was rejected.")

    if response.status_code == 403:
        raise FootballAPIError(
            "Your API account does not have permission to access this resource."
        )

    raise FootballAPIError(
        f"Football API returned HTTP {response.status_code}"
    )


def _raise_for_match_error(response):
    if response.status_code == 401:
        raise FootballAPIError("API token was rejected.")

    if response.status_code == 403:
        raise FootballAPIError(
            "Your API account does not have permission to retrieve these fixtures."
        )

    if response.status_code != 200:
        raise FootballAPIError(
            f"Football API returned HTTP {response.status_code}"
        )


def _request_matches(token, params):
    response = requests.get(
        f"{API_BASE}/competitions/{COMPETITION}/matches",
        headers=headers(token),
        params=params,
        timeout=30,
    )

    _raise_for_match_error(response)
    return response.json().get("matches", [])


def _current_season_start_year():
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def normalize_match(match):
    """Return a stable v4 match shape for the app's importer.

    football-data v4 uses score.fullTime as the running score while a match is
    IN_PLAY. Some documentation/examples use the older homeTeam/awayTeam score
    keys, so accept both spellings rather than silently storing NULL scores.
    """
    normalized = dict(match or {})
    score = dict(normalized.get("score") or {})
    full_time = dict(score.get("fullTime") or {})

    if "home" not in full_time and "homeTeam" in full_time:
        full_time["home"] = full_time.get("homeTeam")

    if "away" not in full_time and "awayTeam" in full_time:
        full_time["away"] = full_time.get("awayTeam")

    score["fullTime"] = full_time
    normalized["score"] = score

    # LIVE is documented as a convenience filter; normal match resources use
    # IN_PLAY/PAUSED. Normalising it here keeps all UI/status logic consistent
    # if a provider response ever surfaces the pseudo-status directly.
    if normalized.get("status") == "LIVE":
        normalized["status"] = "IN_PLAY"

    return normalized


def get_matches(token, season=2026):
    if not token:
        raise FootballAPIError("No API token has been configured.")

    matches = [
        normalize_match(match)
        for match in _request_matches(
            token,
            {"season": season, "limit": 500},
        )
    ]

    # During the active PL season, overlay the provider's dedicated LIVE view
    # onto the full-season response. This gives running status/scores the best
    # chance of being fresh without changing the app's existing 5-minute live
    # polling cadence. Failure of this optional request must never prevent the
    # normal fixture refresh from succeeding.
    if season == _current_season_start_year():
        try:
            live_matches = [
                normalize_match(match)
                for match in _request_matches(
                    token,
                    {
                        "season": season,
                        "status": "LIVE",
                        "limit": 100,
                    },
                )
            ]
        except FootballAPIError:
            live_matches = []

        positions = {
            match.get("id"): index
            for index, match in enumerate(matches)
            if match.get("id") is not None
        }

        for live_match in live_matches:
            match_id = live_match.get("id")

            if match_id in positions:
                matches[positions[match_id]] = live_match
            else:
                matches.append(live_match)

    return matches
